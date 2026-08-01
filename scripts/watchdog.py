#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
外部存活监控 —— 回答一个别的检查都回答不了的问题：**任务根本没跑。**

用法：
  python3 watchdog.py              # 正常检查
  python3 watchdog.py --dry-run    # 只打印判定，不发不写
  python3 watchdog.py --self-test  # 强制走一次完整告警链路（**会真发消息**）

═══════════════════════════════════════════════════════════════════════
为什么必须是外部的

主脚本能报取数失败、推送失败、状态损坏 —— 但它得先跑起来才能报。
关机、休眠、gateway 没起来、cron 被误删，这几种失败**连一行 stderr
都不会产生**：没有进程，没有日志，没有异常。业务只会觉得「最近很安静」，
而那和「最近确实没有要催的」长得一模一样。

doctor 查得到（它看 health.json 的 last_full_success），但 doctor 要人手动跑。
把它挂进 hermes cron 又是循环依赖 —— gateway 没起来时，
「检查 gateway 起没起来」的那个任务同样不会执行。

所以：**执行者必须是 launchd**（macOS 自带，与 Hermes 无关），
告警走 `hermes send`（已核实 bot-token 平台不需要 gateway 在跑），
它也失败时退到本机通知，再失败就写日志并非零退出让 launchd 记下来。

🔴 本文件**刻意不 import core**。core 坏了、skill 包被 update 弄坏了、
   Python 版本不兼容 —— 这些正是主任务会失败的场景，监控器不能跟着一起哑掉。
   为此复制了约 30 行路径解析与 .env 读取。**这个重复是设计，不是疏漏**
   （同一理由下 qqdoc.load_token 与 core.read_env 也刻意不合并）。
   同理用 /usr/bin/python3 而不是 hermes 的 venv。
═══════════════════════════════════════════════════════════════════════

判定口径：**数错过了几次本该执行的 9:00，不数小时。**

小时阈值在这台机器上是错的工具：用户工作日不关机、周末可能关机。
设 48 小时会让周四的故障潜伏到周末，设 60 小时又会在周一早上误报
（周五跑完 → 周末关机 → 周一开机补跑，中间隔了 72 小时却一次都没缺勤）。
数班次并跳过周末，两种情况都对。
"""

from __future__ import annotations  # 兼容 Python 3.9（macOS 自带版本）

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

MARK = "🧚 项目跟进精灵 · 存活监控"

DEFAULTS = {
    "enabled": True,
    "schedule_hour": 9,
    "count_weekends": False,
    "missed_runs_before_alert": 2,
    "absolute_max_hours": 168,
    "repeat_alert_hours": 24,
    "missing_health_grace_hours": 36,
}


# ── 路径与配置（刻意不依赖 core，理由见文件头） ────────────────────────

def runtime_home() -> Path:
    """优先级必须与 core.hermes_home() 完全一致，否则两边看的不是同一个目录。"""
    env = os.environ.get("FOLLOWUP_HOME") or os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "hermes"
    return Path.home() / ".hermes"


def state_dir() -> Path:
    return runtime_home() / "followup" / "state"


def read_env(key: str) -> str | None:
    """从 .env 读一个键。**绝不打印值。**"""
    p = runtime_home() / ".env"
    if not p.exists():
        return None
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip("'\"") or None
    return None


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    p = runtime_home() / "followup" / "config" / "output.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        seg = data.get("watchdog")
        if isinstance(seg, dict):
            cfg.update({k: v for k, v in seg.items() if not k.startswith("_")})
    except Exception:  # noqa: BLE001
        # 配置读不出来就用默认值继续。
        # 监控器因为配置坏了而罢工，正好是最不该发生的事。
        pass
    return cfg


def parse_dt(s) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        d = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    return d if d.tzinfo else d.astimezone()


# ── 判定 ──────────────────────────────────────────────────────────────

def missed_runs(last_ok: datetime, now: datetime, hour: int,
                count_weekends: bool) -> int:
    """
    从 last_ok 之后到现在，有多少个「本该跑」的 <hour>:00 已经过去而没成功。

    从 last_ok 的**次日**开始数：last_ok 当天那一班已经跑成功了。
    """
    n = 0
    d = last_ok.date()
    limit = now.date()
    while d < limit or (d == limit and True):
        d += timedelta(days=1)
        if d > limit:
            break
        slot = datetime.combine(d, time(hour, 0)).replace(tzinfo=now.tzinfo)
        if slot > now:
            break
        if not count_weekends and d.weekday() >= 5:   # 5=周六 6=周日
            continue
        n += 1
    return n


def judge(health: dict, wd_state: dict, cfg: dict,
          now: datetime) -> tuple[bool, str, str]:
    """
    返回 `(要不要告警, 去重键, 人话说明)`。

    去重键让「同一个故障」在 repeat_alert_hours 内只响一次 ——
    一天查两次却报两次，人两天就把它静音了。
    """
    hour = int(cfg.get("schedule_hour") or 9)
    weekends = bool(cfg.get("count_weekends"))
    need = int(cfg.get("missed_runs_before_alert") or 2)
    max_h = int(cfg.get("absolute_max_hours") or 168)

    if not health:
        first_seen = parse_dt(wd_state.get("first_seen_missing")) or now
        hours = (now - first_seen).total_seconds() / 3600
        grace = int(cfg.get("missing_health_grace_hours") or 36)
        if hours < grace:
            return (False, "missing",
                    f"还没有健康记录，已观察 {hours:.1f} 小时"
                    f"（{grace} 小时内不报，刚装完属正常）")
        return (True, "missing",
                f"**从来没有过一次成功运行的记录。**\n"
                f"已经观察了 {hours / 24:.1f} 天，health.json 始终不存在。\n"
                f"常见原因：定时任务压根没注册、或者每次都在启动阶段就失败了。")

    last_ok = parse_dt(health.get("last_full_success"))
    if not last_ok:
        f = health.get("last_failure") or {}
        return (True, "never-succeeded",
                f"**有健康记录，但从来没有过一次完整成功。**\n"
                f"最近一次失败：[{f.get('stage', '?')}] {str(f.get('reason', ''))[:200]}")

    missed = missed_runs(last_ok, now, hour, weekends)
    hours = (now - last_ok).total_seconds() / 3600

    if missed >= need:
        return (True, f"stale:{health.get('last_full_success')}",
                f"**已经错过 {missed} 次本该执行的 {hour}:00。**\n"
                f"上次完整成功：{health.get('last_full_success')}\n"
                f"（{'含周末' if weekends else '周末不计'}）")

    if hours > max_h:
        return (True, f"absolute:{health.get('last_full_success')}",
                f"**距上次成功已 {hours / 24:.1f} 天**，超过 {max_h / 24:.0f} 天上限。\n"
                f"上次完整成功：{health.get('last_full_success')}\n"
                f"（按班次算只错过 {missed} 次，这条是兜底 —— "
                f"防止工作日日历本身算错把告警吃掉）")

    return (False, "ok",
            f"正常。上次成功 {health.get('last_full_success')}，"
            f"错过 {missed} 次（阈值 {need}）")


# ── 告警：三级降级 ────────────────────────────────────────────────────

def _hermes_bin() -> str | None:
    found = shutil.which("hermes")
    if found:
        return found
    for cand in (runtime_home() / "bin" / "hermes",
                 Path.home() / ".local" / "bin" / "hermes"):
        if cand.exists():
            return str(cand)
    return None


def send_alert(text: str, *, log: Path) -> tuple[bool, str]:
    """
    一级 `hermes send` → 二级本机通知 → 三级日志 + 非零退出。

    一级为什么可靠：`hermes send` 对 telegram 这类 bot-token 平台直连
    平台 REST 接口，**不需要 gateway 在跑** —— 而 gateway 没起来正是
    本监控要抓的场景之一。所以它不构成循环依赖。
    但它仍然依赖 hermes 装还在，所以要有二级。
    """
    details = []

    target = read_env("FOLLOWUP_ALERT_TARGET")
    exe = _hermes_bin()
    if target and exe:
        try:
            r = subprocess.run([exe, "send", "-t", target, "-q", text],
                               capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                return True, "hermes send"
            details.append(f"hermes send 退出码 {r.returncode}："
                           f"{(r.stderr or '').strip()[:200]}")
        except Exception as e:  # noqa: BLE001
            details.append(f"hermes send {type(e).__name__}: {e}")
    else:
        details.append("未配置 FOLLOWUP_ALERT_TARGET" if not target
                       else "找不到 hermes 可执行文件")

    # 二级：本机通知。只有人在电脑前才看得到，所以只能当兜底。
    if sys.platform == "darwin":
        try:
            first = text.splitlines()[0][:120]
            subprocess.run(
                ["osascript", "-e",
                 f'display notification {json.dumps(first)} '
                 f'with title {json.dumps(MARK)}'],
                capture_output=True, timeout=30, check=False)
            return True, "本机通知（hermes send 失败：" + "；".join(details) + "）"
        except Exception as e:  # noqa: BLE001
            details.append(f"osascript {type(e).__name__}: {e}")

    # 三级：至少留下痕迹
    try:
        with log.open("a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now().astimezone().isoformat(timespec='seconds')}] "
                    f"告警全部失败（{'；'.join(details)}）\n{text}\n")
    except OSError:
        pass
    return False, "；".join(details)


# ── 主流程 ────────────────────────────────────────────────────────────

def read_json(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return d if isinstance(d, dict) else {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="项目跟进精灵 · 外部存活监控")
    ap.add_argument("--dry-run", action="store_true", help="只打印判定，不发不写")
    ap.add_argument("--self-test", action="store_true",
                    help="强制走一次完整告警链路（会真发消息，用于装机后验证）")
    args = ap.parse_args(argv)

    now = datetime.now().astimezone()
    cfg = load_config()
    sd = state_dir()
    health = read_json(sd / "health.json")
    wd_path = sd / "watchdog_state.json"
    wd_state = read_json(wd_path)
    log = sd / "watchdog.log"

    if args.self_test:
        ok, how = send_alert(
            f"{MARK} · 自检\n\n这是一条测试消息，说明告警链路是通的。\n"
            f"发出时间 {now.isoformat(timespec='seconds')}", log=log)
        print(f"{'✅' if ok else '🔴'} 告警链路自检：{how}")
        return 0 if ok else 1

    if not cfg.get("enabled", True):
        print("⏸ 存活监控在 output.json 里被关闭了（watchdog.enabled=false）")
        return 0

    alert_needed, key, why = judge(health, wd_state, cfg, now)

    if args.dry_run:
        print(f"{'🔴 需要告警' if alert_needed else '✅ 无需告警'}｜去重键 {key}\n{why}")
        return 0

    # 记一笔「我查过了」。这本身也是信息 —— watchdog 自己如果没跑，
    # last_check_at 会停在某个时间点上，下次有人看的时候就知道了。
    new_state = dict(wd_state)
    new_state["last_check_at"] = now.isoformat(timespec="seconds")
    new_state["checks"] = int(wd_state.get("checks") or 0) + 1
    if not health and not wd_state.get("first_seen_missing"):
        new_state["first_seen_missing"] = now.isoformat(timespec="seconds")
    if health:
        new_state.pop("first_seen_missing", None)

    code = 0
    if alert_needed:
        last_at = parse_dt(wd_state.get("last_alert_at"))
        same = wd_state.get("last_alert_key") == key
        within = (last_at is not None
                  and (now - last_at).total_seconds() / 3600
                  < float(cfg.get("repeat_alert_hours") or 24))
        if same and within:
            print(f"🔁 同一故障已在 {wd_state.get('last_alert_at')} 告警过，本次不重复")
        else:
            text = (f"🔴 {MARK}\n\n{why}\n\n"
                    f"这类故障不会产生任何报错，业务只会觉得「最近很安静」。\n"
                    f"常见原因：电脑长时间关机 / gateway 没起来 / 定时任务被删。\n"
                    f"排查：hermes cron list ｜ launchctl list | grep hermes")
            ok, how = send_alert(text, log=log)
            new_state["last_alert_at"] = now.isoformat(timespec="seconds")
            new_state["last_alert_key"] = key
            new_state["last_alert_ok"] = ok
            new_state["last_alert_how"] = how
            print(f"{'✅ 已告警' if ok else '🔴 告警发不出去'}（{how}）\n{why}")
            if not ok:
                code = 1
    else:
        print(f"✅ {why}")

    try:
        sd.mkdir(parents=True, exist_ok=True)
        tmp = wd_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(new_state, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(wd_path)
    except OSError as e:
        print(f"⚠️ 无法写 watchdog_state.json（{type(e).__name__}: {e}）",
              file=sys.stderr)

    return code


if __name__ == "__main__":
    sys.exit(main())
