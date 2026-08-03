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
import plistlib
import shutil
import subprocess
import sys
import uuid
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


# 每个字段的类型与取值范围。
#
# 🔴 0.3.0-rc1 只在使用点写 `int(cfg.get("schedule_hour") or 9)` ——
#    配置写成 "九" 直接 ValueError 裸崩；写成 25 更隐蔽，
#    要等到 `time(25, 0)` 那一行才炸。监控器崩掉 = 完全没有监控，
#    而崩的原因还是一个手滑打错的数字。
#
# 校验策略：**坏值退回默认值 + 收一条人话警告，绝不罢工。**
# 监控器因为配置坏了而不跑，正是最不该发生的事 —— 它存在的意义
# 就是在别的东西都坏了的时候还能吭一声。
SPEC = {
    "enabled":                    ("bool", None, None),
    "schedule_hour":              ("int", 0, 23),
    "count_weekends":             ("bool", None, None),
    "missed_runs_before_alert":   ("int", 1, 1000),
    "absolute_max_hours":         ("number", 1, 24 * 365),
    "repeat_alert_hours":         ("number", 0, 24 * 365),
    "missing_health_grace_hours": ("number", 0, 24 * 365),
}


def validate_config(seg) -> tuple[dict, list[str]]:
    """
    校验 watchdog 配置段，返回 `(可用的配置, 人话警告列表)`。

    整段不是对象、字段类型不对、数值越界 —— 一律退回默认值并说清楚
    哪个字段、写的是什么、为什么不行、现在按什么值跑。
    """
    cfg = dict(DEFAULTS)
    warnings: list[str] = []

    if seg is None:
        return cfg, warnings
    if not isinstance(seg, dict):
        return cfg, [f"output.json 里的 watchdog 应该是一个对象 {{...}}，"
                     f"实际是 {type(seg).__name__} —— 整段忽略，全部按默认值跑"]

    for key, value in seg.items():
        if key.startswith("_"):        # 下划线开头是注释，本来就不是配置
            continue
        if key not in SPEC:
            warnings.append(f"watchdog.{key} 不是可识别的配置项，已忽略")
            continue

        kind, low, high = SPEC[key]
        default = DEFAULTS[key]

        if kind == "bool":
            # 🔴 不用 bool(value)：那样 "false" 这个字符串会变成 True，
            #    配置写错反而更危险 —— 关掉监控和打开监控是反的。
            if not isinstance(value, bool):
                warnings.append(
                    f"watchdog.{key} 应该是 true 或 false，"
                    f"实际是 {value!r} —— 按默认值 {default} 跑")
                continue
            cfg[key] = value
            continue

        # 数值。bool 是 int 的子类，得先排掉，否则 true 会被当成 1。
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            warnings.append(
                f"watchdog.{key} 应该是数字，实际是 {value!r} —— "
                f"按默认值 {default} 跑")
            continue
        if kind == "int" and not float(value).is_integer():
            warnings.append(
                f"watchdog.{key} 应该是整数，实际是 {value!r} —— "
                f"按默认值 {default} 跑")
            continue
        if not (low <= value <= high):
            warnings.append(
                f"watchdog.{key} 应该在 {low}–{high} 之间，实际是 {value!r} —— "
                f"按默认值 {default} 跑")
            continue

        cfg[key] = int(value) if kind == "int" else value

    return cfg, warnings


def load_config() -> tuple[dict, list[str]]:
    """读并校验配置。返回 `(配置, 警告列表)`。"""
    p = runtime_home() / "followup" / "config" / "output.json"
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        # 配置不存在或读不了就用默认值继续 —— 见 SPEC 上方的说明。
        return dict(DEFAULTS), []
    try:
        data = json.loads(raw)
    except ValueError as e:
        return dict(DEFAULTS), [f"output.json 不是合法 JSON（{e}）—— 全部按默认值跑"]
    if not isinstance(data, dict):
        return dict(DEFAULTS), [
            f"output.json 的顶层应该是一个对象 {{...}}，"
            f"实际是 {type(data).__name__} —— 全部按默认值跑"]
    return validate_config(data.get("watchdog"))


def parse_dt(s) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        d = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    return d if d.tzinfo else d.astimezone()


def nonnegative_int(value) -> int:
    """读取监控器自己的计数；损坏值退回 0，不能反过来拖垮监控。"""
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return number if number >= 0 else 0


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
    # cfg 已经过 validate_config()，这里不再做类型转换 ——
    # 在使用点 int() 正是 0.3.0-rc1 裸崩的地方（"九" → ValueError）。
    hour = cfg["schedule_hour"]
    weekends = cfg["count_weekends"]
    need = cfg["missed_runs_before_alert"]
    max_h = cfg["absolute_max_hours"]

    if not health:
        first_seen = parse_dt(wd_state.get("first_seen_missing")) or now
        hours = (now - first_seen).total_seconds() / 3600
        grace = cfg["missing_health_grace_hours"]
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
        if not isinstance(f, dict):
            f = {"stage": "?", "reason": "失败记录损坏，无法读取"}
        return (True, "never-succeeded",
                f"**有健康记录，但从来没有过一次完整成功。**\n"
                f"最近一次失败：[{f.get('stage', '?')}] {str(f.get('reason', ''))[:200]}")

    if last_ok - now > timedelta(minutes=5):
        return (True, f"future-success:{health.get('last_full_success')}",
                f"**健康记录里的上次成功时间位于未来。**\n"
                f"记录值：{health.get('last_full_success')}\n"
                f"当前时间：{now.isoformat(timespec='seconds')}\n"
                f"可能是系统时钟回拨或 health.json 损坏；不能按正常状态处理。")

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
    #
    # 🔴 0.3.0-rc1 在这里 `check=False` 之后**无条件 return True**，
    #    退出码根本没看。而 osascript 失败是常事：launchd 里没有 GUI 会话、
    #    通知权限被拒、Script Editor 被 MDM 限制 —— 全都返回非零。
    #    结果是三级降级里的第三级永远走不到，watchdog 报「已告警」并退 0，
    #    而那条告警**根本没发出去**。
    if sys.platform == "darwin":
        try:
            first = text.splitlines()[0][:120]
            r = subprocess.run(
                ["osascript", "-e",
                 f'display notification {json.dumps(first)} '
                 f'with title {json.dumps(MARK)}'],
                capture_output=True, text=True, timeout=30, check=False)
            if r.returncode == 0:
                return True, "本机通知（hermes send 失败：" + "；".join(details) + "）"
            details.append(f"osascript 退出码 {r.returncode}："
                           f"{(r.stderr or '').strip()[:200]}")
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


# ── 自我安装：搬到 skill 之外 ──────────────────────────────────────────

LAUNCHD_LABEL = "com.linfangyu.followup-watchdog"

# 找 Python 的顺序。**不硬编码 /usr/bin/python3** ——
# 它在精简过的系统、Linux、或者 CommandLineTools 没装时并不存在，
# 那样 plist 会指向一个不存在的文件，launchd 静默失败，
# 而「监控器自己没跑」恰恰没有任何人会发现。
PYTHON_CANDIDATES = ("/usr/bin/python3", "/opt/homebrew/bin/python3",
                     "/usr/local/bin/python3")


def watchdog_dir() -> Path:
    """
    监控器的独立安装位置。

    🔴 **必须在 skill 目录之外。** 0.3.0-rc1 把它留在
       `skills/work/followup-genie/scripts/` 里，于是 skill 被移动、删除、
       或者 `hermes skills update` 装坏时，plist 指向的文件就没了 ——
       launchd 静默失败，监控器连同被监控对象一起消失。

       而「skill 坏了」正是它最该报警的场景之一。监控器和被监控对象
       死在同一个目录里，等于没有监控。

    放运行时目录下：升级不覆盖运行时目录，删 skill 也波及不到它。
    """
    return runtime_home() / "watchdog"


def find_python() -> str:
    """
    找一个能用的 python3。安装时检测，把结果写死进 plist。

    刻意**不用 sys.executable**：装的时候可能是 hermes 的 venv python 在跑，
    而监控器一旦依赖 hermes 的 venv，hermes 装坏了它就跟着哑 ——
    正是它要抓的场景。要的是系统自带那个。
    """
    for cand in PYTHON_CANDIDATES:
        p = Path(cand)
        if p.is_file() and os.access(cand, os.X_OK):
            return cand
    found = shutil.which("python3")
    if found:
        return found
    raise RuntimeError(
        "找不到可用的 python3。试过：" + "、".join(PYTHON_CANDIDATES)
        + "，以及 PATH。macOS 上通常执行一次 `xcode-select --install` 即可。")


def render_plist(python: str, script: Path, home: Path, hour: int) -> str:
    """
    生成填好真实路径的 plist —— 不再让人对着「请改成…」手抄。

    🔴 **plist 里的 XML 注释绝不能出现连续两个减号。** XML 规范禁止，
       写了整个文件就是非法 XML。

       这条踩过一次：注释里写了 install 那个带两个减号的参数写法。
       launchd 自己的解析器容忍度高、照样加载了，所以在开发机上
       **完全看不出问题**；但 plistlib、xmllint 这类严格解析器直接报
       `not well-formed`。

       代价可能很大：plist 一旦被某个 macOS 版本或某个工具判为非法，
       launchd 就不加载它 —— 而**监控器不加载的表现就是「一切安静」**，
       和「一切正常」长得一模一样。这正是整个 watchdog 存在的理由。

       tests/test_watchdog_install.py 里有两条测试钉着这件事。
    """
    hermes_bin = Path.home() / ".local" / "bin"
    log = home / "followup" / "state" / "watchdog.launchd.log"
    data = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [python, str(script)],
        "EnvironmentVariables": {
            "FOLLOWUP_HOME": str(home),
            "PATH": (f"{hermes_bin}:/opt/homebrew/bin:/usr/local/bin:"
                     "/usr/bin:/bin:/usr/sbin:/sbin"),
        },
        "StartCalendarInterval": [
            {"Hour": (hour + 1) % 24, "Minute": 30},
            {"Hour": (hour + 6) % 24, "Minute": 30},
        ],
        "RunAtLoad": True,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }
    return plistlib.dumps(data, fmt=plistlib.FMT_XML, sort_keys=False).decode("utf-8")


def _atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    """先完整写同目录临时文件，再原子替换；失败时旧文件保持不变。"""
    tmp = path.with_name(f".{path.name}.new.{uuid.uuid4().hex}")
    try:
        with tmp.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, mode)
        os.replace(str(tmp), str(path))
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def install(version: str = "") -> tuple[Path, Path, str]:
    """
    把监控器复制到 skill 之外，并生成填好路径的 plist。

    **不执行 `launchctl load`** —— 装不装由人决定，这里只把东西放好
    并打印那几条命令。返回 `(脚本路径, plist 路径, python 路径)`。
    """
    python = find_python()
    home = runtime_home()
    d = watchdog_dir()
    d.mkdir(parents=True, exist_ok=True)

    script = d / "watchdog.py"
    source_bytes = Path(__file__).resolve().read_bytes()
    _atomic_write(script, source_bytes, 0o755)

    _atomic_write(d / "VERSION", (version or "unknown").encode("utf-8"))

    cfg, _ = load_config()
    plist = d / f"{LAUNCHD_LABEL}.plist"
    plist_text = render_plist(python, script, home, cfg["schedule_hour"])
    _atomic_write(plist, plist_text.encode("utf-8"))
    return script, plist, python


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
    ap.add_argument("--install", action="store_true",
                    help="把监控器装到 skill 之外并生成 plist（不会 launchctl load）")
    ap.add_argument("--version", default="",
                    help="配合 --install，记录装的是哪一版")
    args = ap.parse_args(argv)

    if args.install:
        try:
            script, plist, python = install(args.version)
        except (OSError, RuntimeError) as e:
            print(f"🔴 安装失败：{e}", file=sys.stderr)
            return 1
        print(f"✅ 监控器已装到 skill 之外：{script}")
        print(f"   Python：{python}（安装时检测到的）")
        print(f"   plist：{plist}")
        print("\n还差三条命令，需要你自己跑（我不替你动 launchd）：")
        print(f"  cp {plist} ~/Library/LaunchAgents/")
        print(f"  launchctl load ~/Library/LaunchAgents/{plist.name}")
        print(f"  {python} {script} --self-test    # 会真发一条消息，收到才算通")
        return 0

    now = datetime.now().astimezone()
    cfg, cfg_warnings = load_config()
    for w in cfg_warnings:
        # 打到 stderr：配置写错要看得见，但不能挡住正常判定输出。
        print(f"⚠️ 配置有问题：{w}", file=sys.stderr)
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

    if not cfg["enabled"]:
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
    new_state["checks"] = nonnegative_int(wd_state.get("checks")) + 1
    if not health and not wd_state.get("first_seen_missing"):
        new_state["first_seen_missing"] = now.isoformat(timespec="seconds")
    if health:
        new_state.pop("first_seen_missing", None)

    code = 0
    if alert_needed:
        last_at = parse_dt(wd_state.get("last_alert_at"))
        same = wd_state.get("last_alert_key") == key
        elapsed = ((now - last_at).total_seconds() / 3600
                   if last_at is not None else None)
        within = (elapsed is not None and 0 <= elapsed
                  < cfg["repeat_alert_hours"])
        if same and within:
            print(f"🔁 同一故障已在 {wd_state.get('last_alert_at')} 告警过，本次不重复")
        else:
            text = (f"🔴 {MARK}\n\n{why}\n\n"
                    f"这类故障不会产生任何报错，业务只会觉得「最近很安静」。\n"
                    f"常见原因：电脑长时间关机 / gateway 没起来 / 定时任务被删。\n"
                    f"排查：hermes cron list ｜ launchctl list | grep hermes")
            ok, how = send_alert(text, log=log)
            new_state["last_alert_ok"] = ok
            new_state["last_alert_how"] = how

            if ok:
                # 🔴 去重键**只在发成功之后才写**。
                #
                #    0.3.0-rc1 是无条件写的，后果很难自己发现：
                #    告警发失败 → 照样记进 24 小时去重 → 下次检查看到
                #    「同一故障已告警过」→ 跳过 → 再下次还是跳过……
                #    故障一直在，而告警**永远发不出去**。
                #
                #    监控器最不能有的就是这种失败：它自己哑了，还以为自己在响。
                new_state["last_alert_at"] = now.isoformat(timespec="seconds")
                new_state["last_alert_key"] = key
                new_state.pop("alert_failures", None)
                new_state.pop("last_alert_failed_at", None)
            else:
                # 失败只留诊断痕迹，**不进去重判定** —— 下次检查必定重试。
                new_state["last_alert_failed_at"] = now.isoformat(timespec="seconds")
                new_state["alert_failures"] = nonnegative_int(
                    wd_state.get("alert_failures")) + 1

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
