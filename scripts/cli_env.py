#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
调外部命令行工具时的环境构造。**lark-cli 与 wecom-cli 共用这一份。**

═══════════════════════════════════════════════════════════════════════
🔴 为什么要单独成一个模块：这里面每一条都只在 cron 下发作。

`lark-cli` 和 `wecom-cli` 都是 `#!/usr/bin/env node`，都装在 ~/.local/bin，
都会因为 Hermes 注入的环境变量而拒绝执行。同一个坑咬过两轮（rc2、rc4），
两次的症状都是**「今天没有要催的」** —— 一条催办都不发，且退出码是 0。

复制粘贴到第二个适配层里的后果，是改了一处忘了另一处，
而那一处每天静默跳过一整条业务线。所以：**只有这一份实现。**
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def child_path(exe: str) -> str:
    """
    给外部 CLI 子进程用的 PATH。

    🔴 光找到那个 CLI 还不够：它的 shebang 是 `#!/usr/bin/env node`，
    **执行时还要再找一次 node**。PATH 里没有 node 的话，报出来的是
    `env: node: No such file or directory` —— 一句和「没装 lark-cli」
    毫不相干的错，排查时很容易被带偏。

    node 通常和这些 CLI 装在同一个目录（本机都在 ~/.local/bin），
    所以把 exe 所在目录放最前，再补几个常见位置，最后接继承来的 PATH。
    """
    home = Path(os.path.expanduser("~"))
    parts = [str(Path(exe).parent),
             str(home / ".local" / "bin"),
             str(home / ".hermes" / "node" / "bin"),
             "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    # 继承的 PATH 要**逐段**拼进来。整段 append 的话去重就形同虚设 ——
    # 本机实测会拼出 28 段里 7 段重复。
    parts.extend(os.environ.get("PATH", "").split(":"))
    seen, out = set(), []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return ":".join(out)


# ── Agent context 探测信号 ──────────────────────────────────────────────
# 命中其中任意一个，lark-cli 就拒绝执行并报
# "hermes context detected but lark-cli is not bound to it"。
#
# 🔴 这张清单是**实测枚举**出来的，不是照抄文档。做法是同一个二进制、
#    同一台机器、同一秒，唯一变量是某一个环境变量，逐个跑
#    `lark-cli config show` 看是否报错。
#
# 🔴 **不要改成 `HERMES_*` 前缀通配。** 实测编造的 `HERMES_ZZZ_BUKEN`
#    并不触发，通配等于凭空猜上游语义，会把无关变量一起剔掉。
#
# 🔴 **上游新增探测变量时会原样复发**，而症状是「今天没有要催的」。
#    0.4.0-rc2 只剔了前两个就宣告修复，结果 rc4 又栽在 HERMES_EXEC_ASK 上。
#    当时的验证方法是查 gateway 进程的环境 —— 但 `ps eww` 只显示 exec 时的
#    初始环境，而这些变量是进程起来之后在 Python 里 `os.environ[...] = ...`
#    塞进去的（gateway/run.py 的 HERMES_EXEC_ASK、cli.py 的 HERMES_QUIET）。
#    **看进程环境快照 ≠ 看子进程真正拿到的环境。**
AGENT_CONTEXT_VARS = (
    "HERMES_HOME",
    "OPENCLAW_HOME",
    "HERMES_EXEC_ASK",       # gateway/run.py 模块级无条件注入 —— rc4 的真凶
    "HERMES_GATEWAY_TOKEN",
    "HERMES_SESSION_KEY",
    "HERMES_QUIET",          # cli.py 模块级无条件注入
)


def child_env(exe: str, extra: dict | None = None) -> dict:
    """
    构造外部 CLI 环境，剔除会触发 Agent 上下文绑定的变量。

    `extra` 放各家 CLI 自己的开关（如 lark-cli 的免更新提示），
    剔变量和 PATH 这两件事两边完全一样，所以住在这里。
    """
    env = dict(os.environ)
    for name in AGENT_CONTEXT_VARS:
        env.pop(name, None)
    env.update(extra or {})
    env["PATH"] = child_path(exe)
    return env


def find_bin(name: str) -> str | None:
    """
    找到一个外部 CLI 的可执行文件。

    🔴 不能只靠 PATH。cron 由 launchd 托管的 gateway 派生，它的 PATH 比登录
    shell 短得多 —— 2026-08-04 09:00 那次就栽在这里：`lark-cli` 明明装在
    ~/.local/bin，两条哨兵线却双双报「本机没有安装」，主任务退出码 1。
    """
    found = shutil.which(name)
    if found:
        return found
    home = Path(os.path.expanduser("~"))
    for cand in (home / ".local" / "bin" / name,
                 home / ".hermes" / "bin" / name,
                 Path(f"/opt/homebrew/bin/{name}"),
                 Path(f"/usr/local/bin/{name}")):
        if cand.exists():
            return str(cand)
    return None
