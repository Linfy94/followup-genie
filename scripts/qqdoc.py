#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
腾讯文档只读取数层（零第三方依赖，仅标准库）。

═══════════════════════════════════════════════════════════════════════
铁律：台账源一律只读。本模块只暴露读取能力，写入类工具一律不实现。
═══════════════════════════════════════════════════════════════════════

命令白名单写死为常量 ALLOWED_TOOLS，不从配置读取、不做字符串拼接执行。
特别注意 sheet.operation_sheet —— 它是表格内的 JS 沙箱，能改值改样式增删行列，
**永远不要加进白名单**（实测当前 token 对它无权限，那是第二道保险，不是理由）。

本模块封装了四个实测踩过的坑（详见方案「对抗式审查发现」）：
  P0-1  行列索引是 0-based，end_row/end_col 是包含式。
        传 start_row=1 会静默吃掉表头行，返回整体错位一格的数据且不报错。
  P0-2  NUMBER 型单元格的 string_value 恒为空，值在 number_value 里。
        用 string_value 读「序号」会得到 101 个空串，主键静默失效。
  P0-3  日期在 cells 模式下是 serial number（基准 1899-12-30），
        CSV 模式下才是 "2026/3/31" 字符串。用 CSV 等于依赖单元格显示格式。
  P0-4  cells 模式不返回空单元格（键不存在 ≠ 空字符串）。
"""

from __future__ import annotations  # 兼容 Python 3.9（macOS 自带版本）

import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import nethttp

# ── 只读白名单：改动此处等于改动安全边界，需人工评审 ────────────────────
ALLOWED_TOOLS = frozenset({
    "sheet.get_sheet_info",     # 只返回子表行列数
    "sheet.get_cell_data",      # 只返回单元格值与类型
    "manage.query_file_info",   # 只读性核对用（最后修改人/时间）
})

ENDPOINT = "https://docs.qq.com/openapi/mcp"
TIMEOUT = 45
RETRIES = 3

# Excel/Lotus 日期 serial 基准。已实测校准：46112 → 2026-03-31。
# 1899-12-31 和 1900-01-01 都差 1~2 天，不要改。
SERIAL_EPOCH = date(1899, 12, 30)

# 单次请求最多取多少行，超过则分页。腾讯侧未公布上限，取保守值。
PAGE_ROWS = 500


class LedgerError(Exception):
    """取数失败。必须与「今天没有超时单」区分开——前者是故障，后者是正常。"""


def _hermes_home() -> Path:
    """
    运行时解析配置目录，不硬编码任一平台形式。
    优先级：`FOLLOWUP_HOME` > `HERMES_HOME` > 平台默认。

    与 core.hermes_home() **刻意重复**：qqdoc 是最底层的取数模块，
    core 依赖它，它不能反过来依赖 core（循环导入）。十几行的重复换掉
    循环依赖，值 —— 但两处必须同时改，改一处就会出现「取数找得到凭证、
    判定找不到配置」这种极难排查的半瘫状态。
    """
    import os
    env = os.environ.get("FOLLOWUP_HOME") or os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "hermes"
    return Path.home() / ".hermes"


def load_token() -> str:
    """从 <运行时目录>/.env 读 TENCENT_DOCS_TOKEN。绝不打印 token 本身。"""
    env_path = _hermes_home() / ".env"
    if not env_path.exists():
        raise LedgerError(f"凭证文件不存在：{env_path}")
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("TENCENT_DOCS_TOKEN="):
            tok = line.split("=", 1)[1].strip().strip("'\"")
            if tok:
                return tok
    raise LedgerError(
        "未找到 TENCENT_DOCS_TOKEN。请到 docs.qq.com →「更多操作 → 开放平台」"
        "→「OpenClaw 专用入口」扫码获取，写入 <运行时目录>/.env"
    )


def _rpc(method: str, params: dict | None = None) -> dict:
    """发一次 JSON-RPC。失败重试，最终失败抛 LedgerError（不返回空数据）。"""
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": load_token(),
    }
    last = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(ENDPOINT, data=body, headers=headers)
            # 走 nethttp：TLS 1.3 被中间层搞坏时自动降到 1.2。见该模块头注。
            # 🔴 read() 必须由 nethttp 代做 —— 读到一半断掉同样是 TLS 失败，
            #    留在这里就既不降级、也只表现成普通重试失败。
            # 只读的 JSON-RPC，重发无副作用 → idempotent=True。
            raw = nethttp.fetch(req, timeout=TIMEOUT,
                                idempotent=True).decode("utf-8", "replace")
            break
        except urllib.error.HTTPError as e:
            # 🔴 HTTPError 本身是个类文件对象（持有底层连接），两条后续路径
            # （直接 raise 401/403、或记录 last 继续重试）都不会自动关闭它——
            # 不主动 close() 就要等 GC 才收，长期运行下去会攒一堆
            # ResourceWarning。先关掉，两条路径都不用再操心。
            e.close()
            last = f"HTTP {e.code}"
            if e.code in (401, 403):
                raise LedgerError(
                    f"凭证失效或无权限（HTTP {e.code}）。请重新扫码授权腾讯文档。"
                ) from e
        except Exception as e:  # 网络不可达、超时等
            last = f"{type(e).__name__}: {e}"
        if attempt < RETRIES - 1:
            time.sleep(2 * (attempt + 1))
    else:
        raise LedgerError(f"腾讯文档接口连续 {RETRIES} 次请求失败：{last}")

    # SSE 形式的响应：取 data: 行
    if raw.lstrip().startswith("event:") or "\ndata:" in raw:
        for ln in raw.splitlines():
            if ln.startswith("data:"):
                raw = ln[5:].strip()
                break
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise LedgerError(f"接口返回不是合法 JSON（可能接口结构变化）：{raw[:200]}") from e

    if "error" in payload:
        raise LedgerError(f"接口报错：{payload['error']}")
    return payload


def call_tool(name: str, arguments: dict) -> dict:
    """调用一个白名单内的只读工具，返回已解析的 result 内容。"""
    if name not in ALLOWED_TOOLS:
        # 这不是可以放宽的检查。写入类工具一律走不到这里。
        raise LedgerError(
            f"工具 {name!r} 不在只读白名单内，拒绝调用。"
            f"白名单：{sorted(ALLOWED_TOOLS)}"
        )
    payload = _rpc("tools/call", {"name": name, "arguments": arguments})
    try:
        text = payload["result"]["content"][0]["text"]
    except (KeyError, IndexError) as e:
        raise LedgerError(f"接口返回结构异常（可能接口变化）：{str(payload)[:200]}") from e
    data = json.loads(text)
    if data.get("error"):
        raise LedgerError(f"{name} 返回业务错误：{data['error']}")
    return data


# ── 取值：P0-2 / P0-3 / P0-4 的护栏都在这里 ──────────────────────────

def cell_text(cell: dict | None) -> str:
    """
    取单元格的文本值。

    P0-2：NUMBER 型的 string_value 恒为空，必须先看 value_type。
          「序号」正是 NUMBER 型，而它是灰色名单/例外表/state 的主键。
    P0-4：cell 为 None（键不存在）就是空，这是 cells 模式的空值表示。
    """
    if not cell:
        return ""
    vt = cell.get("value_type")
    if vt == "NUMBER":
        n = cell.get("number_value", 0)
        # 整数不要显示成 3.0，否则序号 "3" 匹配不上 "3.0"
        return str(int(n)) if float(n).is_integer() else str(n)
    if vt == "BOOL":
        return "true" if cell.get("bool_value") else "false"
    return (cell.get("string_value") or "").strip()


_TEXT_DATE_RE = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$")


def cell_date(cell: dict | None) -> date | None:
    """
    取单元格的日期值。

    P0-3：cells 模式下日期通常是 serial number，不是字符串。用 serial 自己转，
          完全不受单元格显示格式影响——业务把格式改成「3月31日」也不会挂。

    🔴 2026-08-20 接 AI外贸拓客台账实测：同一列「访客/需求时间」里，
       210/1312 行（16%）是 STRING 类型、内容形如 "2025/9/16"——不是
       cells 模式该有的样子，本文件头部 P0-3 注释原话是「CSV 模式下才是
       这种字符串」，但这批行确实是 STRING 类型，大概率是手工录入或
       旧版 RPA 导入时绕开了日期选择器。206/210 能解析出年月日，
       **全部**早于 2026-06——不是随机噪声，是一整批同源的历史数据。

       只认 "YYYY/M/D" 这一种**无歧义**格式（年在前，斜杠分隔，不猜
       "M/D/YYYY" 这类会跟中国以外习惯冲突的写法），业务 2026-08-20
       确认要推广到所有腾讯文档台账，不只这一条线——这条护栏迟早会在
       别的台账上复现同一种「手工录入绕开日期选择器」的模式。
    """
    if not cell:
        return None
    vt = cell.get("value_type")
    if vt == "NUMBER":
        n = cell.get("number_value")
        if n is None:
            return None
        try:
            days = int(n)
        except (TypeError, ValueError):
            return None
        # 合理性护栏：serial 太小或太大说明这列不是日期
        if not (20000 < days < 80000):  # 约 1954-10 ~ 2119-01
            return None
        return SERIAL_EPOCH + timedelta(days=days)
    if vt == "STRING":
        m = _TEXT_DATE_RE.match((cell.get("string_value") or "").strip())
        if not m:
            return None
        y, mo, d = (int(x) for x in m.groups())
        try:
            return date(y, mo, d)
        except ValueError:
            return None  # 形状对但不是合法日期（比如 2025/13/40），不猜
    return None


class Sheet:
    """
    一张子表的只读视图。

    行按 0-based 存放，row 0 是表头。列一律按名字访问，
    绝不按位置——业务在中间插入「终止」列时，按位置读会整体错位且不报错。
    """

    def __init__(self, header: list[str], rows: dict[int, dict[int, dict]]):
        self._rows = rows
        self.header = header
        # 列名 → 列号。重复列名保留第一个，并在 duplicate_columns 里记下
        self.col_of: dict[str, int] = {}
        self.duplicate_columns: list[str] = []
        for idx, name in enumerate(header):
            name = (name or "").strip()
            if not name:
                continue
            if name in self.col_of:
                self.duplicate_columns.append(name)
            else:
                self.col_of[name] = idx

    @property
    def data_rows(self) -> list[int]:
        """所有数据行号（不含表头），升序。"""
        return sorted(r for r in self._rows if r >= 1)

    def has_column(self, name: str) -> bool:
        return name in self.col_of

    def cell(self, row: int, col_name: str) -> dict | None:
        col = self.col_of.get(col_name)
        if col is None:
            return None
        return self._rows.get(row, {}).get(col)

    def text(self, row: int, col_name: str) -> str:
        return cell_text(self.cell(row, col_name))

    def date(self, row: int, col_name: str) -> date | None:
        return cell_date(self.cell(row, col_name))

    def row_is_blank(self, row: int) -> bool:
        return not any(cell_text(c) for c in self._rows.get(row, {}).values())


def read_sheet(file_id: str, sheet_id: str) -> Sheet:
    """
    读一张子表的全部内容。

    P0-1：start_row / start_col 从 0 开始，end_row / end_col 是包含式。
          row_count 从 get_sheet_info 动态取，绝不写死——业务追加行会超出。
    """
    info = call_tool("sheet.get_sheet_info", {"file_id": file_id})
    target = None
    for sh in info.get("sheets", []):
        if sh.get("sheet_id") == sheet_id:
            target = sh
            break
    if target is None:
        available = [
            f"{s.get('sheet_id')}({s.get('sheet_name')})" for s in info.get("sheets", [])
        ]
        raise LedgerError(
            f"子表 {sheet_id} 不存在。该文件现有子表：{', '.join(available) or '无'}"
        )

    row_count = int(target.get("row_count") or 0)
    col_count = int(target.get("col_count") or 0)
    if row_count < 2 or col_count < 1:
        raise LedgerError(f"子表 {sheet_id} 看起来是空表（{row_count} 行 {col_count} 列）")

    rows: dict[int, dict[int, dict]] = {}
    start = 0
    while start < row_count:
        end = min(start + PAGE_ROWS - 1, row_count - 1)
        data = call_tool(
            "sheet.get_cell_data",
            {
                "file_id": file_id,
                "sheet_id": sheet_id,
                "start_row": start,          # 0-based
                "end_row": end,              # 包含式
                "start_col": 0,              # 0-based，不能传 1（会吃掉序号列）
                "end_col": col_count - 1,    # 包含式
                "return_csv": False,         # cells 模式，不要 CSV
            },
        )
        cells = data.get("cells") or []
        for c in cells:
            rows.setdefault(int(c["row"]), {})[int(c["col"])] = c
        if not cells and start == 0:
            raise LedgerError(f"子表 {sheet_id} 读取到 0 个单元格，取数异常")
        start = end + 1

    header_cells = rows.get(0, {})
    header = [cell_text(header_cells.get(i)) for i in range(col_count)]
    return Sheet(header, rows)


def file_fingerprint(file_id: str) -> dict:
    """
    取文件的最后修改人/时间，用于只读性验证。

    用法：运行前取一次基线，运行后再取一次核对。两次一致 = 台账未被动过。
    """
    info = call_tool("manage.query_file_info", {"file_id": file_id})
    return {
        "title": info.get("title"),
        "last_modify_name": info.get("last_modify_name"),
        "last_modify_time": info.get("last_modify_time"),
    }
