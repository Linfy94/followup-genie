#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
定时口径必须全仓一致。

═══════════════════════════════════════════════════════════════════════
🔴 0.3.0-rc1 有三处口径，互相矛盾：

    README.md                 0 9 * * 1-5   （周一至周五）
    scripts/install.sh        0 9 * * *     （每天，含周末）
    docs/接一条新业务线.md      0 9 * * *
    生产上实际跑的             0 9 * * *

而 watchdog 的 count_weekends=false 是**按周一至周五设计的**：
它数「错过了几次本该执行的 9:00」时会跳过周末。
主任务每天推、监控器按工作日算，两边对不上 ——
周末真的漏跑了，监控器也不会数进去。

照着不同文档装的人会得到不同的行为，而这种不一致没有任何东西会报错。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import harness  # noqa: F401 —— 只为挂 sys.path

import _manifest  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

EXPECTED = "0 9 * * 1-5"

# 交付出去的文本文件。notes/ 是开发记录、不交付，历史里写的是当时的口径，
# 不该被这条规则约束。
SCANNED_SUFFIXES = (".md", ".sh", ".py", ".json", ".example", "")


def delivered_text_files():
    seen = []
    for name in _manifest.TOP_FILES:
        p = ROOT / name
        if p.is_file():
            seen.append(p)
    for name in _manifest.TOP_DIRS:
        d = ROOT / name
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*")):
            if not p.is_file():
                continue
            if any(_manifest.should_skip(part)
                   for part in p.relative_to(ROOT).parts):
                continue
            if p.suffix in SCANNED_SUFFIXES:
                seen.append(p)
    return seen


class ScheduleConsistencyTest(unittest.TestCase):

    # 抓所有形如 `0 9 * * X` 的 cron 表达式。
    # 星期字段只允许 cron 合法字符 —— 用 \S+ 会把结尾的引号一起吞进去，
    # 于是 `"0 9 * * 1-5"` 的星期字段变成 `1-5"`，正确的写法反被判成违规。
    CRON = re.compile(r"0\s+9\s+\*\s+\*\s+([0-9*\-,/]+)")

    def test_no_delivered_file_says_every_day(self):
        offenders = []
        for p in delivered_text_files():
            # 本文件的文档字符串里写着旧口径当反例，不能自己拿自己开刀
            if p.name == Path(__file__).name:
                continue
            for i, line in enumerate(
                    p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                for m in self.CRON.finditer(line):
                    if m.group(1) != "1-5":
                        offenders.append(f"{p.relative_to(ROOT)}:{i}  {line.strip()}")
        self.assertEqual(
            offenders, [],
            "🔴 定时口径不一致，README 说周一至周五，这些地方却不是：\n"
            + "\n".join(offenders))

    def test_readme_states_the_canonical_schedule(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(EXPECTED, text, "README 是口径的事实来源，必须写明")

    def test_installer_registers_the_same_schedule(self):
        text = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
        self.assertIn(EXPECTED, text,
                      "装出来的定时任务必须和 README 说的一致")

    def test_watchdog_default_matches_the_schedule(self):
        """
        主任务按周一至周五跑，监控器就必须按周一至周五数班次。
        两边不一致的话：周末漏跑监控器不算数，或者周末误报。
        """
        import watchdog
        self.assertFalse(watchdog.DEFAULTS["count_weekends"],
                         "cron 是 1-5，监控器不该把周末算成「本该跑」")
        self.assertEqual(watchdog.DEFAULTS["schedule_hour"], 9,
                         "cron 是 9 点，监控器的判定基准也该是 9 点")


if __name__ == "__main__":
    unittest.main()
