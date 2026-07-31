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

MARKER = "跟进精灵"  # 写进提醒备注，用于下次运行时认回自己创建的提醒


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
    提醒事项的标题。

    🔴 这里必须是完整自包含的句子，与 telegram 清单的措辞不同——
    telegram 里有「业务线」「阶段」两级标题做上下文，条目可以简写；
    而提醒事项是**一条一条独立弹出**的，业务看到时没有任何上下文，
    标题不自己说清楚"谁的什么项目卡在哪一步多久了"就看不懂。
    """
    return (
        f"{item.name}的{item.line_label}项目，"
        f"已在「{item.stage}」阶段超期{item.overdue_days}天"
    )


def _body(item, today: date) -> str:
    # 备注里**两个天数都留**：标题只能放一个数，而排查时真正要看的是
    # 「它在这个节点待了多久、允许多久、起点是哪天、起点怎么来的」。
    # 备注是她自己查细节的地方，信息多不碍事 —— 与清单要「减少信息长度」不矛盾。
    bits = [
        f"{MARKER} · {item.line}",
        f"超期 {item.overdue_days} 天"
        f"（在本节点 {item.stalled_days} 天，允许 {item.allowance} 天）",
        f"起点 {item.clock_from.isoformat()}，来源：{item.clock_source}",
    ]
    if item.action:
        bits.append(f"该做什么：{item.action}")
    bits.append(f"台账序号：{item.key}")
    bits.append(f"生成于 {today.isoformat()}")
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


def _upsert(list_name: str, title: str, body: str, due: date, *, write_enabled: bool) -> str:
    """
    创建或更新一条提醒。同一标题只保留一条 —— 首次推送几十条、之后每天再跑，
    若重复创建，提醒事项列表两周内就没法用了。
    """
    script = f'''
    tell application "Reminders"
        set tgt to missing value
        repeat with r in (reminders of list "{_esc(list_name)}")
            if name of r is "{_esc(title)}" and completed of r is false then
                set tgt to r
                exit repeat
            end if
        end repeat
        if tgt is missing value then
            make new reminder at end of list "{_esc(list_name)}" with properties ¬
                {{name:"{_esc(title)}", body:"{_esc(body)}", ¬
                  due date:date "{due.strftime('%Y-%m-%d')}"}}
            return "created"
        else
            set body of tgt to "{_esc(body)}"
            return "updated"
        end if
    end tell
    '''
    return _osa_write(script, write_enabled=write_enabled)


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

    created = updated = failed = 0
    for it in items:
        try:
            r = _upsert(list_name, _title(it), _body(it, today), today, write_enabled=True)
            if r == "created":
                created += 1
            else:
                updated += 1
        except (RuntimeError, WriteBlocked) as e:
            failed += 1
            if failed == 1:
                print(f"⚠️ 提醒写入失败：{e}", file=sys.stderr)
    print(f"\n📌 提醒事项：新建 {created}、更新 {updated}"
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
