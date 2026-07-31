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

import core
import qqdoc
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
        for n in off:
            doc.add(WARN, f"节点「{n.get('name')}」未启用（不会产生任何催办）",
                    n.get("_禁用原因") or "配置里 enabled=false")
        # 阈值与复提醒必填
        for n in on:
            thr = (n.get("threshold") or {}).get("days")
            rep = n.get("repeat") or {}
            if not thr:
                doc.add(BAD, f"节点「{n.get('name')}」缺 threshold.days")
            if not (rep.get("days") or rep.get("workdays")):
                doc.add(BAD, f"节点「{n.get('name')}」缺 repeat（复提醒间隔）",
                        "不允许默认成「只催一次」")

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
            rep = n.get("repeat") or {}
            every = (f"每 {rep['workdays']} 个工作日" if rep.get("workdays")
                     else f"每 {rep.get('days')} 天" if rep.get("days") else "—")
            mark = "" if n.get("enabled") else "（未启用）"
            rows.append(
                f"{n.get('name', '?'):<10} 在本节点满 {thr.get('days')} 天 → "
                f"第 {core.first_reminder_day(n)} 天首次提醒（显示「超期 1 天」），"
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
    if wd_cfg.get("exclude_holidays"):
        try:
            hol = core.load_json(hol_path, "节假日表") if hol_path.exists() else None
        except LedgerError as e:
            doc.add(BAD, "节假日表损坏", f"{e}\n每日运行会以启动阶段故障退出（退出码 2）")
            return ledgers_cfg, rules_cfg, output_cfg
        wc = core.WorkdayCalc(wd_cfg, hol)
        if wc.holiday_warning:
            doc.add(WARN, "节假日表需要处理", wc.holiday_warning)
        else:
            doc.add(OK, f"节假日表就绪（{len(wc.holidays)} 个假日）")
    else:
        doc.add(OK, "工作日口径：仅排除周末（未启用节假日表）")

    return ledgers_cfg, rules_cfg, output_cfg


def check_credential(doc: Doc) -> bool:
    try:
        qqdoc.load_token()
    except LedgerError as e:
        doc.add(BAD, "腾讯文档凭证缺失", f"{e}\n"
                "🔴 注意：凭证问题会表现成「今天没有超时单」，不是正常状态")
        return False
    doc.add(OK, "腾讯文档凭证已配置", "（内容不打印）")
    return True


def check_ledger(doc: Doc, ledger: dict, rules_cfg: dict) -> None:
    name = ledger.get("name")
    try:
        fp_before = qqdoc.file_fingerprint(ledger["file_id"])
    except LedgerError as e:
        doc.add(BAD, f"台账「{name}」不可访问",
                f"{e}\n🔴 这是故障，不是「今天没有超时单」")
        return
    doc.add(OK, f"台账「{name}」可访问",
            f"最后修改：{fp_before.get('last_modify_name')} / {fp_before.get('last_modify_time')}")

    try:
        sheet = qqdoc.read_sheet(ledger["file_id"], ledger["sheet_id"])
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
                    f"台账 owner：{fp_before.get('last_modify_name')}")

    # 只读性验证：读了一整轮，确认没动过
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
                "请在 <hermes>/.env 里配 FOLLOWUP_WECOM_WEBHOOK。\n"
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
                "请在 <hermes>/.env 配 FOLLOWUP_ALERT_TARGET（形如 telegram:<chat_id>）。\n"
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


def check_state(doc: Doc) -> None:
    se = core.read_state("stage_entered.json")
    fs = core.read_state("followup_state.json")
    sh = core.read_state("stage_history.json")
    if core.STATE_DAMAGE:
        for d in core.STATE_DAMAGE:
            doc.add(BAD, "状态文件损坏",
                    d["message"] + "\n自检只报告不修复 —— 改名保留由下一次真实运行做")
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


def main() -> int:
    ap = argparse.ArgumentParser(description="项目跟进精灵 · 自检")
    ap.add_argument("--validate-config", action="store_true",
                    help="只查配置，不联网、不读台账")
    args = ap.parse_args()

    # 自检是纯诊断工具，任何情况下都不写状态、不改坏文件、不发消息。
    # 这里显式声明出来，而不是靠「它现在恰好没有写入代码」。
    core.set_read_only(True)

    doc = Doc()
    cfgs = check_configs(doc)
    if cfgs is None:
        print(doc.render())
        return 2
    ledgers_cfg, rules_cfg, output_cfg = cfgs

    if not args.validate_config:
        if check_credential(doc):
            for ledger in ledgers_cfg.get("ledgers", []):
                if ledger.get("enabled"):
                    check_ledger(doc, ledger, rules_cfg)
        check_reminders(doc, output_cfg)
        check_wecom(doc, output_cfg)
        check_alert(doc, output_cfg)
        check_health(doc)
        check_state(doc)

    print(doc.render())
    return 1 if doc.bad else 0


if __name__ == "__main__":
    sys.exit(main())
