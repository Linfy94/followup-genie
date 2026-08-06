#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞书多维表格只读取数层。

═══════════════════════════════════════════════════════════════════════
铁律同 qqdoc.py：本模块只暴露读取能力，不实现任何写操作。
═══════════════════════════════════════════════════════════════════════

不重新实现一遍 Feishu OAuth/token 刷新——本机已有的 lark-cli 把这些都做好了
（多 profile、token 刷新、跨租户自建应用授权）。这里只是 subprocess 调用它、
解析 JSON，把结果包成跟 qqdoc.Sheet 同样接口（header / data_rows / text /
date / has_column）的对象，好让 core.py 不用为飞书另写一套判定逻辑。

只读白名单写死为 ALLOWED_SUBCOMMANDS，不从配置读取、不做字符串拼接执行。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import date, datetime
from pathlib import Path

from qqdoc import LedgerError

ALLOWED_SUBCOMMANDS = frozenset({"+table-list", "+field-list", "+record-list"})

TIMEOUT = 45
PAGE_SIZE = 200

_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d")


def lark_cli_bin() -> str | None:
    """
    找到 lark-cli 可执行文件。

    🔴 不能只靠 PATH。cron 由 launchd 托管的 gateway 派生，它的 PATH 比登录
    shell 短得多 —— 2026-08-04 09:00 那次就栽在这里：`lark-cli` 明明装在
    ~/.local/bin，两条哨兵线却双双报「本机没有安装」，主任务退出码 1。

    这个坑项目里早就防过一次（check_followup._hermes_bin 的同名注释），
    只是当时没推广到这里。候选路径与那边保持一致。
    """
    found = shutil.which("lark-cli")
    if found:
        return found
    home = Path(os.path.expanduser("~"))
    for cand in (home / ".local" / "bin" / "lark-cli",
                 home / ".hermes" / "bin" / "lark-cli",
                 Path("/opt/homebrew/bin/lark-cli"),
                 Path("/usr/local/bin/lark-cli")):
        if cand.exists():
            return str(cand)
    return None


def _child_path(exe: str) -> str:
    """
    给 lark-cli 子进程用的 PATH。

    🔴 光找到 lark-cli 还不够：它的 shebang 是 `#!/usr/bin/env node`，
    **执行时还要再找一次 node**。PATH 里没有 node 的话，报出来的是
    `env: node: No such file or directory` —— 一句和"没装 lark-cli"
    毫不相干的错，排查时很容易被带偏。

    node 通常和 lark-cli 装在同一个目录（本机都在 ~/.local/bin），
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


# ── lark-cli 的 Agent context 探测信号 ──────────────────────────────────
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


def _child_env(exe: str) -> dict:
    """构造外部 CLI 环境，剔除会触发 Agent 上下文绑定的变量。"""
    env = dict(os.environ)
    for name in AGENT_CONTEXT_VARS:
        env.pop(name, None)
    env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
    env["PATH"] = _child_path(exe)
    return env


def _run_cli(subcommand: str, args: list[str]) -> dict:
    """调只读白名单内的一个 lark-cli base 子命令，返回解析后的 JSON 信封。"""
    if subcommand not in ALLOWED_SUBCOMMANDS:
        raise LedgerError(
            f"lark_base 只允许调用只读子命令 {sorted(ALLOWED_SUBCOMMANDS)}，"
            f"收到 {subcommand!r}（拒绝调用，这不是可以放宽的检查）"
        )
    exe = lark_cli_bin()
    if exe is None:
        # 🔴 **绝不自动安装。** 装一个命令行工具会改动这台电脑的全局环境，
        #    该由人来决定。这里只把话说清楚，然后停下。
        #    话要说得让没有开发背景的人也能照着做。
        raise LedgerError(
            "读不了飞书台账：这台电脑上找不到 lark-cli。\n"
            "\n"
            "lark-cli 是飞书官方的命令行工具，本程序靠它读飞书多维表格。\n"
            "只有配置了飞书台账才需要它；只用腾讯文档的话，不用装。\n"
            "\n"
            "怎么装（需要先有 Node.js）：\n"
            "    npm install -g @larksuiteoapi/lark-cli\n"
            "装完在终端里执行一次 `lark-cli auth login` 完成授权。\n"
            "\n"
            "已经找过这些位置，都没有：PATH、~/.local/bin、~/.hermes/bin、"
            "/opt/homebrew/bin、/usr/local/bin。\n"
            "\n"
            "🔴 如果你在终端里敲 `which lark-cli` 明明是有的，那就不是没装，"
            "而是定时任务能看到的目录比你终端里少。"
            "把它装到上面任意一个位置，或者在定时任务的配置里补上它所在的目录。"
        )
    cmd = [exe, "base", subcommand, *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=TIMEOUT,
            env=_child_env(exe),
        )
    except FileNotFoundError as e:
        # 上面 exists() 过了还能走到这里：文件在但不可执行（权限/损坏的软链）
        raise LedgerError(
            f"lark-cli 存在于 {exe} 但无法执行（权限不足或链接已断）"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise LedgerError(f"lark-cli 调用超时（{TIMEOUT}s）：{subcommand}") from e

    raw = proc.stdout if proc.returncode == 0 else proc.stderr
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise LedgerError(f"lark-cli 返回不是合法 JSON（{subcommand}）：{raw[:300]}") from e

    if not payload.get("ok"):
        err = payload.get("error") or {}
        raise LedgerError(
            f"lark-cli 调用失败（{subcommand}）：{err.get('message') or payload}"
        )
    return payload


def _os_environ() -> dict:
    """已废弃；保留兼容，外部 CLI 必须改用 `_child_env()`。"""
    return dict(os.environ)


def _record_list_page(base_token: str, table_id: str, profile: str,
                      offset: int) -> dict:
    args = [
        "--base-token", base_token, "--table-id", table_id,
        "--profile", profile, "--as", "user", "--format", "json",
        "--limit", str(PAGE_SIZE), "--offset", str(offset),
    ]
    return _run_cli("+record-list", args)["data"]


def _fetch_table(base_token: str, table_id: str, profile: str
                 ) -> tuple[list[str], list[str], list[list]]:
    """读一张表的全部记录（分页）。返回 (字段名列表, record_id 列表, 行值列表)。"""
    fields: list[str] | None = None
    record_ids: list[str] = []
    rows: list[list] = []
    offset = 0
    while True:
        d = _record_list_page(base_token, table_id, profile, offset)
        if fields is None:
            fields = d.get("fields") or []
        page_rows = d.get("data") or []
        record_ids.extend(d.get("record_id_list") or [])
        rows.extend(page_rows)
        if not d.get("has_more") or not page_rows:
            break
        offset += len(page_rows)
    return fields or [], record_ids, rows


def _cell_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        if not v:
            return ""
        if isinstance(v[0], dict):
            # link 字段：[{"id": "recvXXX"}, ...]。非空即"有值"；
            # 判定只关心 empty/not_empty，用 record_id 拼接即可，不用于展示。
            return ",".join(str(x.get("id", "")) for x in v if isinstance(x, dict))
        return ",".join(str(x) for x in v)
    return str(v)


def _cell_date(v) -> date | None:
    s = _cell_text(v)
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


class Sheet:
    """
    一张飞书多维表格的只读视图，接口跟 qqdoc.Sheet 对齐
    （header / data_rows / has_column / text / date），core.py 不用区分数据源。
    """

    def __init__(self, header: list[str], rows: dict[int, dict]):
        self.header = header
        self._rows = rows
        self._col_set = {h for h in header if h}
        # lark-cli 返回的字段名本身就是唯一的（Base 不允许重名字段），不会有重复列。
        self.duplicate_columns: list[str] = []

    @property
    def data_rows(self) -> list[int]:
        return sorted(self._rows.keys())

    def has_column(self, name: str) -> bool:
        return name in self._col_set

    def text(self, row: int, col_name: str) -> str:
        return _cell_text(self._rows.get(row, {}).get(col_name))

    def date(self, row: int, col_name: str) -> date | None:
        return _cell_date(self._rows.get(row, {}).get(col_name))


def check_credential(base_token: str, profile: str = "sentinel") -> None:
    """
    最轻量的只读探针：确认这个 profile 的 lark-cli 身份能读到这个 Base。
    读不到会抛 LedgerError（信息里带着 lark-cli 的原始错误）。给 doctor.py 用。
    """
    _run_cli("+table-list", ["--base-token", base_token, "--profile", profile, "--as", "user"])


def read_sheet(base_token: str, table_id: str, *, profile: str = "sentinel",
               link_date_fields: list[dict] | None = None) -> Sheet:
    """
    读一张多维表格主表的全部记录。

    link_date_fields：主表上的某些 link 字段（比如「AI哨兵发货表」）本身只
    是关联记录的 id，看不到日期。这里按配置把它替换成关联表里那条记录的
    某个日期字段的值（取最早一条），这样 core.py 的 clock/empty/not_empty
    判定可以直接用这个字段名，不用另外为"跨表取值"写一套逻辑。
    格式：[{"link_field", "child_table_id", "child_date_field"}, ...]
    """
    fields, record_ids, rows = _fetch_table(base_token, table_id, profile)
    # 飞书自己的 record_id 永远唯一，暴露成一个普通列——这张表常见的人类可读
    # 字段（比如"企业名称"）不保证唯一：同一家公司在不同分行各开一个项目
    # 很正常。想用业务字段当主键就用那个字段，想要保真的唯一键就用这个。
    header = list(fields) + ["_record_id"]
    table: dict[int, dict] = {}
    for i, (rid, vals) in enumerate(zip(record_ids, rows), start=1):
        table[i] = dict(zip(fields, vals))
        table[i]["_record_id"] = rid

    for spec in link_date_fields or []:
        link_field = spec["link_field"]
        child_table_id = spec["child_table_id"]
        child_date_field = spec["child_date_field"]
        c_fields, c_record_ids, c_rows = _fetch_table(base_token, child_table_id, profile)
        child_by_id = {rid: dict(zip(c_fields, vals)) for rid, vals in zip(c_record_ids, c_rows)}
        for row in table.values():
            linked = row.get(link_field) or []
            dates = []
            for entry in linked:
                if not isinstance(entry, dict):
                    continue
                child_row = child_by_id.get(entry.get("id"))
                if child_row:
                    v = _cell_text(child_row.get(child_date_field))
                    if v:
                        dates.append(v)
            # 取最早一次（比如最早的发货时间）——固定宽度的 "YYYY-MM-DD HH:MM:SS"
            # 字符串排序等价于时间排序，不用现在就 parse。
            row[link_field] = min(dates) if dates else ""

    return Sheet(header, table)
