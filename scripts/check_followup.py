#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目跟进精灵 —— 每日催办主脚本（零第三方依赖，仅标准库）。

用法：
  python3 check_followup.py                     # 正常跑一次
  python3 check_followup.py --dry-run           # 不发不写（试跑）
  python3 check_followup.py --verify-readonly   # 顺带做只读性验证
  python3 check_followup.py --today 2026-08-07  # 模拟某一天（不发不写）

设计：纯规则判定，零 LLM 零 token。cron 用 --no-agent 直接投递本脚本的 stdout。
      清单为空则完全静默（无事不发是降噪的一部分）。

铁律：台账只读。唯一的持久化写入是 <运行时目录>/followup/state/ 下的本地文件。

═══════════════════════════════════════════════════════════════════════
退出码（Hermes 按它判定任务成败，见 cron/scheduler.py 的 no_agent 分支）：
  0  正常。含「今天没有超时单」「另一次运行进行中已跳过」
  1  主任务失败：取数 / 入口断言 / 主通道投递 / 只读性验证
  2  启动阶段故障：配置、无启用台账、节假日表损坏、状态目录不可写、参数错

🔴 绝不能在推送失败时返回 0 —— 那会让 Hermes 显示成功、让项目被记为
   已通知、让业务静默 7 天收不到催办，而这一切看起来都像「今天没事」。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations  # 兼容 Python 3.9（macOS 自带版本）

import argparse
import shutil
import subprocess
import sys
from datetime import date

import core
import qqdoc
from core import LedgerError


# ══════════════════════════════════════════════════════════════════════
# 故障告警
# ══════════════════════════════════════════════════════════════════════

def _hermes_bin() -> str | None:
    """
    找到 hermes 可执行文件。

    不能只靠 PATH：cron 由 launchd 托管的 gateway 派生，它的 PATH 与登录 shell
    不同。本机实测 gateway 的 PATH 恰好含 ~/.local/bin，但那是**这台机器的偶然**，
    业务那台不保证 —— 找不到就得报错，不能静默地什么也没发。
    """
    found = shutil.which("hermes")
    if found:
        return found
    for cand in (core.hermes_home() / "bin" / "hermes",
                 core.hermes_home().parent / ".local" / "bin" / "hermes"):
        if cand.exists():
            return str(cand)
    return None


def alert(text: str, output_cfg: dict, *, stream=None) -> tuple[bool, str]:
    """
    故障告警走 telegram。返回 (成功?, 说明)。

    telegram 已从「日常投递」降级为「只在出事时响一声」——企微是唯一的
    内容通道，它挂了就是完全静默，而「完全静默」和「今天没有超时单」
    长得一模一样，这是全方案最隐蔽的失败模式。

    🔴 旧实现三处问题，全在这里修掉：
       ①不看 returncode，hermes send 失败也当成功
       ②目标 telegram:<chat_id> 硬编码在代码里，迁到业务电脑要改代码
       ③找不到 hermes 时被 except 吞掉，一声不响

    **「告警发出去了」不等于「主任务成功」**：本函数的结果只记进 health，
    绝不参与主任务退出码的计算。
    """
    out = stream or sys.stderr
    cfg = (output_cfg or {}).get("alert") or {}
    if not cfg.get("enabled", True):
        return False, "告警通道在 output.json 里被关闭了"

    target = core.read_env("FOLLOWUP_ALERT_TARGET")
    if not target:
        detail = ("未配置 FOLLOWUP_ALERT_TARGET（<运行时目录>/.env），"
                  "告警降级为只写 health.json + stderr")
        print(f"🔴 故障告警未发出：{detail}\n   原本要告警的内容：{text[:200]}",
              file=out)
        return False, detail

    exe = _hermes_bin()
    if not exe:
        detail = "PATH 里找不到 hermes 可执行文件，无法发送告警"
        print(f"🔴 故障告警未发出：{detail}\n   原本要告警的内容：{text[:200]}",
              file=out)
        return False, detail

    timeout = int(cfg.get("timeout_seconds") or 30)
    try:
        r = subprocess.run(
            [exe, "send", "-t", target, "-q", text],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        detail = f"hermes send 超时（{timeout}s）"
    except Exception as e:  # noqa: BLE001 —— 告警失败不能让主流程崩
        detail = f"{type(e).__name__}: {e}"
    else:
        if r.returncode == 0:
            return True, "ok"
        detail = (f"hermes send 退出码 {r.returncode}"
                  + (f"：{(r.stderr or '').strip()[:300]}" if r.stderr else ""))

    print(f"🔴 故障告警发送失败：{detail}\n   原本要告警的内容：{text[:200]}", file=out)
    return False, detail


def fail(stage: str, reason: str, output_cfg: dict, *, code: int,
         real_run: bool) -> int:
    """
    统一故障出口。**每一条失败路径都必须经过这里**，否则迟早有一条忘了告警。

    做四件事：stderr 说清「这不是没有超时单」→ 记 health → 发告警 → 返回非零。
    """
    print(f"❌ {stage}失败（这不是「今天没有超时单」，这是故障）：\n   {reason}",
          file=sys.stderr)
    ok, detail = (False, "试跑模式不发告警")
    if real_run:
        ok, detail = alert(f"🔴 项目跟进精灵 · {stage}失败\n\n{reason}", output_cfg)
        h = core.read_health()
        core.update_health(
            last_run_at=core.now_iso(),
            last_failure={"at": core.now_iso(), "stage": stage, "reason": reason[:500]},
            alert_ok=ok,
            alert_detail=detail,
            consecutive_failures=int(h.get("consecutive_failures") or 0) + 1,
        )
    return code


# ══════════════════════════════════════════════════════════════════════
# 渲染
# ══════════════════════════════════════════════════════════════════════

def _fmt_share(n: int, total: int) -> str:
    """把占比说成人话：一半 / 三分之一 / 百分比。"""
    if total <= 0:
        return ""
    r = n / total
    if abs(r - 0.5) <= 0.04:
        return "占一半"
    if abs(r - 1 / 3) <= 0.03:
        return "占三分之一"
    if abs(r - 0.25) <= 0.03:
        return "占四分之一"
    return f"占 {round(r * 100)}%"


def group_items(rep: core.Report, output_cfg: dict) -> list[dict]:
    """
    把待催清单分组排序，**两套渲染共用这一份**。

    分开各写一遍的话，迟早只改了企微那边、终端那边还是旧格式。

    业务 2026-07-31 定的规则：
      · 组内按**超期天数**升序 —— 短的在前，久的在后
      · 只在指定阶段（默认「待收资」）插「超2个月 可考虑终止」分割线
      · 分割线插在第一个**超期** ≥60 天的项目之前：线以上还救得回来，线以下该做决定

    排序与分割线都用 overdue_days 而不是 stalled_days（业务 2026-07-31 确认）：
    同一组内两者只差一个常数、结果一样；但**一旦某组混进不同阈值的节点，
    只有 overdue_days 是对的**。而且分割线的判据与显示的数字一致，
    业务不会看到「显示超期 55 天却出现了超2个月」这种自相矛盾。
    """
    hint = (output_cfg or {}).get("terminal_hint") or {}
    hint_on = bool(hint.get("enabled"))
    hint_stages = hint.get("stages")
    hint_days = int(hint.get("days") or 60)
    hint_text = hint.get("text") or "可考虑终止"

    by_node: dict[str, list[core.Item]] = {}
    for it in rep.due:
        by_node.setdefault(it.node_name, []).append(it)

    groups = []
    # 按 node_name 排序 = 按 ①②③④ 的编号顺序，与流程顺序一致
    for node_name in sorted(by_node):
        items = sorted(by_node[node_name], key=lambda x: x.overdue_days)
        stage = items[0].stage
        cut = None
        # stages 为空数组 = 所有阶段都插；未配置该键则按「所有阶段」处理
        applies = hint_on and (not hint_stages or stage in hint_stages)
        if applies:
            for i, it in enumerate(items):
                if it.overdue_days >= hint_days:
                    cut = i
                    break
        groups.append({
            "stage": stage, "node_name": node_name, "items": items,
            "cut": cut, "hint_text": hint_text,
        })
    return groups


def _headline(rep: core.Report, groups: list[dict]) -> list[str]:
    """总任务量 + 积压最重。两套渲染共用。"""
    lines = [f"总任务量：{rep.total_rows} 个项目里，{len(rep.due)} 个要催办"]
    # 只有一个阶段时不说「积压最重」——那必然是 100%，等于没说
    if len(groups) > 1:
        worst = max(groups, key=lambda g: len(g["items"]))
        share = _fmt_share(len(worst["items"]), len(rep.due))
        lines.append(
            f"积压最重：{worst['stage']} {len(worst['items'])} 项"
            + (f"（{share}）" if share else "")
        )
    return lines


def _problems(rep: core.Report) -> list[str]:
    """始终显示的故障与数据质量问题。绝不藏进 --verbose。"""
    out = []
    if rep.accounted != rep.total_rows:
        out.append(
            f"各项之和 {rep.accounted} ≠ 项目总数 {rep.total_rows}，有行去向不明，请检查"
        )
    out.extend(rep.warnings)
    return out


def render(reports: list[core.Report], today: date, write_on: bool,
           output_cfg: dict, verbose: bool = False,
           run_warnings: list[str] | None = None) -> str:
    """
    终端 / hermes 日志用的纯文本渲染。演示走的就是这一份，所以它和企微那份
    保持同一结构（升序、编号、分割线位置），只在标记上不同。

    🔴 故障绝不藏：取数失败、字段缺失、数据质量问题、各项之和 ≠ 总数，
    一律照常显示。把故障也藏进 --verbose，就又回到「故障伪装成今天没事」
    那个最隐蔽的失败模式了。
    """
    lines: list[str] = [f"🧚 项目跟进精灵 · {today.isoformat()}"]

    for w in (run_warnings or []):
        lines.append(f"⚠️ {w}")

    for rep in reports:
        groups = group_items(rep, output_cfg)
        lines.append("")
        lines.append(f"——{rep.line_label}——")
        lines.extend(_headline(rep, groups))

        for g in groups:
            lines.append("")
            lines.append(f"【{g['stage']}】{len(g['items'])} 项")
            for i, it in enumerate(g["items"]):
                if g["cut"] is not None and i == g["cut"]:
                    lines.append(f"------- {g['hint_text']} -------")
                lines.append(f"{i + 1}、{it.name} — 超期 {it.overdue_days} 天")

        problems = _problems(rep)
        if problems:
            lines.append("")
            for p in problems:
                lines.append(f"⚠️ {p}")

        # 禁用的节点始终报一行 —— 一个悄悄不跑的规则比一个跑错的规则更难发现
        for d in rep.disabled_nodes:
            lines.append(f"⏸ {d.split('：')[0]}：未启用，不会产生催办")

        if not verbose:
            continue

        lines.append("")
        lines.append("─" * 46)
        lines.append("◆ 运行摘要（--verbose）")
        parts = [
            f"今天催 {len(rep.due)}",
            f"超期但未到复提醒间隔 {len(rep.overdue_muted)}",
            f"终止 {len(rep.terminal)}",
            f"暂缓 {len(rep.paused)}",
            f"范围外 {rep.out_of_scope}",
            f"未超期 {rep.not_overdue}",
            f"无待催节点 {rep.no_node}",
        ]
        lines.append("   " + " ｜ ".join(parts))
        if rep.out_of_scope_detail:
            detail = "、".join(
                f"{k} {v} 行" for k, v in sorted(rep.out_of_scope_detail.items())
            )
            lines.append(f"   范围过滤明细：{detail}")
        for d in rep.disabled_nodes:
            lines.append(f"   ⏸ {d}")
        for n in rep.notices:
            lines.append(f"   ℹ️ {n}")
        if rep.review_hints:
            lines.append(
                f"   📋 待复核 {len(rep.review_hints)} 条（底纹与规则判定不一致）："
            )
            for h in rep.review_hints[:5]:
                lines.append(f"      {h}")
            if len(rep.review_hints) > 5:
                lines.append(f"      … 另有 {len(rep.review_hints) - 5} 条")
        if not write_on:
            lines.append("   🔒 演练模式：reminders.write=false，不会创建任何提醒事项")

    return "\n".join(lines)


def render_wecom(reports: list[core.Report], today: date, output_cfg: dict,
                 run_warnings: list[str] | None = None) -> str:
    """
    企微专用渲染（markdown_v2）。

    与 render() 分开是刻意的：stdout 那份进 hermes 日志，排查时要看原始信息，
    塞 ** # - 这些标记只会碍事；企微客户端会渲染，用得上标题分级。

    ═══════════════════════════════════════════════════════════════════
    样式（业务 2026-07-31 定稿）：
      · 只做标题分级 —— 业务线 ##、阶段 ###，真的比正文大，即她要的「大一号字」
      · **条目一律不加样式**：不加粗、不引用、不斜体。业务明确否掉了
        「抬高其他项来反衬」的做法
      · 「小一号字」做不到 —— 官方文档原文：markdown_v2 不支持字体颜色。
        旧版 markdown 有颜色但不支持列表，会退回她投诉过的「一堵墙」

    两个刻意的语法选择：
      1. 序号用「1、」而不是「1.」—— markdown 的有序列表被分割线打断后，
         后半段可能被渲染器重新从 1 编号，业务会看到两个「1」。
         用顿号就是纯文本，不进列表解析，零渲染风险。
      2. 分割线用「------- 文字 -------」整行，不用单独的 `---`。
         紧跟在文字后面的一行 `---` 在 markdown 里会把上一行变成二级标题。
    ═══════════════════════════════════════════════════════════════════
    """
    L: list[str] = [f"# 🧚 项目跟进精灵 · {today.isoformat()}"]

    for w in (run_warnings or []):
        L.append("")
        L.append(f"⚠️ {w}")

    for rep in reports:
        groups = group_items(rep, output_cfg)
        L.append("")
        L.append(f"## {rep.line_label}")
        L.extend(_headline(rep, groups))

        for g in groups:
            L.append("")
            L.append(f"### 【{g['stage']}】{len(g['items'])} 项")
            for i, it in enumerate(g["items"]):
                if g["cut"] is not None and i == g["cut"]:
                    L.append(f"------- {g['hint_text']} -------")
                L.append(f"{i + 1}、{it.name} — 超期 {it.overdue_days} 天")

        problems = _problems(rep)
        if problems:
            L.append("")
            L.append("### ⚠️ 需要注意")
            for p in problems:
                L.append(f"- {p}")

        for d in rep.disabled_nodes:
            L.append("")
            L.append(f"> ⏸ {d.split('：')[0]}：未启用，不会产生催办")

    return "\n".join(L)


# ══════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="项目跟进精灵 · 每日催办")
    ap.add_argument("--dry-run", action="store_true", help="不发不写（试跑）")
    ap.add_argument("--verify-readonly", action="store_true",
                    help="运行前后核对台账最后修改时间，验证只读")
    ap.add_argument("--json", action="store_true",
                    help="输出 JSON 而非文本。诊断用，不发不写")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="附上运行摘要、待复核清单、运维提示等调试信息"
                         "（故障与数据质量问题默认就会显示，不需要这个开关）")
    ap.add_argument("--today", metavar="YYYY-MM-DD",
                    help="把「今天」当作指定日期（回归测试与复现问题用）。"
                         "默认不发不写，是纯模拟")
    ap.add_argument("--force-push", action="store_true",
                    help="配合 --today 时仍然真发企微并写状态（补跑用）。"
                         "日常不要加——回归测试误发到业务群是不可逆的")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # ══ 只读闸门 ═══════════════════════════════════════════════════════
    # 真实运行 = 会真发消息、会写状态。三个诊断开关一律排除在外。
    #
    # 🔴 必须算在**任何一条可能失败的语句之前**。旧实现把它放在读完配置、
    #    解析完日期之后，而那之前已经有一条 fail(real_run=not args.dry_run)
    #    的岔路 —— 于是 `--json` / `--today` 撞上坏配置时，会真发 telegram、
    #    真创建 health.json。一个用来「看看情况」的开关，动了现场。
    #
    #    从这里往下，所有成功与失败路径共用同一个 real_run，没有第二个表达式。
    real_run = (not args.dry_run) and (not args.json) and \
               (not args.today or args.force_push)
    core.set_read_only(not real_run)   # core 层兜底闸门，见 core.py 的说明

    # 配置尽早读一次：故障告警需要它，而故障可能发生在任何一步
    try:
        ledgers_cfg, rules_cfg, output_cfg = core.load_configs()
    except LedgerError as e:
        # 这一步失败时 output_cfg 还没有，但告警目标在 .env 里，仍然发得出去
        return fail("配置读取", str(e), {}, code=2, real_run=real_run)

    # 顶层是对象只是及格线，关键字段的类型也得对。
    # 🔴 必须紧跟在 load_configs 之后 —— 下面每一处配置读取都是 .get() 链，
    #    `"wecom_webhook": "https://…"` 这种手误会让主通道检查自己先炸成
    #    AttributeError，而那种崩法指向的是崩溃现场，不是病根。
    cfg_errs = core.validate_configs(ledgers_cfg, rules_cfg, output_cfg)
    if cfg_errs:
        return fail("配置校验", "\n".join(f"· {e}" for e in cfg_errs),
                    output_cfg, code=2, real_run=real_run)

    if args.today:
        try:
            today = date.fromisoformat(args.today)
        except ValueError:
            # 参数写错走 real_run=False：操作者当场就看得到，
            # 不该为一次手误往 telegram 发告警。
            return fail("参数校验",
                        f"--today 格式应为 YYYY-MM-DD，收到 {args.today!r}",
                        output_cfg, code=2, real_run=False)
    else:
        today = date.today()

    # 🔴 未来日期 + 强推 = 会写出未来日期的快照，把节点计时基准彻底污染。直接拒绝。
    if args.force_push and args.today and today > date.today():
        return fail(
            "参数校验",
            f"--today {today} 是未来日期，不允许与 --force-push 同用。"
            f"那会写出未来日期的快照，之后所有停滞天数都会算错。",
            output_cfg, code=2, real_run=False,
        )

    # ── 启动阶段自检：这些都必须在跑判定之前失败，不能等推送完了才炸 ──
    if real_run:
        try:
            core.ensure_state_dir()
        except LedgerError as e:
            return fail("状态目录检查", str(e), output_cfg, code=2, real_run=True)

    primary = ((output_cfg.get("notify") or {}).get("primary") or "wecom_webhook")
    if primary not in ("wecom_webhook", "stdout"):
        return fail("配置校验",
                    f"notify.primary＝{primary!r} 不认识，只能是 wecom_webhook 或 stdout",
                    output_cfg, code=2, real_run=real_run)
    if primary == "wecom_webhook" and not (
            (output_cfg.get("wecom_webhook") or {}).get("enabled")):
        return fail(
            "配置校验",
            "notify.primary 指向 wecom_webhook，但该通道 enabled=false。"
            "这样永远拿不到投递凭证，会天天重推同一批。"
            "要么启用它，要么把 primary 改成 stdout。",
            output_cfg, code=2, real_run=real_run,
        )

    hol_path = core.config_dir() / "holidays.json"
    try:
        holidays = core.load_json(hol_path, "节假日表") if hol_path.exists() else None
    except LedgerError as e:
        # 旧实现这行在 try 外面，节假日表写坏了会抛裸 traceback
        return fail("节假日表读取", str(e), output_cfg, code=2, real_run=real_run)

    active = [l for l in ledgers_cfg.get("ledgers", []) if l.get("enabled")]
    if not active:
        return fail("配置校验", "config/ledgers.json 里没有启用的台账",
                    output_cfg, code=2, real_run=real_run)

    # ── 运行锁：两次同时跑会互相覆盖状态、重复推送 ──
    lock_path = None
    lock_token = ""
    run_warnings: list[str] = []
    if real_run:
        try:
            lock_path, lock_token, steal = core.acquire_lock()
            if steal:
                run_warnings.append(steal)
                alert(f"⚠️ 项目跟进精灵：{steal}", output_cfg)
        except core.LockBusy as e:
            # 不是故障，安静退出（退出码 0）。Hermes 那边表现为一次静默运行。
            # 但「上一次跑太久所以让路」这种情况要告警 —— 真卡死的话每天都会跳，
            # 而「每天静默跳过」和「每天没有超时单」长得一模一样。
            msg = str(e)
            print(f"⏭ 本次跳过：{msg}", file=sys.stderr)
            if "还没结束" in msg:
                alert(f"⚠️ 项目跟进精灵：{msg}", output_cfg)
            return 0

    try:
        return _run(args, today, real_run, primary, output_cfg, rules_cfg,
                    active, holidays, run_warnings)
    finally:
        if lock_path:
            core.release_lock(lock_path, lock_token)


def _run(args, today: date, real_run: bool, primary: str, output_cfg: dict,
         rules_cfg: dict, active: list, holidays, run_warnings: list[str]) -> int:
    write_on = core.reminders_write_enabled(output_cfg)
    workday = core.WorkdayCalc(rules_cfg.get("workday") or {}, holidays,
                               core.nodes_using_workdays(rules_cfg))

    stage_entered = core.read_state("stage_entered.json")
    stage_history = core.read_state("stage_history.json")
    followup_state = core.read_state("followup_state.json")
    # 🔴 health.json 自己也要在这里读一次（返回值用不上，只为它的副作用）。
    #    以前它第一次被碰到是在后面的 update_health() 内部，而 update_health()
    #    把所有异常吞成一行 stderr —— 不登记 STATE_DAMAGE、不隔离、不告警。
    #    结果是：健康记录自己骨折了，反而是最没人发现的那种坏法，还会被静默重建，
    #    连「上次成功是什么时候」都一起丢掉。放到这里读，它就和其余状态文件
    #    共用下面这套「登记 → 隔离保留 → 告警 → last_recovery」的机制。
    core.read_health()
    # read_state 可能已经登记了「状态文件损坏」。这是重大降级，不能只在报告里
    # 显示就算了 —— stage_entered 损坏意味着全部项目按「最新进展日期」重新初始化，
    # 停滞天数集体失真，必须有人立刻知道。
    if core.STATE_DAMAGE:
        if real_run:
            # 先改名保留，再取 message —— quarantine 会把「已保留为 xxx」补进去。
            # 诊断模式下这一步被闸门挡住，坏文件原样留在现场。
            kept = core.quarantine_damaged()
            core.update_health(
                last_recovery={"at": core.now_iso(), "files": kept,
                               "damaged": [d["name"] for d in core.STATE_DAMAGE]},
            )
        run_warnings.extend(d["message"] for d in core.STATE_DAMAGE)
        if real_run:
            ok, why = alert(
                "🔴 项目跟进精灵：状态文件损坏\n\n"
                + "\n".join(f"· {d['message']}" for d in core.STATE_DAMAGE)
                + "\n\n坏文件已改名保留，未删除。停滞天数本次可能失真。",
                output_cfg)
            core.update_health(alert_ok=ok, alert_detail=why)

    reports: list[core.Report] = []
    snapshots: dict[str, dict] = {}
    failures: list[str] = []
    baselines: dict[str, dict] = {}

    for ledger in active:
        lid = ledger.get("id")
        # 逐份台账处理，单份失败不影响其他份
        try:
            if args.verify_readonly:
                baselines[lid] = qqdoc.file_fingerprint(ledger["file_id"])

            ruleset = (rules_cfg.get("rulesets") or {}).get(ledger.get("ruleset"))
            if not ruleset:
                raise LedgerError(
                    f"rules.json 里找不到规则集 {ledger.get('ruleset')!r}"
                )
            last_snapshot = core.read_state(f"snapshot_last_{lid}.json")
            rep, snap = core.evaluate_ledger(
                ledger, ruleset, workday, today,
                stage_entered, followup_state, last_snapshot, stage_history,
            )
            reports.append(rep)
            snapshots[lid] = snap
        except LedgerError as e:
            failures.append(f"{ledger.get('name')}：{e}")
        except Exception as e:  # 未预期的错误也要归到这份台账，不能整体崩
            failures.append(f"{ledger.get('name')}：未预期错误 {type(e).__name__}: {e}")

    if failures:
        detail = "\n".join(f"· {f}" for f in failures)
        print("❌ 以下台账处理失败（这不是「今天没有超时单」，是故障）：",
              file=sys.stderr)
        for f in failures:
            print(f"   - {f}", file=sys.stderr)
        if real_run:
            ok, why = alert(f"🔴 项目跟进精灵取数失败：\n{detail}", output_cfg)
            h = core.read_health()
            core.update_health(
                last_run_at=core.now_iso(),
                last_failure={"at": core.now_iso(), "stage": "取数/判定",
                              "reason": detail[:500]},
                alert_ok=ok, alert_detail=why,
                consecutive_failures=int(h.get("consecutive_failures") or 0) + 1,
            )
        if not reports:
            return 1
    elif real_run:
        core.update_health(last_fetch_ok=core.now_iso())

    total_due = sum(len(r.due) for r in reports)
    read_count = sum(r.total_rows for r in reports)
    muted_count = sum(len(r.overdue_muted) for r in reports)
    dq_warnings = sum(len(_problems(r)) for r in reports)

    # ── 主通道投递 ──
    # 提到输出之前：投递不依赖渲染出来的文本，先做掉才能把「发没发出去」
    # 一并写进下面那段输出里。企微那边一个字都没变，只是调用顺序前移。
    delivered, delivery = _deliver(args, reports, today, output_cfg, primary,
                                   total_due, real_run)

    if real_run:
        core.update_health(last_run_summary={
            "at": core.now_iso(),
            "read": read_count,
            "due": total_due,
            "muted": muted_count,
            "messages": delivery.get("total"),
            "delivery": delivery.get("summary"),
            "data_quality_warnings": dq_warnings,
        })

    # ── 输出 ──
    if args.json:
        _print_json(reports, today, write_on, failures)
    elif args.verbose or total_due or failures or run_warnings or any(
        r.warnings or r.accounted != r.total_rows for r in reports
    ):
        # 有待催、有故障、有数据质量问题时都要输出。
        # 只有「一切正常且今天没有超时单」才完全静默 —— 但 --verbose 是
        # 诊断开关，它下面的静默毫无用处（排查时最想看的正是「今天为什么没有」）。
        text = render(reports, today, write_on, output_cfg,
                      verbose=args.verbose, run_warnings=run_warnings)
        if real_run:
            # 清单末尾补一行投递结果：清单本身不能证明它发出去了。
            text = f"{text}\n投递：{delivery['summary']}"
        print(text)
    elif real_run:
        # 🔴 真实运行至少留一行回执。以前这里是完全静默，于是「今天没有超时单」
        #    和「今天压根没跑起来」在日志上长得一模一样 —— 业务手动点一下，
        #    看到的就是毫无反应。企微该不发还是不发，变的只是本地这行字。
        print(f"✅ 检查完成：读取 {read_count} 项，待催 {total_due} 项，"
              f"静默期 {muted_count} 项，{delivery['summary']}。")
    # else: 诊断模式（--dry-run / 不带 --force-push 的 --today）维持完全静默

    # ── 写入本地状态 ──
    # 🔴 两级拆分，这是本次修复的核心：
    #    · 事实观测（节点进入时间、历史、快照、首次超期日）→ 每次真实运行都写
    #    · 投递凭证（last_notified）→ 只在主通道完整送达时才写
    #    混在一起写，就会出现「业务没收到，系统却认为已通知」的静默漏催。
    exit_code = 0
    if real_run:
        if delivered is True:
            for it in (i for r in reports for i in r.due):
                followup_state.setdefault(it.state_key, {})["last_notified"] = \
                    core.iso(today)
        elif delivered is False:
            exit_code = 1
            print("🔴 主通道未完整送达，本次不记「已通知」——"
                  "下次运行会把整批重发。重复消息业务能识别，静默漏催她发现不了。",
                  file=sys.stderr)

        core.write_state("stage_entered.json", stage_entered)
        core.write_state("stage_history.json", stage_history)
        core.write_state("followup_state.json", followup_state)
        for lid, snap in snapshots.items():
            core.write_state(f"snapshot_last_{lid}.json", snap)
            # 每日快照只增不删，永久保留
            core.write_state(f"snapshot_{today:%Y%m%d}_{lid}.json", snap)

        # 提醒事项同步（写入开关在 reminders_sync 内部把关）
        try:
            import reminders_sync
            reminders_sync.sync(
                [i for r in reports for i in r.due], output_cfg, today,
                stream=sys.stdout if (args.verbose and not args.json) else sys.stderr,
            )
        except Exception as e:
            print(f"⚠️ 提醒事项同步失败（不影响判定结果）：{e}", file=sys.stderr)

        if failures:
            exit_code = exit_code or 1
        if exit_code == 0 and not run_warnings:
            core.update_health(
                last_run_at=core.now_iso(),
                last_full_success=core.now_iso(),
                consecutive_failures=0,
            )
        else:
            core.update_health(last_run_at=core.now_iso())
    elif failures:
        exit_code = 1

    # ── 只读性验证 ──
    if args.verify_readonly:
        if _verify_readonly(active, baselines) is False:
            exit_code = 1

    return exit_code


def _deliver(args, reports, today, output_cfg, primary, total_due, real_run):
    """
    走主通道投递。返回 (delivered, detail)。

    delivered 的语义与此前**完全一致**，调用方的判断一个字都不用改：
      True(完整送达) / False(失败) / None(无需投递)。
    None 的两种情形：今天没有待催（无事不发），或试跑模式被闸门拦下。
    这两种都**不构成送达凭证**，所以也不会提交 last_notified —— 但也不算故障。

    detail 是本次新增的展示信息，形状固定：
      {"channel", "attempted", "sent", "total", "summary"}
    只用于那行本地回执和 health.json 的 last_run_summary，**不参与任何判定**。
    """
    label = "企微" if primary == "wecom_webhook" else "stdout"

    if not total_due:
        return None, {"channel": primary, "attempted": False, "sent": 0,
                      "total": 0, "summary": f"{label}未发送"}

    if primary == "stdout":
        # 没有企微通道时的退路。stdout 打印即视为送达 —— 但这没有任何投递保证，
        # doctor 会就此提醒。
        if real_run:
            return True, {"channel": "stdout", "attempted": True, "sent": 1,
                          "total": 1, "summary": "stdout 已输出待催清单"}
        return None, {"channel": "stdout", "attempted": False, "sent": 0,
                      "total": 0, "summary": "stdout 试跑，未产生投递凭证"}

    try:
        import wecom_push
        res = wecom_push.push(
            render_wecom(reports, today, output_cfg),
            output_cfg, allowed=real_run, stream=sys.stderr,
        )
    except Exception as e:
        msg = f"企微推送异常 {type(e).__name__}: {e}"
        print(f"🔴 {msg}", file=sys.stderr)
        detail = {"channel": "wecom_webhook", "attempted": True, "sent": 0,
                  "total": 0, "summary": f"企微推送异常：{type(e).__name__}"}
        if real_run:
            ok, why = alert(
                f"🔴 项目跟进精灵：{msg}\n业务今天没收到催办清单"
                f"（{today.isoformat()}，本应推 {total_due} 项）", output_cfg)
            core.update_health(
                last_failure={"at": core.now_iso(), "stage": "企微推送",
                              "reason": msg},
                alert_ok=ok, alert_detail=why,
            )
            return False, detail
        return None, detail

    if not res.attempted:
        return None, {"channel": "wecom_webhook", "attempted": False, "sent": 0,
                      "total": res.total,
                      "summary": f"企微未发送（{res.skipped_reason}）"}
    if res.ok:
        if real_run:
            core.update_health(last_wecom_ok=core.now_iso())
        return True, {"channel": "wecom_webhook", "attempted": True,
                      "sent": res.sent, "total": res.total,
                      "summary": f"企微已送达 {res.sent}/{res.total} 条"}

    head = "部分失败" if res.partial else "全部失败"
    if real_run:
        body = (f"🔴 项目跟进精灵：企微推送{head}，业务今天收到的清单不完整\n"
                f"日期 {today.isoformat()}，本应推 {total_due} 项 / "
                f"{res.total} 条消息，成功 {res.sent} 条\n"
                + "\n".join(res.errors[:5]))
        ok, why = alert(body, output_cfg)
        h = core.read_health()
        core.update_health(
            last_failure={"at": core.now_iso(), "stage": "企微推送",
                          "reason": res.summary()[:500]},
            alert_ok=ok, alert_detail=why,
            consecutive_failures=int(h.get("consecutive_failures") or 0) + 1,
        )
    return False, {"channel": "wecom_webhook", "attempted": True,
                   "sent": res.sent, "total": res.total,
                   "summary": f"企微{head} {res.sent}/{res.total} 条"}


def _print_json(reports, today, write_on, failures) -> None:
    import json as _json
    payload = {
        "date": today.isoformat(),
        "reminders_write": write_on,
        "failures": failures,
        "ledgers": [
            {
                "id": r.ledger_id, "name": r.ledger_name, "line": r.line,
                "total_rows": r.total_rows,
                "due": [
                    {"key": i.key, "name": i.name, "node": i.node_name,
                     "stage": i.stage,
                     # 两个都给：overdue_days 是业务看到的，stalled_days 是
                     # 排查时要的「它在这个节点到底待了多久」。少给哪个都会
                     # 让下游得自己反推。
                     "stalled_days": i.stalled_days,
                     "allowance_days": i.allowance,
                     "overdue_days": i.overdue_days,
                     "clock_from": i.clock_from.isoformat(),
                     "clock_source": i.clock_source, "action": i.action}
                    for i in r.due
                ],
                "counts": {
                    "due": len(r.due), "overdue_muted": len(r.overdue_muted),
                    "terminal": len(r.terminal), "paused": len(r.paused),
                    "out_of_scope": r.out_of_scope, "not_overdue": r.not_overdue,
                    "no_node": r.no_node,
                },
                "disabled_nodes": r.disabled_nodes,
                "warnings": r.warnings,
                "notices": r.notices,
                "review_hints": r.review_hints,
            }
            for r in reports
        ],
    }
    print(_json.dumps(payload, ensure_ascii=False, indent=1))


def _verify_readonly(active: list, baselines: dict) -> bool:
    print("\n── 只读性验证 ──")
    bad = False
    for ledger in active:
        lid = ledger.get("id")
        if lid not in baselines:
            continue
        after = qqdoc.file_fingerprint(ledger["file_id"])
        before = baselines[lid]
        same = (before.get("last_modify_time") == after.get("last_modify_time")
                and before.get("last_modify_name") == after.get("last_modify_name"))
        print(f"  {ledger.get('name')}：{'✅ 未被修改' if same else '🔴 检测到台账被修改'}")
        print(f"     最后修改：{after.get('last_modify_name')} / "
              f"{after.get('last_modify_time')}")
        if not same:
            bad = True
    if bad:
        print("🔴 台账被修改了。请立即停用并检查白名单实现。", file=sys.stderr)
        return False
    return True


if __name__ == "__main__":
    sys.exit(main())
