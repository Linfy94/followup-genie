#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
比对两次 `check_followup.py --dry-run --json` 的结果。

═══════════════════════════════════════════════════════════════════════
为什么要有这个东西：

改规则也好、升级代码也好，唯一能证明「改对了」的动作是
**改动前后的待催名单逐条比对** —— 它写在部署七步的第 4 步，
也写在每轮的验证清单里。但它一直靠肉眼：9 张台账、上千行，
于是它成了最容易被跳过的一步，而跳过它的代价是静默的：
名单少一条没有任何人会发现。

这个脚本只做一件事：把那个动作变成一条命令。

    python3 scripts/check_followup.py --dry-run --json > /tmp/before.json
    # …改配置或改代码…
    python3 scripts/check_followup.py --dry-run --json > /tmp/after.json
    python3 scripts/diff_due.py /tmp/before.json /tmp/after.json

退出码：0 无差异 ｜ 1 有差异 ｜ 2 参数或文件读不了

🔴 「有差异」不等于「出错了」。改判定口径的那几轮就是**故意**要有差异，
   那时这个脚本的价值在于让你看清**差异是不是你预期的那些**。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations  # 兼容 Python 3.9（macOS 自带版本）

import json
import sys
from pathlib import Path

# 每条待催的身份。key 是台账主键，node 是它卡在哪个节点 ——
# 同一个项目从①走到②是**换了一条待催**，不是同一条改了属性，
# 所以节点必须进身份，否则节点迁移会被显示成「超期天数变了」。
IDENTITY = ("key", "node")

# 计数里逐项比对的字段。少写一个就等于那一类的变化永远看不见。
COUNT_KEYS = ("due", "overdue_muted", "terminal", "paused", "advanced",
              "out_of_scope", "not_overdue", "no_node")


def die(msg: str) -> None:
    """参数或文件问题一律退出码 2 —— 与「有差异」的 1 分开，
    否则脚本没跑成会被误读成「查出了差异」。"""
    print(msg, file=sys.stderr)
    sys.exit(2)


def load(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as e:
        die(f"❌ 读不了 {path}：{e}")
    except json.JSONDecodeError as e:
        die(f"❌ {path} 不是合法 JSON：{e}\n"
            f"   （常见原因：把 --verbose 的日志和 --json 的输出混进了同一个文件。"
            f"日志走 stderr，重定向时只该收 stdout）")


def index(payload: dict) -> dict:
    """台账 id → {身份元组: 那条待催的完整内容}。"""
    out: dict = {}
    for led in payload.get("ledgers") or []:
        rows = {}
        for item in led.get("due") or []:
            rows[tuple(item.get(f) for f in IDENTITY)] = item
        out[led.get("id")] = {"name": led.get("name"), "rows": rows,
                              "counts": led.get("counts") or {},
                              "total_rows": led.get("total_rows"),
                              "warnings": led.get("warnings") or []}
    return out


def describe(item: dict) -> str:
    return (f"{item.get('key')} {item.get('name')}"
            f"｜{item.get('node')}｜超期 {item.get('overdue_days')} 天")


def main() -> int:
    if len(sys.argv) != 3:
        die("用法：diff_due.py <改前.json> <改后.json>\n"
            "  两份都由 check_followup.py --dry-run --json 生成。")

    before, after = load(sys.argv[1]), load(sys.argv[2])
    b_idx, a_idx = index(before), index(after)
    diffs = 0

    # 🔴 跨天比对必然出现「差异」，而那不是改动造成的：停滞天数天天在涨、
    #    节律到期日天天在变。不喊出来的话，人会把日期差当成改错了去查。
    if before.get("date") != after.get("date"):
        print(f"🔴 两次运行不是同一天（{before.get('date')} vs {after.get('date')}）。"
              f"待催名单本来就会随日期变，下面的差异**不能**直接当成改动的后果。\n")

    if before.get("failures") or after.get("failures"):
        print(f"⚠️  有台账读取失败，名单本身不完整："
              f"改前 {before.get('failures')} / 改后 {after.get('failures')}\n")

    for lid in sorted(set(b_idx) | set(a_idx)):
        b = b_idx.get(lid)
        a = a_idx.get(lid)
        if b is None or a is None:
            side = "只在改后有" if b is None else "只在改前有"
            print(f"🔴 台账 {lid}：{side} —— 配置里增删了台账，或某一份这次没读到")
            diffs += 1
            continue

        lines: list[str] = []

        added = sorted(set(a["rows"]) - set(b["rows"]))
        removed = sorted(set(b["rows"]) - set(a["rows"]))
        for k in added:
            lines.append(f"  ＋ 新增催办  {describe(a['rows'][k])}")
        for k in removed:
            # 🔴 「不再催」比「新增」危险得多：多催一条业务看得见，
            #    少催一条谁都看不见。所以它单独标出来。
            lines.append(f"  － 不再催办  {describe(b['rows'][k])}")

        for k in sorted(set(a["rows"]) & set(b["rows"])):
            bi, ai = b["rows"][k], a["rows"][k]
            for field in ("overdue_days", "stalled_days", "allowance_days",
                          "clock_from", "clock_source", "stage", "action"):
                if bi.get(field) != ai.get(field):
                    lines.append(f"  ~ {describe(ai)}：{field} "
                                 f"{bi.get(field)!r} → {ai.get(field)!r}")

        if b["total_rows"] != a["total_rows"]:
            lines.append(f"  ~ 总行数 {b['total_rows']} → {a['total_rows']}"
                         f"（台账本身被人改过，不是代码改动）")

        for ck in COUNT_KEYS:
            bv, av = b["counts"].get(ck), a["counts"].get(ck)
            if bv != av:
                lines.append(f"  ~ 去向计数 {ck}：{bv} → {av}")

        bw, aw = set(b["warnings"]), set(a["warnings"])
        for w in sorted(aw - bw):
            lines.append(f"  ＋ 新增警告  {w}")
        for w in sorted(bw - aw):
            lines.append(f"  － 消失警告  {w}")

        if lines:
            diffs += len(lines)
            print(f"── {lid}（{a['name']}）")
            print("\n".join(lines))
            print()

    if diffs == 0:
        print("✅ 无差异：待催名单、去向计数、警告三者逐条一致。")
        return 0
    print(f"⚠️  共 {diffs} 处差异。"
          f"若这次改的不是判定口径，那么一处差异都不该有。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
