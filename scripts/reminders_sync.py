#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
macOS 提醒事项同步（osascript + AppleScript，系统自带，零依赖）。

═══════════════════════════════════════════════════════════════════════
写入开关：output.json 的 reminders.write，默认 false。
  false（演练模式）→ 只把「本该创建/更新哪些提醒」打印到 stdout，
                     不调用任何 osascript 写入命令。
  true            → 真实创建/更新提醒。

所有写入类调用集中在 _run_applescript() 一个函数里，入口第一行检查开关。
不允许写入代码散落在多处。开关只能人工改，本脚本任何情况下不得改写它。
═══════════════════════════════════════════════════════════════════════

定位：提醒事项只做通知，不做状态。
  停催信号来自台账状态，不是业务勾没勾提醒。它的完成状态不参与任何判定。
  提醒被删除、被忽略、被勾选，都不影响判定正确性。
"""

from __future__ import annotations  # 兼容 Python 3.9（macOS 自带版本）

import subprocess
import sys
from datetime import date, timedelta

import core

MARKER = "⭕️"  # 标题前缀，用于一眼认出这条提醒是精灵建的


class WriteBlocked(Exception):
    """开关关闭时的哨兵，不该被外部看到。"""


def _osa(script: str) -> str:
    """
    osascript 执行底座。**只允许只读脚本经此直接调用**
    （查询列表、探测权限）。任何会改动用户数据的脚本必须走 _osa_write()。
    """
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError:
        raise RuntimeError("找不到 osascript —— 提醒事项写入只在 macOS 可用")
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "osascript 超时。首次运行可能在等自动化权限授权弹窗，"
            "请在系统设置 → 隐私与安全性 → 自动化里允许访问「提醒事项」"
        )
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        if "-1743" in err or "not allowed" in err.lower():
            raise RuntimeError(
                "被 macOS 自动化权限拒绝（TCC）。需在 系统设置 → 隐私与安全性 → "
                "自动化 中允许访问「提醒事项」。注意：终端里授权过不代表 cron "
                "运行时也通过，必须用 cron 的实际运行路径复验——这是静默失败。"
            )
        raise RuntimeError(f"osascript 失败：{err}")
    return (r.stdout or "").strip()


def _osa_write(script: str, *, write_enabled: bool) -> str:
    """
    唯一的写入入口。开关关闭时直接抛 WriteBlocked，绝不执行。

    新增任何"会在业务的提醒事项/日历里留下痕迹"的操作，都必须经过这里，
    不要直接调 _osa()。
    """
    if not write_enabled:
        raise WriteBlocked
    return _osa(script)


def _esc(s: str) -> str:
    """转义 AppleScript 字符串字面量。"""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _title(item) -> str:
    """
    提醒事项的标题。业务 2026-08-03 定的模板：

        ⭕️AI节能盒子「节能测试」星河示例科技有限公司

    它必须自包含（业务线 + 阶段 + 企业名），与企微清单的条目措辞不同 ——
    清单里有「业务线」「阶段」两级标题做上下文，条目可以只写企业名；
    而提醒事项是**一条一条独立弹出**的，看到时没有任何上下文。

    🔴 **标题里绝不能出现天数。** 天数每天都在涨，写进标题就等于每天换一个
    标题；而升级前的版本正是靠标题认回旧提醒的，那样会给同一个项目
    每天再建一条。天数放备注，每天刷新的是备注，标题保持不变。

    🔴 **但标题本身不是身份。** 同一家企业在同一条业务线上可以有多个项目
    （台账里是不同序号的两行），它们的「业务线＋阶段＋企业名」完全一样 ——
    靠标题匹配会把它们**合并成一条提醒**，业务只看到一个、漏掉另一个，
    而且没有任何迹象。真正的身份是 `item.state_key`
    （`台账|序号|节点`，与 followup_state / stage_entered 同一把钥匙），
    它与提醒事项自身 id 的对应关系记在 `state/reminder_map.json`。
    业务看到的标题因此保持业务定的模板，一个技术编号都不用往里塞。
    """
    return f"{MARKER}{item.line_label}「{item.stage}」{item.name}"


def _body(item) -> str:
    """
    备注。业务要求「不要太长」，所以严格只有两行：

        超期 158 天
        该做什么：催节能测试出报告

    天数写在这里而不是标题里，见 `_title()` 的说明 ——
    这一行每天被刷新，标题则保持不变，`_upsert` 才认得回来。
    """
    bits = [f"超期 {item.overdue_days} 天"]
    if item.action:
        bits.append(f"该做什么：{item.action}")
    return "\n".join(bits)


def _list_exists(name: str) -> bool:
    """只读查询，不受写入开关管辖。"""
    script = f'''
    tell application "Reminders"
        set found to false
        repeat with l in lists
            if name of l is "{_esc(name)}" then set found to true
        end repeat
        return found
    end tell
    '''
    return _osa(script).lower() == "true"


MAP_FILE = "reminder_map.json"


def _open_reminders(list_name: str) -> "list[tuple[str, str]]":
    """
    列出该列表里所有**未完成**的提醒，返回 [(id, 标题), ...]。只读，不受写入开关管辖。

    每行 `id<TAB>标题`。标题是单行文本，不含制表符与换行，所以按第一个
    制表符切开是安全的；真遇到异常行宁可跳过，也不要猜。
    """
    script = f'''
    tell application "Reminders"
        set out to ""
        repeat with r in (reminders of list "{_esc(list_name)}")
            if completed of r is false then
                set out to out & (id of r as string) & tab & (name of r as string) & linefeed
            end if
        end repeat
        return out
    end tell
    '''
    pairs = []
    for line in _osa(script).splitlines():
        if "\t" not in line:
            continue
        rid, title = line.split("\t", 1)
        if rid:
            pairs.append((rid, title))
    return pairs


def _upsert(list_name: str, title: str, body: str, due: date, rid: str, *,
            write_enabled: bool) -> "tuple[str, str]":
    """
    按 **提醒事项自身的 id** 创建或更新一条提醒。返回 (结果, id)。

    结果是 "created" 或 "updated"。

    🔴 这里**不做标题匹配**。标题不唯一（同企业同阶段的两个项目标题一样），
    靠它匹配会把两个项目合并成一条。认领旧提醒是 `sync()` 开头一次性做完的，
    做完之后每条 item 手里都有确定的 id 或确定没有 —— 写入阶段不再有歧义。

    rid 为空 = 这条从没建过；rid 指向的提醒已被业务删除或勾选完成 = 同样重建，
    并把新 id 记回映射（提醒只做通知、不承担状态，删了重建是正确行为）。
    """
    find = ""
    if rid:
        find = f'''
        repeat with r in (reminders of list "{_esc(list_name)}")
            if (id of r as string) is "{_esc(rid)}" and completed of r is false then
                set tgt to r
                exit repeat
            end if
        end repeat
        '''
    script = f'''
    tell application "Reminders"
        set tgt to missing value
        {find}
        if tgt is missing value then
            set newR to make new reminder at end of list "{_esc(list_name)}" with properties ¬
                {{name:"{_esc(title)}", body:"{_esc(body)}", ¬
                  due date:date "{due.strftime('%Y-%m-%d')}"}}
            return "created" & tab & (id of newR as string)
        else
            set body of tgt to "{_esc(body)}"
            set name of tgt to "{_esc(title)}"
            return "updated" & tab & (id of tgt as string)
        end if
    end tell
    '''
    raw = _osa_write(script, write_enabled=write_enabled)
    result, _, new_rid = raw.partition("\t")
    return result.strip(), new_rid.strip()


def _adopt_existing(items: list, mapping: dict, list_name: str) -> int:
    """
    把升级前建好的旧提醒认领进映射，避免升级当天**整批重建一遍**。

    旧版本靠标题匹配，所以现存提醒的标题正好等于我们现在会生成的标题。
    按标题认一次即可，认完之后一律走 id。

    🔴 一个 id 只能被认领一次（`claimed`）。少了这一条，同企业同阶段的
    两个项目会双双认领同一条旧提醒 —— 两个键指向同一个 id，
    每天互相覆盖备注，业务永远只看得到一条。这正是本次要修的病，
    在认领这一步同样会犯。第二个项目认不到就会新建，恰好把历史遗留的
    合并状态自动拆开。

    返回认领了几条。
    """
    unmapped = [it for it in items if not mapping.get(it.state_key)]
    if not unmapped:
        return 0
    claimed = set(mapping.values())
    by_title: dict = {}
    for rid, title in _open_reminders(list_name):
        if rid not in claimed:
            by_title.setdefault(title, []).append(rid)
    adopted = 0
    for it in unmapped:
        bucket = by_title.get(_title(it))
        if bucket:
            mapping[it.state_key] = bucket.pop(0)
            adopted += 1
    return adopted


def sync(items: list, output_cfg: dict, today: date, stream=None) -> None:
    """
    把今天要催的单同步到提醒事项。

    开发机上（write=false）只打印本该做什么，不产生任何真实提醒 ——
    这是开发期的验收标准：跑完一轮，自己的提醒事项 App 里零新增条目。

    stream：诊断输出去哪。默认 stdout（文本模式下它是报告的一部分）；
            调用方在 --json 模式下要传 sys.stderr，否则会污染 JSON 输出，
            让下游程序解析失败。
    """
    out = stream or sys.stdout
    write_enabled = core.reminders_write_enabled(output_cfg)
    list_name = (output_cfg.get("reminders") or {}).get("list_name") or "项目跟进精灵"

    if not items:
        return

    if not write_enabled:
        print(file=out)
        print(f"🔒 演练模式（reminders.write=false）—— 本该在列表「{list_name}」里"
              f"创建/更新 {len(items)} 条提醒：", file=out)
        for it in items:
            print(f"   · {_title(it)}｜到期 {today.isoformat()}", file=out)
        print("   （未调用任何 osascript 写入命令）", file=out)
        return

    if sys.platform != "darwin":
        print("⚠️ 提醒事项写入只在 macOS 可用，已跳过", file=sys.stderr)
        return

    try:
        if not _list_exists(list_name):
            # 不自动建列表 —— 让业务自己在提醒事项里建，避免程序在她的
            # 个人数据里创建她没预期的东西
            print(f"⚠️ 提醒事项里没有名为「{list_name}」的列表，请先手动创建",
                  file=sys.stderr)
            return
    except (RuntimeError, WriteBlocked) as e:
        print(f"⚠️ 提醒事项不可用：{e}", file=sys.stderr)
        return

    mapping = core.read_state(MAP_FILE) or {}
    try:
        adopted = _adopt_existing(items, mapping, list_name)
    except (RuntimeError, WriteBlocked) as e:
        # 认领失败不该让整轮同步停下 —— 最坏结果是这次全部当新的建，
        # 而那正是升级前的行为，不是倒退。
        print(f"⚠️ 读取现有提醒失败，本轮按新建处理：{e}", file=sys.stderr)
        adopted = 0

    created = updated = failed = 0
    for it in items:
        try:
            r, rid = _upsert(list_name, _title(it), _body(it), today,
                             mapping.get(it.state_key, ""), write_enabled=True)
            if rid:
                mapping[it.state_key] = rid
            if r == "created":
                created += 1
            else:
                updated += 1
        except (RuntimeError, WriteBlocked) as e:
            failed += 1
            if failed == 1:
                print(f"⚠️ 提醒写入失败：{e}", file=sys.stderr)

    # 映射只增不减。哪怕某个项目今天不催了，也留着它的 id ——
    # 明天它再超期时才认得回原来那条，不会重建。
    core.write_state(MAP_FILE, mapping)

    print(f"\n📌 提醒事项：新建 {created}、更新 {updated}"
          + (f"、认领旧提醒 {adopted}" if adopted else "")
          + (f"、失败 {failed}" if failed else ""), file=out)


def probe() -> tuple[bool, str]:
    """
    TCC 权限探测，供 doctor 使用。不创建任何东西，只读列表数量。

    注意：这个探测本身也受 TCC 管辖，所以「探测通过」才说明权限就绪。
    必须在 cron 的实际运行路径下跑，终端里通过不代表 cron 通过。
    """
    if sys.platform != "darwin":
        return False, "非 macOS，提醒事项通道不可用"
    try:
        # 只读脚本，走 _osa。TCC 权限对读写是同一道闸，读得到就说明权限已授予。
        out = _osa('tell application "Reminders" to return (count of lists) as string')
        return True, f"可访问提醒事项（{out} 个列表）"
    except RuntimeError as e:
        return False, str(e)
