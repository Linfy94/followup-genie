#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
企业微信在线表格只读取数层。

═══════════════════════════════════════════════════════════════════════
铁律同 qqdoc.py / lark_base.py：本模块只暴露读取能力，不实现任何写操作。

🔴 这一条在企微这边比另外两个数据源更要紧。`wecom-cli doc` 底下实测有
   25 个子命令，其中 sheet_update_range_data、sheet_append_data、
   sheet_delete_sub、smartsheet_delete_records、smartsheet_delete_fields、
   smartsheet_delete_sheet、create_doc、edit_doc_content 全都**能改能删**。
   白名单里只有两个只读命令，其余永远不进 —— 与 sheet.operation_sheet 同等对待。
═══════════════════════════════════════════════════════════════════════

不重新实现企微 OAuth——本机已有的 wecom-cli 做好了。这里 subprocess 调它、
解析 Markdown，包成跟 qqdoc.Sheet 同样接口（header / data_rows / text /
date / has_column）的对象，core.py 不用为企微另写一套判定逻辑。
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import date, datetime

import cli_env
from qqdoc import LedgerError

ALLOWED_SUBCOMMANDS = frozenset({"sheet_get_info", "get_doc_content"})

TIMEOUT = 60
POLL_MAX = 20            # 实测 5~6 次就 task_done，20 次是宽裕的上限
POLL_INTERVAL = 2.0
POLL_BUDGET = 120.0      # 🔴 总时长闸门：cron 里绝不能挂死

_DATE_FORMATS = ("%Y/%m/%d", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S")

# 同一份文档只取一次：美誉度那份要供 AI体检、GEO 两个台账用，
# 而一次 get_doc_content 要轮询十几秒。不缓存等于每天多跑一遍。
_INFO_CACHE: dict[str, dict] = {}
_CONTENT_CACHE: dict[str, str] = {}


def clear_cache() -> None:
    """给测试和 doctor 用；正常运行一个进程内只取一次。"""
    _INFO_CACHE.clear()
    _CONTENT_CACHE.clear()


def wecom_cli_bin() -> str | None:
    return cli_env.find_bin("wecom-cli")


def _child_env(exe: str) -> dict:
    """
    🔴 实测 wecom-cli 的 shebang 同样是 `#!/usr/bin/env node`、同样装在
       ~/.local/bin —— 坑了 rc2 和 rc4 两轮的 Agent 上下文变量泄漏
       在这里会原样重现，而症状是「今天没有要催的」、退出码 0。
       实现与 lark_base 共用 cli_env 那一份。
    """
    return cli_env.child_env(exe)


def _missing_cli_message() -> str:
    # 🔴 **绝不自动安装。** 装一个命令行工具会改动这台电脑的全局环境，
    #    该由人来决定。话要说得让没有开发背景的人也能照着做。
    return (
        "读不了企业微信文档台账：这台电脑上找不到 wecom-cli。\n"
        "\n"
        "wecom-cli 是读企业微信在线文档的命令行工具。\n"
        "只有配置了企微文档台账才需要它；只用腾讯文档和飞书的话，不用装。\n"
        "\n"
        "已经找过这些位置，都没有：PATH、~/.local/bin、~/.hermes/bin、"
        "/opt/homebrew/bin、/usr/local/bin。\n"
        "\n"
        "🔴 如果你在终端里敲 `which wecom-cli` 明明是有的，那就不是没装，"
        "而是定时任务能看到的目录比你终端里少。"
        "把它装到上面任意一个位置，或者在定时任务的配置里补上它所在的目录。"
    )


def _run_cli(subcommand: str, payload: dict) -> dict:
    """
    调只读白名单内的一个 wecom-cli doc 子命令，返回**业务层**的结果字典。

    🔴 三层信封，每一层都能藏错误，漏看任何一层都会把「读不到」变成「0 行」：
       ① 进程退出码
       ② JSON-RPC 的 result.content[0].text（真正的响应是这里的**字符串**）
       ③ 那个字符串解出来的 errcode
    """
    if subcommand not in ALLOWED_SUBCOMMANDS:
        raise LedgerError(
            f"wecom_doc 只允许调用只读子命令 {sorted(ALLOWED_SUBCOMMANDS)}，"
            f"收到 {subcommand!r}（拒绝调用，这不是可以放宽的检查）"
        )
    exe = wecom_cli_bin()
    if exe is None:
        raise LedgerError(_missing_cli_message())

    cmd = [exe, "doc", subcommand, "--json", json.dumps(payload, ensure_ascii=False)]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=TIMEOUT,
            env=_child_env(exe),
        )
    except FileNotFoundError as e:
        raise LedgerError(
            f"wecom-cli 存在于 {exe} 但无法执行（权限不足或链接已断）") from e
    except subprocess.TimeoutExpired as e:
        raise LedgerError(f"wecom-cli 调用超时（{TIMEOUT}s）：{subcommand}") from e

    raw = proc.stdout if proc.returncode == 0 else (proc.stderr or proc.stdout)
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as e:
        raise LedgerError(
            f"wecom-cli 返回不是合法 JSON（{subcommand}）：{raw[:300]}") from e

    if envelope.get("error"):
        raise LedgerError(
            f"wecom-cli 调用失败（{subcommand}）：{envelope['error']}")

    try:
        text = envelope["result"]["content"][0]["text"]
    except (KeyError, IndexError, TypeError) as e:
        raise LedgerError(
            f"wecom-cli 返回结构不认识（{subcommand}）：{raw[:300]}") from e

    try:
        body = json.loads(text)
    except json.JSONDecodeError as e:
        raise LedgerError(
            f"wecom-cli 的响应体不是合法 JSON（{subcommand}）：{text[:300]}") from e

    # 🔴 这一句是整个模块最重要的一行。
    #    实测拿不到权限时，JSON-RPC 外层是 `"isError": false`，错误只藏在
    #    这个 errcode 里（851008 = 机器人缺「获取成员文档内容」能力授权）。
    #    只看 isError 会把「没权限」读成「0 行」——**正好伪装成「今天没有
    #    要催的」**，而那也正是授权过期时的表现。授权会不会 7 天过期至今
    #    没有答案，这一句就是那个未知数的兜底。
    code = body.get("errcode")
    if code not in (0, None):
        guidance = {
            851008: (
                "这是智能机器人缺「获取成员文档内容」能力授权。"
                "请由机器人管理员在企业微信「工作台 → 智能机器人」开通该能力；"
                "不要改用群机器人 Webhook。"
            ),
            851003: (
                "这是机器人对目标文档没有对象权限。10 人以上企业中，机器人是独立身份，"
                "不会继承业务人员的文档权限。请让文档所有者或企业微信管理员确认能否把"
                "这份文档显式授权给该智能机器人；重新扫码、换分享链接或给业务人员加协作者都不能替代它。"
            ),
        }.get(code, "请保留 errcode 与 errmsg，按 docs/企业微信文档接入.md 的错误对照表处理；不要盲目重新扫码。")
        if code == 851002:
            if subcommand == "get_doc_content":
                guidance = (
                    "文档结构可读，但正文读取接口不兼容该文档类型。当前适配器无法继续读取；"
                    "请停止接入并保留错误，不要修改 sheet_id 或反复授权。"
                )
            else:
                guidance = (
                    "这是链接或文档类型不兼容。请确认 source=wecom_doc 对应企业微信在线表格，"
                    "并核对完整 URL；此时不要先改权限。"
                )
        raise LedgerError(
            f"企微文档接口报错（{subcommand}）：errcode={code} "
            f"errmsg={body.get('errmsg')!r}。"
            f"{guidance}"
            f"这不是「今天没有要催的」，是读不到数据。"
        )
    return body


def doc_info(url: str) -> dict:
    """文档结构：名称 + 各子表的 sheet_id / title / 行列数。"""
    if url not in _INFO_CACHE:
        _INFO_CACHE[url] = _run_cli("sheet_get_info", {"url": url})
    return _INFO_CACHE[url]


def doc_content(url: str) -> str:
    """
    整份文档的 Markdown。**异步轮询**，必须有次数与总时长两道闸门 ——
    cron 里挂死比读不到更糟：它连「失败」都表现不出来。
    """
    if url in _CONTENT_CACHE:
        return _CONTENT_CACHE[url]

    body = _run_cli("get_doc_content", {"url": url, "type": 2})
    task_id = body.get("task_id")
    started = time.monotonic()
    tries = 1
    while not body.get("task_done"):
        if tries >= POLL_MAX or (time.monotonic() - started) > POLL_BUDGET:
            raise LedgerError(
                f"企微文档内容轮询超时（{tries} 次 / "
                f"{time.monotonic() - started:.0f}s）：{url}。"
                f"这不是「今天没有要催的」，是没取到数据。"
            )
        time.sleep(POLL_INTERVAL)
        payload = {"url": url, "type": 2}
        if task_id:
            payload["task_id"] = task_id
        body = _run_cli("get_doc_content", payload)
        task_id = body.get("task_id") or task_id
        tries += 1

    _CONTENT_CACHE[url] = body.get("content") or ""
    return _CONTENT_CACHE[url]


def _split_by_sheet(markdown: str, titles: list[str]) -> dict[str, list[str]]:
    """
    把整份文档的 Markdown 按子表切开，返回 {子表标题: 表格行列表}。

    🔴 **不能用「不以 `|` 开头就是新子表」这条规则。** 实测正文里还有独立
       成行的图片 `![](https://wdcdn.qpic.cn/...)`，那样切会把图片行当成
       一个子表、后面整段错位，而且不报错 —— 只是某条业务线的数据静默变形。
       必须拿 sheet_get_info 给的 title 做白名单。
    """
    wanted = set(titles)
    out: dict[str, list[str]] = {}
    current: str | None = None
    for line in markdown.split("\n"):
        stripped = line.strip()
        if stripped in wanted:
            current = stripped
            out.setdefault(current, [])
            continue
        if current is not None and stripped.startswith("|"):
            out[current].append(stripped)
    return out


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator(line: str) -> bool:
    """Markdown 表格的 |---|---| 分隔行。"""
    cells = _cells(line)
    return bool(cells) and all(set(c) <= {"-", ":"} and c for c in cells)


def _cell_date(s: str) -> date | None:
    """
    认得出的日期写法才返回日期，认不出就返回 None ——**不猜**。

    🔴 GEO 那张表里写的是 `6.12`（没有年份）。猜一个年份出来会在跨年时
       静默算错，而催办天数错了是看不出来的。没有年份的起点靠配置里的
       播种表（manual_stage_entered）给，不在这里编。
    """
    s = (s or "").strip()
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
    一张企微在线表格的只读视图，接口跟 qqdoc.Sheet / lark_base.Sheet 对齐
    （header / data_rows / has_column / text / date），core.py 不用区分数据源。
    """

    def __init__(self, header: list[str], rows: dict[int, dict]):
        self.header = header
        self._rows = rows
        # 重复列名保留第一个并记下来，与 qqdoc 同口径（core 会打数据质量警告）。
        self.duplicate_columns: list[str] = []
        seen: set[str] = set()
        for name in header:
            if not name:
                continue
            if name in seen:
                if name not in self.duplicate_columns:
                    self.duplicate_columns.append(name)
            seen.add(name)
        self._col_set = seen

    @property
    def data_rows(self) -> list[int]:
        return sorted(self._rows.keys())

    def has_column(self, name: str) -> bool:
        return name in self._col_set

    def text(self, row: int, col_name: str) -> str:
        return str(self._rows.get(row, {}).get(col_name, "") or "").strip()

    def date(self, row: int, col_name: str) -> date | None:
        return _cell_date(self.text(row, col_name))


def read_sheet(url: str, sheet_id: str) -> Sheet:
    """
    读一份企微在线表格里的某个子表。

    🔴 必须 `url + sheet_id` 成对定位。实测两份不同文档里都有 sheet_id
       `BB08J2` 的子表 —— sheet_id 只在文档内唯一，光凭它会串表。
    """
    info = doc_info(url)
    sheets = info.get("sheets") or []
    titles = [s.get("title", "") for s in sheets]
    match = next((s for s in sheets if s.get("sheet_id") == sheet_id), None)
    if match is None:
        raise LedgerError(
            f"企微文档《{info.get('name')}》里没有 sheet_id={sheet_id!r} 的子表。"
            f"现有的是：{[(s.get('sheet_id'), s.get('title')) for s in sheets]}"
        )

    title = match.get("title", "")
    blocks = _split_by_sheet(doc_content(url), titles)
    lines = blocks.get(title) or []
    if not lines:
        raise LedgerError(
            f"企微文档《{info.get('name')}》的子表「{title}」在正文里一行表格都没有。"
            f"这不是「今天没有要催的」，是解析对不上 —— "
            f"子表标题可能被改过，或返回格式变了。"
        )

    header = _cells(lines[0])
    body = [ln for ln in lines[1:] if not _is_separator(ln)]
    rows: dict[int, dict] = {}
    for i, line in enumerate(body, start=1):
        cells = _cells(line)
        row: dict[str, str] = {}
        for name, value in zip(header, cells):
            # 重复列名取第一个（与 qqdoc 同口径）
            if name and name not in row:
                row[name] = value
        rows[i] = row
    return Sheet(header, rows)


def check_credential(url: str) -> None:
    """
    最轻量的只读探针：确认机器人能读到这份文档。给 doctor.py 用。

    🔴 注意 `sheet_get_info` **不返回修改时间**（顶层只有 errcode/errmsg/
       name/sheets/url）。所以企微线做不了真正的只读性验证，与飞书一样
       只能靠上面的命令白名单兜底。doctor 里要把这句明说出来。
    """
    doc_info(url)
