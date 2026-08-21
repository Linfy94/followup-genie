#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目跟进精灵 · 自检（业务可随时自己跑）。

用法：
  python3 doctor.py                  # 全项自检
  python3 doctor.py --validate-config  # 只查配置，不联网

设计原则：**每一项失败都要说清「这不是没有超时单，这是故障」**。
凭证过期、字段改名、权限没给，都会表现成"今天很安静"——那是最隐蔽的失败模式。
"""

from __future__ import annotations  # 兼容 Python 3.9（macOS 自带版本）

import argparse
import json
import sys
from datetime import date, datetime

import cli_env
import core
import nethttp
import qqdoc
import lark_base
import wecom_doc
from core import LedgerError

OK, WARN, BAD = "✅", "⚠️ ", "🔴"


class Doc:
    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []
        self.bad = 0
        self.warn = 0

    def add(self, level: str, title: str, detail: str = "") -> None:
        self.rows.append((level, title, detail))
        if level == BAD:
            self.bad += 1
        elif level == WARN:
            self.warn += 1

    def render(self) -> str:
        out = ["", "🧚 项目跟进精灵 · 自检报告", "=" * 52]
        for level, title, detail in self.rows:
            out.append(f"{level} {title}")
            if detail:
                for ln in detail.splitlines():
                    out.append(f"     {ln}")
        out.append("=" * 52)
        if self.bad:
            out.append(f"{BAD} {self.bad} 项失败、{self.warn} 项提醒 —— 有故障，催办结果不可信")
        elif self.warn:
            out.append(f"{WARN}全部通过，但有 {self.warn} 项提醒值得看一眼")
        else:
            out.append(f"{OK} 全部通过")
        return "\n".join(out)


def check_runtime(doc: Doc) -> None:
    """
    这一趟跑在哪个 Python 上，以及它是不是定时任务用的那个。

    ═══════════════════════════════════════════════════════════════════
    🔴 2026-08-14 排查一天三个故障时，`doctor` 关于运行环境**一个字都不报**。
       这台机器上有三个 Python、三套 SSL 后端（LibreSSL 2.8.3 不支持 TLS 1.3，
       另两个支持），同一份代码在不同解释器下表现完全相反。
       我自己敲了裸 `python3` 落到 Homebrew 那个，把「用错解释器」
       和「真故障」混在一起，白花近一小时。这两行就是为了消灭那一小时。

       比对靠 health.json 里上一次**真实运行**记下的指纹，
       **不写死任何路径** —— 写死只在这台 Hermes 上成立，
       业务电脑和 WorkBuddy 的路径都不同。
    ═══════════════════════════════════════════════════════════════════
    """
    me = core.runtime_fingerprint()
    doc.add(OK, "运行环境",
            f"解释器：{me['executable']}\n"
            f"Python：{me['python']}｜SSL：{me['ssl']}")

    try:
        theirs = (core.read_health() or {}).get("runtime") or {}
    except Exception:  # noqa: BLE001 —— 自检不能因为读不到健康记录就裸崩
        theirs = {}
    if not theirs.get("executable"):
        doc.add(OK, "定时任务用的解释器：还没有记录",
                "下一次真实运行（不是 --dry-run）会记下来，之后这里会自动比对。")
        return
    if theirs["executable"] == me["executable"]:
        doc.add(OK, "解释器与定时任务一致")
        return
    doc.add(WARN, "🔴 你现在这个解释器，不是定时任务跑的那个",
            f"定时任务：{theirs['executable']}\n"
            f"          Python {theirs.get('python', '?')}｜SSL {theirs.get('ssl', '?')}\n"
            f"你现在　：{me['executable']}\n"
            f"          Python {me['python']}｜SSL {me['ssl']}\n"
            "SSL 后端不同会让 TLS 行为不同（LibreSSL 2.8.3 不支持 TLS 1.3，"
            "OpenSSL 3.x 默认用它）。\n"
            "在这里试出来的结果，不代表明天 9:00 的结果 —— "
            "要复现定时任务，请用上面那个解释器跑。")


def check_tls(doc: Doc) -> None:
    """
    这一趟有没有因为 TLS 1.3 坏掉而降级到 1.2。

    ═══════════════════════════════════════════════════════════════════
    🔴 2026-08-14：本机代理把**所有** TLS 1.3 记录搞坏（腾讯文档、企微、
       飞书、乃至 Google 全断，TLS 1.2 全通）。当天读数正常，但企微推送
       0/1 条失败 —— 业务没收到清单，而报错只是一行 SSL 异常。
       程序现在能自动降到 TLS 1.2（scripts/nethttp.py + scripts/cli_env.py），
       所以这**不再是故障**，但它值得一眼看见。

    🔴 这里刻意**不做主动探针**，试过，砍掉了：
       写过一版「对各服务分别用 TLS1.3 / TLS1.2 各握一次手」的探针，
       实测它同一份报告里给出自相矛盾的结论 —— 上面刚报「本次已降级」，
       下面却说三个站 TLS1.3 全通，还把一个实际读取成功的域名判成「连不上」。
       原因是探针只发一个 `HEAD /`，太小，复现不出 bad record mac
       这种与数据量相关的故障；而 curl 与 Python 对同一主机的表现也不一致。

       **一份自相矛盾的自检报告比没有更糟** —— 它会让人怀疑整份报告。
       下面这个判据来自**这一趟的真实流量**，不会说谎，而且零额外网络。
    ═══════════════════════════════════════════════════════════════════
    """
    degraded = []
    if nethttp.degraded():
        degraded.append("Python 侧（腾讯文档取数 / 企微推送）")
    if cli_env.tls_degraded():
        degraded.append("命令行工具侧（lark-cli / wecom-cli）")
    if not degraded:
        doc.add(OK, "TLS：本次运行未发生降级")
        return
    doc.add(WARN, "本次运行已降级到 TLS 1.2",
            "、".join(degraded) + "\n"
            "程序自己扛住了，催办不受影响 —— 但这说明本机到外网的 TLS 1.3 是坏的，"
            "多半是代理/VPN 软件。\n"
            "想自己确认，在终端跑：curl -sI --tlsv1.3 https://qyapi.weixin.qq.com\n"
            "网络修好后无需改配置：每个进程都会先试 TLS 1.3，自动恢复。")


def check_configs(doc: Doc) -> tuple[dict, dict, dict] | None:
    try:
        ledgers_cfg, rules_cfg, output_cfg = core.load_configs()
    except LedgerError as e:
        doc.add(BAD, "配置文件", str(e))
        return None
    doc.add(OK, "配置文件可读", f"目录：{core.config_dir()}")

    # ── 结构校验：与主脚本共用同一份实现 ──
    # 🔴 必须在这里提前返回。下面每一行都在 .get() 链上，
    #    `"ledgers": {}` 或 `"rulesets": []` 会让**自检本身**抛裸 traceback ——
    #    一个诊断工具在被诊断的东西坏掉时崩溃，等于没有诊断工具。
    cfg_errs = core.validate_configs(ledgers_cfg, rules_cfg, output_cfg)
    if cfg_errs:
        for e in cfg_errs:
            doc.add(BAD, "配置结构错误", e)
        doc.add(BAD, "配置结构不合法，后续检查全部跳过",
                "先修好上面这些，再跑一次 doctor")
        return None
    doc.add(OK, "配置结构校验通过", "台账数组、规则集、节点阈值、输出各段类型均正确")

    # ── 主通知通道：错配会让状态永远提交不了、天天重推 ──
    primary = ((output_cfg.get("notify") or {}).get("primary") or "wecom_webhook")
    if primary not in ("wecom_webhook", "stdout"):
        doc.add(BAD, f"notify.primary＝{primary!r} 不认识",
                "只能是 wecom_webhook 或 stdout")
    elif primary == "wecom_webhook":
        if (output_cfg.get("wecom_webhook") or {}).get("enabled"):
            doc.add(OK, "主通知通道：企微群机器人",
                    "只有它完整送达，项目才会被记为「已通知」进入静默期")
        else:
            doc.add(BAD, "主通道指向企微，但企微通道是关闭的",
                    "这样永远拿不到投递凭证，会天天重推同一批。\n"
                    "要么启用 wecom_webhook，要么把 notify.primary 改成 stdout")
    else:
        doc.add(WARN, "主通知通道：stdout",
                "stdout 打印即视为送达，**没有任何投递保证**。\n"
                "只在没有企微通道时才该这么配。")

    active = [l for l in ledgers_cfg.get("ledgers", []) if l.get("enabled")]
    if not active:
        doc.add(BAD, "没有启用的台账", "config/ledgers.json 里所有台账的 enabled 都是 false")
    else:
        doc.add(OK, f"启用了 {len(active)} 份台账",
                "、".join(f"{l.get('name')}（{l.get('line')}）" for l in active))

    # 规则集引用完整性
    for l in active:
        rs = (rules_cfg.get("rulesets") or {}).get(l.get("ruleset"))
        if not rs:
            doc.add(BAD, f"台账「{l.get('name')}」引用的规则集不存在",
                    f"ruleset={l.get('ruleset')!r}，rules.json 里没有")
            continue
        nodes = rs.get("nodes") or []
        on = [n for n in nodes if n.get("enabled")]
        off = [n for n in nodes if not n.get("enabled")]
        doc.add(OK, f"规则集「{l.get('ruleset')}」：{len(on)} 个节点启用",
                "、".join(n.get("name", "?") for n in on))
        # 禁用的节点必须显式列出 —— 一个悄悄不跑的规则比一个跑错的规则更难发现
        #
        # 🔴 两种拼法都要认。配置里实际存在 `_禁用原因` 与 `_停用说明` 两个键，
        #    而这里原本只读前者 —— 于是盒子线①收资那段写得最详细的停用依据
        #    （含业务 2026-08-18「永久取消、不要再改回 true」的定论）
        #    在 doctor 里根本看不到，只打通用的「配置里 enabled=false」。
        #    2026-08-18 起「⏸ 未启用」不再进企微推送，**doctor 成了这条信息的主通道**，
        #    主通道说不出理由就失去了意义，所以这里必须两种都读。
        for n in off:
            doc.add(WARN, f"节点「{n.get('name')}」未启用（不会产生任何催办）",
                    n.get("_禁用原因") or n.get("_停用说明")
                    or "配置里 enabled=false")
        # 阈值与复提醒必填
        # 🔴 repeat 的检查**必须调 core 的那一份**，不许在这里再写一遍。
        #    自带一份的下场已经见过：它只认 days/workdays/weekday，
        #    rc8 加的 monthday 没跟上，配了 monthday 的节点会被误报「缺 repeat」。
        for n in on:
            thr = n.get("threshold") or {}
            if not ("days" in thr or "workdays" in thr):
                doc.add(BAD, f"节点「{n.get('name')}」缺 threshold.days/workdays")
            for e in core.repeat_errors(n.get("repeat"), f"节点「{n.get('name')}」"):
                doc.add(BAD, "复提醒节律配置有误", e)

        # ── 阈值边界对照表 ──
        # 「满7天」到底是第7天还是第8天提醒，光看 threshold.days 看不出来。
        # 这张表就是给业务看的口径凭证，出现争议时按它对。
        rows = []
        bad_boundary = False
        for n in nodes:
            thr = n.get("threshold") or {}
            b = thr.get("boundary", "after")
            if b not in ("on", "after"):
                bad_boundary = True
                rows.append(f"{n.get('name'):<10} boundary={b!r} 🔴 不合法")
                continue
            # 节律文案只有一份实现（core.cadence_text），两套渲染与这张
            # 口径表共用 —— 这张表是给业务核对「规则改成什么了」的凭证，
            # 和推送里说的必须是同一句话。
            every = core.cadence_text(n.get("repeat")) or "—"
            mark = "" if n.get("enabled") else "（未启用）"
            unit = "个工作日" if "workdays" in thr else "天"
            amount = thr.get("workdays") if "workdays" in thr else thr.get("days")
            rows.append(
                f"{n.get('name', '?'):<10} 在本节点满 {amount} {unit} → "
                f"第 {core.first_reminder_day(n)} {unit}首次提醒（显示「超期 1 天」），"
                f"之后{every}{mark}"
            )
        rows.append("")
        rows.append("推送里显示的是「超期天数」＝ 在本节点的天数 − 允许天数"
                    f"（允许天数 = 首次提醒日 − 1）。")
        rows.append("允许天数：" + "、".join(
            f"{n.get('name', '?')} {core.allowance_days(n)} 天" for n in nodes))
        doc.add(BAD if bad_boundary else OK, "阈值边界与超期天数口径",
                "\n".join(rows))

    # 写入开关
    write_on = core.reminders_write_enabled(output_cfg)
    if write_on:
        doc.add(WARN, "提醒事项写入：已开启（会创建真实提醒）",
                "若这是开发机，应把 output.json 的 reminders.write 改回 false")
    else:
        doc.add(OK, "提醒事项写入：关闭（演练模式）",
                "只打印本该创建哪些提醒，不产生任何真实提醒。\n"
                "迁到业务电脑后把 output.json 的 reminders.write 改成 true")

    # 节假日表
    hol_path = core.config_dir() / "holidays.json"
    wd_cfg = rules_cfg.get("workday") or {}
    wd_nodes = core.nodes_using_workdays(rules_cfg)
    if wd_cfg.get("exclude_holidays"):
        try:
            hol = core.load_json(hol_path, "节假日表") if hol_path.exists() else None
        except LedgerError as e:
            doc.add(BAD, "节假日表损坏", f"{e}\n每日运行会以启动阶段故障退出（退出码 2）")
            return ledgers_cfg, rules_cfg, output_cfg
        wc = core.WorkdayCalc(wd_cfg, hol, wd_nodes)
        if wc.holiday_warning:
            doc.add(WARN, "节假日表需要处理", wc.holiday_warning)
        else:
            doc.add(OK, f"节假日表就绪（{len(wc.holidays)} 个假日）")
    elif wd_nodes:
        # 有人依赖工作日口径，却没启用节假日表 —— 装机漏拷 holidays.json
        # 最典型的表现就是这个，而它原本完全无声。
        wc = core.WorkdayCalc(wd_cfg, None, wd_nodes)
        doc.add(WARN, "工作日口径与规则对不上", wc.holiday_warning)
    else:
        doc.add(OK, "工作日口径：仅排除周末（没有节点按工作日复提醒，无需节假日表）")

    return ledgers_cfg, rules_cfg, output_cfg


def needs_tencent_token(enabled_ledgers: list) -> bool:
    """
    这台机器到底要不要腾讯文档凭证。

    🔴 纯飞书用户没有、也不该有 TENCENT_DOCS_TOKEN。以前这里无条件查，
    会给他一条红色的「腾讯文档凭证缺失」—— 自检报红是最强的「别用」信号，
    业务会卡在一个跟他完全无关的东西上。

    注意默认值：source 缺省是 tencent_mcp，所以没写 source 的台账算腾讯文档。
    """
    return any(l.get("source", "tencent_mcp") == "tencent_mcp"
               for l in enabled_ledgers if isinstance(l, dict))


def check_credential(doc: Doc) -> bool:
    """腾讯文档凭证。只影响 source=tencent_mcp 的台账。"""
    try:
        qqdoc.load_token()
    except LedgerError as e:
        doc.add(BAD, "腾讯文档凭证缺失", f"{e}\n"
                "🔴 注意：凭证问题会表现成「今天没有超时单」，不是正常状态")
        return False
    doc.add(OK, "腾讯文档凭证已配置", "（内容不打印）")
    return True


def check_lark_credential(doc: Doc, ledger: dict) -> bool:
    """飞书 lark-cli 身份。只影响 source=lark_cli 的台账，每个 profile 查一次即可。"""
    profile = ledger.get("profile", "sentinel")
    try:
        lark_base.check_credential(ledger["base_token"], profile)
    except LedgerError as e:
        doc.add(BAD, f"飞书身份（profile={profile}）不可用", f"{e}\n"
                "🔴 注意：这会表现成「今天没有超时单」，不是正常状态。\n"
                "请按上面的错误分类处理；不要在重新登录和添加协作者之间反复尝试。")
        return False
    doc.add(OK, f"飞书身份（profile={profile}）可用", "（内容不打印）")
    return True


def check_wecom_doc_credential(doc: Doc, ledger: dict) -> bool:
    """
    企微文档能不能读。只影响 source=wecom_doc 的台账，同一份文档探一次。

    🔴 这里报红的最常见原因是**机器人的「获取成员文档内容」能力授权掉了**
       （errcode 851008）。那种情况下取数会返回 0 行 —— 也就是表现成
       「今天没有超时单」，所以必须在自检里单独点名。
    """
    try:
        wecom_doc.check_credential(ledger["url"])
    except LedgerError as e:
        doc.add(BAD, "企业微信文档读取权限不可用", f"{e}\n"
                "🔴 注意：这会表现成「今天没有超时单」，不是正常状态。\n"
                "错误详情已按 errcode 给出唯一处理动作：851008 补机器人能力；"
                "851003 核对机器人对该文档的对象权限；851002 按失败命令核对链接或正文兼容性。")
        return False
    doc.add(OK, "企业微信文档可读", "（内容不打印）")
    return True


def check_ledger(doc: Doc, ledger: dict, rules_cfg: dict) -> None:
    name = ledger.get("name")
    source = ledger.get("source", "tencent_mcp")
    fp_before = None

    if source == "tencent_mcp":
        try:
            fp_before = qqdoc.file_fingerprint(ledger["file_id"])
        except LedgerError as e:
            doc.add(BAD, f"台账「{name}」不可访问",
                    f"{e}\n🔴 这是故障，不是「今天没有超时单」")
            return
        doc.add(OK, f"台账「{name}」可访问",
                f"最后修改：{fp_before.get('last_modify_name')} / {fp_before.get('last_modify_time')}")

    try:
        sheet = core.read_ledger_sheet(ledger)
    except LedgerError as e:
        doc.add(BAD, f"台账「{name}」读取失败", str(e))
        return

    ruleset = (rules_cfg.get("rulesets") or {}).get(ledger.get("ruleset")) or {}
    a = core.assert_sheet(sheet, ledger, ruleset)
    for f in a.fatal:
        doc.add(BAD, f"台账「{name}」断言失败", f)
    for w in a.warnings:
        doc.add(WARN, f"台账「{name}」", w)
    if a.ok and not a.warnings:
        rows = [r for r in sheet.data_rows if sheet.text(r, ledger.get("name_field", "项目名称"))]
        doc.add(OK, f"台账「{name}」结构与数据一致性检查通过",
                f"{len(rows)} 个有效项目，{len([h for h in sheet.header if h])} 个有名列")

    # 「终止」列状态 —— 新冷启动下它是唯一的退出阀门
    tc = ledger.get("terminal_column") or {}
    if tc.get("enabled"):
        if sheet.has_column(tc.get("field", "")):
            doc.add(OK, f"「{tc['field']}」列已接入（终止判定优先级 1）")
        else:
            doc.add(BAD, f"配置说「{tc.get('field')}」列已启用，但台账里没有这一列")
    else:
        exists = sheet.has_column(tc.get("field") or "终止")
        if exists:
            doc.add(WARN, f"台账已新增「{tc.get('field') or '终止'}」列，但配置里还没启用",
                    "把 config/ledgers.json 的 terminal_column.enabled 改成 true 即可接入")
        else:
            doc.add(WARN, "台账还没有「终止」列",
                    "当前冷启动策略下它是业务唯一的退出阀门（改终止 或 推进节点）。\n"
                    "缺它的话，业务遇到「想让某条别催了」时没有正确的表达方式。\n"
                    + (f"台账 owner：{fp_before.get('last_modify_name')}" if fp_before else ""))

    # 只读性验证：读了一整轮，确认没动过。另外两个数据源都拿不到等价指纹，
    # 只能把这件事明说出来 —— 「没验」和「验过了」必须分得开。
    if source != "tencent_mcp":
        gate = {
            "lark_cli": "lark_base.ALLOWED_SUBCOMMANDS"
                        "（+table-list / +field-list / +record-list）",
            "wecom_doc": "wecom_doc.ALLOWED_SUBCOMMANDS"
                         "（sheet_get_info / get_doc_content）",
        }.get(source, "该数据源的只读命令白名单")
        extra = ""
        if source == "wecom_doc":
            # 2026-08-10 实测：sheet_get_info 顶层只有 errcode/errmsg/name/sheets/url
            extra = ("\n实测 sheet_get_info 的返回里没有修改时间，"
                     "所以这条验证在企微上做不了，不是漏做。")
        doc.add(WARN, f"台账「{name}」只读性验证未实现",
                f"数据源 {source} 暂无「最后修改人/时间」等价指纹可核对，"
                f"只依赖 {gate} 这道白名单兜底。{extra}")
        return
    try:
        fp_after = qqdoc.file_fingerprint(ledger["file_id"])
    except LedgerError as e:
        doc.add(WARN, "只读性验证未能完成", str(e))
        return
    same = (fp_before.get("last_modify_time") == fp_after.get("last_modify_time")
            and fp_before.get("last_modify_name") == fp_after.get("last_modify_name"))
    if same:
        doc.add(OK, f"只读性验证通过 —— 台账「{name}」未被修改")
    else:
        doc.add(BAD, f"🔴 检测到台账「{name}」被修改",
                "请立即停用并检查命令白名单实现。这是一票否决项。")


def check_reminders(doc: Doc, output_cfg: dict) -> None:
    if sys.platform != "darwin":
        doc.add(WARN, "非 macOS，提醒事项通道不可用", "Windows 形态需走企微 webhook")
        return
    try:
        import reminders_sync
    except Exception as e:
        doc.add(BAD, "reminders_sync 模块加载失败", str(e))
        return
    ok, msg = reminders_sync.probe()
    if ok:
        doc.add(OK, "macOS 自动化权限（提醒事项）可用", msg)
        doc.add(WARN, "但这只证明「当前运行路径」有权限",
                "TCC 权限按发起进程记录。终端里通过不代表 cron 通过，且失败是静默的。\n"
                "装机时必须用 cron 的实际运行路径复验：\n"
                "  hermes cron run \"项目跟进精灵\"")
    else:
        doc.add(WARN, "macOS 自动化权限（提醒事项）不可用", msg)


def check_wecom(doc: Doc, output_cfg: dict) -> None:
    """
    企微通道自检。**不发测试消息** —— 那会打扰业务群里的真人。
    只检查「配置说要发」和「凭证在不在」是否一致。
    """
    cfg = (output_cfg or {}).get("wecom_webhook") or {}
    if not cfg.get("enabled"):
        doc.add(OK, "企微推送：未启用", "业务不会收到企微消息")
        return
    try:
        import wecom_push
    except Exception as e:
        doc.add(BAD, "wecom_push 模块加载失败", str(e))
        return
    url = wecom_push._webhook_url()
    if not url:
        doc.add(BAD, "企微推送已启用，但找不到 webhook 地址",
                "请在 <运行时目录>/.env 里配 FOLLOWUP_WECOM_WEBHOOK。\n"
                "🔴 当前状态下业务收不到任何消息，而且不会有人发现")
        return
    if "qyapi.weixin.qq.com" not in url:
        doc.add(WARN, "企微 webhook 地址看起来不对",
                "正常应形如 https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…")
        return
    doc.add(OK, "企微推送：已启用且凭证就绪",
            f"消息类型 {cfg.get('msgtype', 'markdown')}，"
            f"超 {cfg.get('split_bytes', 4000)} 字节自动拆条（地址不打印）\n"
            "注：自检不发测试消息，避免打扰业务群")


def check_alert(doc: Doc, output_cfg: dict) -> None:
    """
    告警通道自检。**不发测试消息** —— 同企微，不打扰真人。

    这条通道的意义：企微是唯一的内容通道，它挂了就是完全静默，
    而「完全静默」和「今天没有超时单」长得一模一样。
    """
    cfg = (output_cfg or {}).get("alert") or {}
    if not cfg.get("enabled", True):
        doc.add(WARN, "故障告警：已关闭",
                "取数失败、企微推送失败时不会有任何人被通知，\n"
                "只能靠 health.json 和 doctor 事后发现")
        return
    target = core.read_env("FOLLOWUP_ALERT_TARGET")
    if not target:
        doc.add(WARN, "故障告警：已启用但没有目标",
                "请在 <运行时目录>/.env 配 FOLLOWUP_ALERT_TARGET（形如 telegram:<chat_id>）。\n"
                "当前状态下告警降级为只写 health.json + stderr。")
        return
    import check_followup
    exe = check_followup._hermes_bin()
    if not exe:
        doc.add(BAD, "故障告警：找不到 hermes 可执行文件",
                "PATH 里没有 hermes，告警发不出去。\n"
                "🔴 注意 cron 的 PATH 与登录终端不同 —— 这里通过不代表 cron 通过")
        return
    doc.add(OK, "故障告警通道就绪",
            f"目标平台 {target.split(':', 1)[0]}（具体地址不打印）｜可执行 {exe}\n"
            "注：自检不发测试消息")


def check_health(doc: Doc) -> None:
    """
    健康记录自检。

    🔴 **这是唯一能抓到「根本没跑」的检查。**
    关机、休眠、gateway 没起来、cron 被误删 —— 这些失败连 stderr 都不会产生，
    脚本压根没执行，别的检查全都测不到。只有「上次成功是什么时候」发现得了。
    """
    h = core.read_health()
    if not h:
        doc.add(WARN, "还没有健康记录（health.json 为空）",
                "说明加固后还没真实跑过一次。首次 cron 运行后会生成。")
        return

    now = datetime.now().astimezone()
    last_ok = core.parse_dt(h.get("last_full_success"))
    if not last_ok:
        doc.add(WARN, "从未有过一次完整成功的运行",
                f"最近一次失败：{(h.get('last_failure') or {}).get('reason', '未记录')}")
    else:
        age_h = (now - last_ok).total_seconds() / 3600
        if age_h > 48:
            doc.add(BAD, f"已经 {int(age_h // 24)} 天没有成功运行过",
                    f"上次成功：{h.get('last_full_success')}\n"
                    "🔴 常见原因：电脑关机/休眠、gateway 没起来、cron 被删。\n"
                    "   这类失败不会产生任何报错——业务只会觉得「最近很安静」。\n"
                    "   查：hermes cron list")
        elif age_h > 26:
            doc.add(WARN, f"上次成功运行在 {int(age_h)} 小时前",
                    f"{h.get('last_full_success')}（每天 9:00 一次，超过 26 小时值得看一眼）")
        else:
            doc.add(OK, f"上次完整成功：{h.get('last_full_success')}")

    n = int(h.get("consecutive_failures") or 0)
    if n:
        f = h.get("last_failure") or {}
        doc.add(BAD, f"连续失败 {n} 次",
                f"最近一次：[{f.get('stage')}] {f.get('reason', '')[:300]}\n"
                f"时间：{f.get('at')}")
    if h.get("alert_ok") is False:
        doc.add(BAD, "上次故障告警没发出去",
                f"{h.get('alert_detail', '')}\n"
                "🔴 出了事连通知都发不出去，等于故障对外完全不可见")
    if h.get("last_wecom_ok"):
        doc.add(OK, f"上次企微推送成功：{h.get('last_wecom_ok')}")

    # ── 最近一次运行摘要 ──
    # Hermes --no-agent 模式下 stdout 可能不落在人眼前，这份摘要是事后
    # 唯一能回答「上次到底读了多少、催了几个、发没发出去」的地方。
    s = h.get("last_run_summary")
    if isinstance(s, dict):
        doc.add(OK, f"最近一次运行摘要（{s.get('at', '时间未记')}）",
                f"读取 {s.get('read')} 项 ｜ 待催 {s.get('due')} 项 ｜ "
                f"静默期 {s.get('muted')} 项\n"
                f"消息 {s.get('messages')} 条 ｜ 投递：{s.get('delivery')}\n"
                f"数据质量提示 {s.get('data_quality_warnings')} 条")

    # ── 状态损坏的恢复事件 ──
    # 坏文件被改名保留过一次，就该一直看得见，直到有人处理掉。
    rec = h.get("last_recovery")
    if isinstance(rec, dict):
        doc.add(WARN, f"曾从状态损坏中恢复过（{rec.get('at', '时间未记')}）",
                f"受损：{'、'.join(rec.get('damaged') or []) or '未记录'}\n"
                f"已保留为：{'、'.join(rec.get('files') or []) or '（未能改名）'}\n"
                "坏文件没有删除。确认无用后可自行清理，"
                "但先看一眼里面是不是有值得捞回来的记录。")


def check_state(doc: Doc) -> None:
    se = core.read_state("stage_entered.json")
    fs = core.read_state("followup_state.json")
    sh = core.read_state("stage_history.json")
    if core.STATE_DAMAGE:
        for d in core.STATE_DAMAGE:
            doc.add(BAD, "状态文件损坏",
                    d["message"] + "\n自检只报告不修复 —— 改名保留由下一次真实运行做")
    # 🔴 上一次状态改写没走完（多半是升级迁移崩在半路）。自检只报告不修复，
    #    跟上面的损坏文件同一条规矩 —— 修复是写入，属于真实运行的事。
    txn = core.pending_state_transaction()
    if txn is not None:
        doc.add(BAD, "上一次状态改写没走完",
                f"事务日志还在（备份：{txn.get('backup_dir') or '(日志已损坏，读不出)'}）。\n"
                "这说明有一次状态改写崩在半路，当前状态可能是半写的。\n"
                "下一次真实运行会在拿到锁之后自动照备份复原并告警；"
                "在那之前，判定结果不可信。")
    if not se:
        doc.add(WARN, "还没有节点进入时间记录（stage_entered.json 为空）",
                "说明还没跑过。首次运行会用「最新进展日期」初始化 —— 这是刻意的：\n"
                "若初始化成「今天」，停滞 171 天的项目会被算成 0 天，首日一条都不催。")
    else:
        doc.add(OK, f"节点进入时间记录：{len(se)} 条")
    if sh:
        cycles = sum(len(v) for v in sh.values())
        doc.add(OK, f"阶段流转历史：{len(sh)} 个节点、{cycles} 个已结束周期",
                "跑够 1~2 个月后，这就是各阶段真实耗时的数据来源")
    doc.add(OK, f"催办状态记录：{len(fs)} 条", f"目录：{core.state_dir()}")

    # 运行锁：卡死的锁会让之后每天都静默跳过
    lock = core.state_dir() / core.LOCK_FILE
    if lock.exists():
        try:
            info = json.loads(lock.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            info = {}
        started = core.parse_dt(info.get("started_at"))
        age = ((datetime.now().astimezone() - started).total_seconds()
               if started else None)
        alive = core._pid_alive(info.get("pid")) if isinstance(info.get("pid"), int) else False
        if alive and age is not None and age < core.LOCK_STALE_SECONDS:
            doc.add(WARN, "当前有一次运行正在进行",
                    f"pid {info.get('pid')}，开始于 {info.get('started_at')}")
        else:
            doc.add(WARN, "残留了一个陈旧运行锁",
                    f"{lock}\n下次运行会自动夺回并告警，不需要手动删。\n"
                    f"pid {info.get('pid')}（存活={alive}）")


def list_values(ledger: dict, rules_cfg: dict, limit: int = 60) -> int:
    """
    只读枚举一份台账里、判据用到的每一列的**真实**取值。

    ═══════════════════════════════════════════════════════════════════
    改触发条件之前必须先看这个。业务口头说的写法照抄进配置，
    **会一条都匹配不上** —— 而且不报错，只表现为那个节点从此不催：

      · 同一个文件里三个子表，分行写「杭州」/「杭州分行」两种
      · GEO 的「启动优化时间」全表 43 种写法，业务说的「未开始」
        在表里根本不存在（实际是「未开始，等客户确认平台」等 7 种）

    这两条都是靠一次性临时脚本发现的，每次要用都得重写一遍。做成常驻命令。

    只列判据引用到的列（when / scope_filters / known_values / 计时起点 /
    主键），不是全表 —— 全表没有哪一列值得看，反而把真正要核的淹掉。

    🔴 这里可以截断。业务口径③「任何分组都全量列出、不许出现『另有 N 条』」
       管的是**给业务的催办清单**；这是给改配置的人看的诊断输出，
       一列几百种取值全打出来只会没法读。截断处会明说还剩多少。
    ═══════════════════════════════════════════════════════════════════
    """
    name = ledger.get("name") or ledger.get("id")
    try:
        sheet = core.read_ledger_sheet(ledger)
    except LedgerError as e:
        print(f"❌ 台账「{name}」读取失败：{e}", file=sys.stderr)
        return 1

    ruleset = (rules_cfg.get("rulesets") or {}).get(ledger.get("ruleset")) or {}

    # 收集「判据真正依赖的列」。与 assert_sheet 那套引用收集同一个口径，
    # 只是这里连停用的节点也列 —— 改配置时常常正要把某个停用节点打开。
    fields: dict = {}

    def note(field, why):
        if field:
            fields.setdefault(field, set()).add(why)

    # 🔴 主键列与项目名称列**故意不列**。它们是身份，不是判据 ——
    #    没有人会对企业名写触发条件，而把它们打出来只是把上百个客户名
    #    刷满屏幕，把真正要核的那两三列淹掉。主键的唯一性另有 assert_sheet 管。
    for c in (ledger.get("scope_filters") or []):
        note(c.get("field"), "责任范围过滤")
    for f in (ledger.get("known_values") or {}):
        note(f, "取值白名单")
    for node in ruleset.get("nodes") or []:
        why = f"节点「{node.get('name') or node.get('id')}」" \
              + ("" if node.get("enabled") else "（未启用）")
        for c in (node.get("when") or []):
            if isinstance(c, dict):
                note(c.get("field"), why)
        clock = node.get("clock") or {}
        note(clock.get("field"), why + " 计时起点")
        for f in core.clock_fallback_fields(clock):
            note(f, why + " 计时兜底")

    rows = sheet.data_rows
    print(f"── 台账「{name}」（{ledger.get('source', 'tencent_mcp')}）"
          f"共 {len(rows)} 行 ──\n")

    for field in sorted(fields):
        why = "、".join(sorted(fields[field]))
        if not sheet.has_column(field):
            print(f"🔴 「{field}」台账里没有这一列 —— 配置引用了它（{why}）。"
                  f"判据会静默失效。\n")
            continue
        counts: dict = {}
        for r in rows:
            counts[sheet.text(r, field)] = counts.get(sheet.text(r, field), 0) + 1
        blank = counts.pop("", 0)
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        print(f"【{field}】{len(ordered)} 种取值（{why}）"
              + (f"，另有 {blank} 行为空" if blank else ""))
        for value, n in ordered[:limit]:
            # 自由文本列会有整段话，截一下才读得下去；配置里要写的是
            # 完整值，所以截断处明确标出来，不让人照着半截抄。
            shown = value if len(value) <= 60 else value[:60] + f"…（共 {len(value)} 字）"
            print(f"    {n:5d} × {shown!r}")
        if len(ordered) > limit:
            rest = sum(n for _, n in ordered[limit:])
            print(f"    …… 还有 {len(ordered) - limit} 种（共 {rest} 行）未显示")
        print()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="项目跟进精灵 · 自检")
    ap.add_argument("--validate-config", action="store_true",
                    help="只查配置，不联网、不读台账")
    ap.add_argument("--values", metavar="台账id",
                    help="只读列出这份台账里判据用到的各列真实取值（改触发条件前先看它）")
    args = ap.parse_args()

    # 自检是纯诊断工具，任何情况下都不写状态、不改坏文件、不发消息。
    # 这里显式声明出来，而不是靠「它现在恰好没有写入代码」。
    core.set_read_only(True)

    doc = Doc()
    # 排在最前：后面每一项的结论都依赖「这是哪个解释器」。
    # 离线也跑 —— 用错解释器这件事和联不联网无关。
    check_runtime(doc)
    cfgs = check_configs(doc)
    if cfgs is None:
        print(doc.render())
        return 2
    ledgers_cfg, rules_cfg, output_cfg = cfgs

    if args.values:
        # 配置有问题时先把配置报告打出来 —— 拿着一份坏配置去枚举取值，
        # 看到的东西没有意义。
        if doc.bad:
            print(doc.render())
            return 2
        wanted = [l for l in ledgers_cfg.get("ledgers", [])
                  if isinstance(l, dict) and l.get("id") == args.values]
        if not wanted:
            ids = [l.get("id") for l in ledgers_cfg.get("ledgers", [])
                   if isinstance(l, dict)]
            print(f"❌ ledgers.json 里没有 id={args.values!r} 的台账。"
                  f"现有：{', '.join(str(i) for i in ids)}", file=sys.stderr)
            return 2
        return list_values(wanted[0], rules_cfg)

    if not args.validate_config:
        enabled = [l for l in ledgers_cfg.get("ledgers", [])
                   if isinstance(l, dict) and l.get("enabled")]
        # 🔴 只有真的有腾讯文档台账时才查腾讯凭证。
        #    纯飞书用户没有、也不该有 TENCENT_DOCS_TOKEN，
        #    而以前这里无条件查，会给他一条红色的「凭证缺失」——
        #    自检报红是最强的「别用」信号，业务会卡在一个和他无关的东西上。
        needs_tencent = needs_tencent_token(enabled)
        tencent_ok = check_credential(doc) if needs_tencent else True
        if not needs_tencent:
            doc.add(OK, "腾讯文档凭证：本机没有腾讯文档台账，无需配置")
        lark_ok: dict[str, bool] = {}   # profile -> 是否可用，避免同一 profile 重复查
        wecom_ok: dict[str, bool] = {}  # url -> 是否可读，同一份文档只探一次
        for ledger in ledgers_cfg.get("ledgers", []):
            if not ledger.get("enabled"):
                continue
            source = ledger.get("source", "tencent_mcp")
            if source == "tencent_mcp":
                if not tencent_ok:
                    continue  # 已经在 check_credential 报过一次，不用再报
            elif source == "lark_cli":
                profile = ledger.get("profile", "sentinel")
                if profile not in lark_ok:
                    lark_ok[profile] = check_lark_credential(doc, ledger)
                if not lark_ok[profile]:
                    continue
            elif source == "wecom_doc":
                url = ledger.get("url", "")
                if url not in wecom_ok:
                    wecom_ok[url] = check_wecom_doc_credential(doc, ledger)
                if not wecom_ok[url]:
                    continue
            check_ledger(doc, ledger, rules_cfg)
        # 放在台账都读完之后：降级标志到这时才是这一趟的真实结论。
        check_tls(doc)
        check_reminders(doc, output_cfg)
        check_wecom(doc, output_cfg)
        check_alert(doc, output_cfg)
        check_health(doc)
        check_state(doc)

    print(doc.render())
    return 1 if doc.bad else 0


if __name__ == "__main__":
    sys.exit(main())
