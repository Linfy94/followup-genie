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
import re
import subprocess
from datetime import date, datetime

import cli_env
from qqdoc import LedgerError

ALLOWED_SUBCOMMANDS = frozenset({"+table-list", "+field-list", "+record-list"})

TIMEOUT = 45
PAGE_SIZE = 200

_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d")


# 🔴 找可执行文件、拼 PATH、剔 Agent 变量这三件事**与 wecom_doc 完全一样**，
#    已经提到 cli_env.py 共用。下面三个名字保留成薄包装：现有测试与调用方
#    一个字都不用改，而实现只有一份 —— 复制粘贴的后果是改了一处忘了另一处，
#    而那一处每天静默跳过一整条业务线。
AGENT_CONTEXT_VARS = cli_env.AGENT_CONTEXT_VARS

# lark-cli 自己的开关（免更新提示），与环境剔除无关，所以留在这边。
_LARK_ENV = {
    "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
    "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
}


def lark_cli_bin() -> str | None:
    """找到 lark-cli 可执行文件（候选路径见 cli_env.find_bin）。"""
    return cli_env.find_bin("lark-cli")


def _child_path(exe: str) -> str:
    return cli_env.child_path(exe)


def _child_env(exe: str) -> dict:
    return cli_env.child_env(exe, _LARK_ENV)


def _arg_value(args: list[str], flag: str, default: str = "") -> str:
    """从 argv 里取一个标志的值；只用于生成报错指引。"""
    try:
        return args[args.index(flag) + 1]
    except (ValueError, IndexError):
        return default


def _cli_error(payload: dict, args: list[str]) -> str:
    """
    把 lark-cli 的结构化错误变成一条**唯一且可执行**的处理建议。

    飞书读取同时依赖四层：命名 profile、用户 OAuth、应用 scope、文档协作者。
    以前这里只保留 message，doctor 再统一猜成「重新 auth login」，结果资源权限
    不足时让人反复登录，登录失效时又让作者反复加协作者。结构化字段必须留住。
    """
    err = payload.get("error") or {}
    profile = _arg_value(args, "--profile", "sentinel")
    identity = payload.get("identity") or "user"
    err_type = str(err.get("type") or "")
    subtype = str(err.get("subtype") or "")
    code = err.get("code")
    message = str(err.get("message") or "未知错误")
    hint = str(err.get("hint") or "").strip()
    missing = err.get("missing_scopes") or []
    if not isinstance(missing, list):
        missing = [missing]

    facts = [f"identity={identity}", f"profile={profile}"]
    if err_type:
        facts.append(f"type={err_type}")
    if subtype:
        facts.append(f"subtype={subtype}")
    if code is not None:
        facts.append(f"code={code}")

    lower = " ".join((err_type, subtype, message)).lower()
    if missing or subtype == "missing_scope":
        scopes = "、".join(str(x) for x in missing if x) or "（飞书未返回具体 scope）"
        action = (
            f"处理建议：这是 API 权限范围问题，不是文档协作者问题。"
            f"请给 profile={profile} 补最小的多维表格只读授权；缺少：{scopes}。"
            f"不要让文档作者重复添加协作者。"
        )
        if hint:
            action += f" lark-cli 建议：{hint}"
    elif "keychain" in lower:
        action = (
            f"处理建议：这是 profile={profile} 的本机凭证存储不可用，不是重新扫码"
            f"或文档协作者问题。先在普通终端运行 `lark-cli config show --profile "
            f"{profile}`；若定时任务读不到 macOS 钥匙串，再评估 "
            f"`lark-cli config keychain-downgrade --profile {profile}` 的安全取舍。"
        )
    elif any(word in lower for word in (
            "not logged", "login required", "token expired", "invalid token",
            "unauthorized", "authentication")):
        action = (
            f"处理建议：这是用户登录态问题。请先运行 `lark-cli auth status "
            f"--profile {profile} --json --verify`；确认失败后，只对同一个 profile "
            f"执行 `lark-cli auth login --profile {profile} "
            f"--scope \"bitable:app:readonly offline_access\"`。"
        )
    elif str(code) == "91403" or subtype in {"resource_permission", "forbidden"}:
        action = (
            f"处理建议：当前 user 身份已经调用到飞书，但没有这份 Base 的资源权限。"
            f"请让文档作者把 `auth status --profile {profile}` 显示的实际登录账号"
            f"加入文档协作者。程序固定使用 `--as user`，不要把机器人当成协作者。"
        )
    elif "profile" in lower and any(word in lower for word in (
            "not found", "missing", "initialize", "not initialized")):
        action = (
            f"处理建议：这台电脑没有可用的 profile={profile} 应用配置。"
            f"重新登录不会创建 profile；请先按 `docs/飞书多维表格接入.md` 完成"
            f"应用配置，再对同一个 profile 登录。"
        )
    else:
        action = (
            f"处理建议：先检查 `lark-cli auth status --profile {profile} --json "
            f"--verify`，再用真实 base_token 做只读 `+table-list` 探针。"
            f"不要在“重新登录”和“添加协作者”之间盲目来回尝试。"
        )
        if hint:
            action += f" lark-cli 建议：{hint}"

    return f"{message}\n错误分类：{'，'.join(facts)}\n{action}"


def _run_cli(subcommand: str, args: list[str]) -> dict:
    """调只读白名单内的一个 lark-cli base 子命令，返回解析后的 JSON 信封。"""
    if subcommand not in ALLOWED_SUBCOMMANDS:
        raise LedgerError(
            f"lark_base 只允许调用只读子命令 {sorted(ALLOWED_SUBCOMMANDS)}，"
            f"收到 {subcommand!r}（拒绝调用，这不是可以放宽的检查）"
        )
    profile = _arg_value(args, "--profile", "sentinel")
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
            f"装完请按 docs/飞书多维表格接入.md 配置同一个 profile={profile}。\n"
            "只执行不带 profile 的 `lark-cli auth login` 可能登录到另一套配置，"
            "程序仍然读不到。\n"
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
        raise LedgerError(
            f"lark-cli 调用失败（{subcommand}）：{_cli_error(payload, args)}"
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
    """
    单元格转日期。**认不出来返回 None，而 None 会让整个节点静默跳过**，
    所以这里少认一种格式的代价是「那些项目再也不催」，且只有运行报告里
    一行数据质量警告可查。

    🔴 2026-08-14 实测踩到：飞书哨兵主台账的「立项时间」返回的是
       `2025-10-24T16:06:34.000+08:00`（带 T、毫秒、时区偏移），
       上面三种 strptime 格式一种都不匹配 → **731 行全部解析失败**。
       配置注释里还写着「72/72 都能解析」，说明 lark-cli 或字段类型
       在某次之后改了输出形状，而这件事不报错、只表现成「今天没有要催的」。
       ISO-8601 是飞书的原生形状，必须直接认。
    """
    s = _cell_text(v)
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:
        # Python 3.9 的 fromisoformat 不认末尾的 Z，先换成等价的 +00:00。
        return datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z")
                                      else s).date()
    except ValueError:
        pass
    return _embedded_date(s)


# 「已完成,2026年08月07日」这种：状态和日期挤在同一列里。
# 🔴 只认**完整的年月日**。业务 2026-08-14 定 ④发货 用「方案完成情况」计时，
#    而同一批数据里舆情的「日期」列存在只写 '2026' 的行 —— 把它猜成 2026-01-01
#    会得到一个看起来正常、实际错了大半年的计时起点，
#    **比解析失败更难发现**（失败至少会跳过并报警）。所以年份单独出现一律不认。
_EMBEDDED_DATE = re.compile(
    r"(\d{4})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*日?")


def _embedded_date(s: str) -> date | None:
    m = _EMBEDDED_DATE.search(s)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None  # 2026年13月45日 这种：认出了形状但不是合法日期


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
