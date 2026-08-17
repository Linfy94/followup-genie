#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
定时口径必须全仓一致。

═══════════════════════════════════════════════════════════════════════
这条规则出现过两次口径变更，两次都是同一个教训：
**「哪天不该发」的判断放在 cron 表达式里，能表达的东西太少。**

0.3.0-rc1：三处文档互相矛盾（README 写周一至周五，install.sh 与
           接入文档写每天，生产上跑的是每天）。统一成了周一至周五。

0.4.0-rc1：周一至周五**排不掉法定节假日，也排不掉调休补班日**。
           国庆连休那五个工作日照发（0.3.0-rc4 已在脚本里补了闸门解决），
           而 2026 年那 6 个补班日（1/4、2/14、2/28、5/9、9/20、10/10）
           落在周六周日，**cron 根本不触发，业务在上班却收不到催办**。
           于是改回**每天跑**，由脚本按 config/holidays.json 判断：
           法定假日不发、普通周末不发、补班日照发。

           cron 只负责「每天叫醒一次」，「今天该不该发」全部交给脚本 ——
           因为只有脚本读得到节假日表。

🔴 监控器（watchdog）不用跟着改，这一点不显眼：
   它的 count_weekends=false 数的是「错过了几次**本该成功**的 9:00」。
   周末与法定假日主任务照跑、照写 last_full_success（只是不投递），
   所以周末不计入是对的，也不会误报。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import harness  # noqa: F401 —— 只为挂 sys.path

import _manifest  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

EXPECTED = "0 9 * * *"

# cron 的星期字段应该长什么样。改这里等于改产品行为，需人工评审。
EXPECTED_DOW = "*"

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

    def test_no_delivered_file_says_weekdays_only(self):
        """
        🔴 交付文件里不许再出现 `0 9 * * 1-5`。
        那个写法会让补班日（周六周日）根本不触发 —— 业务在上班却收不到催办，
        而且看起来和「今天没有要催的」一模一样。
        """
        offenders = []
        for p in delivered_text_files():
            # 本文件的文档字符串里写着旧口径当反例，不能自己拿自己开刀
            if p.name == Path(__file__).name:
                continue
            # CHANGELOG 同理：它**记述**的正是「当初改成这个写法、后来发现错了」，
            # 引用旧表达式是记录的一部分。为了绕过这条检查去改写历史记述，
            # 会让变更记录变得含糊 —— 那比留着这行字更糟。
            # 🔴 豁免只给这两个「以旧写法为叙述对象」的文件，别再往里加。
            if p.name == "CHANGELOG.md":
                continue
            for i, line in enumerate(
                    p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                for m in self.CRON.finditer(line):
                    if m.group(1) != EXPECTED_DOW:
                        offenders.append(f"{p.relative_to(ROOT)}:{i}  {line.strip()}")
        self.assertEqual(
            offenders, [],
            "🔴 定时口径不一致。cron 应该每天跑、由脚本判断当天是不是工作日，"
            "这些地方却把星期写死了：\n" + "\n".join(offenders))

    def test_readme_states_the_canonical_schedule(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(EXPECTED, text, "README 是口径的事实来源，必须写明")

    def test_installer_registers_the_same_schedule(self):
        text = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
        self.assertIn(EXPECTED, text,
                      "装出来的定时任务必须和 README 说的一致")

    def test_watchdog_still_ignores_weekends(self):
        """
        cron 改成每天之后，监控器**依然**不该把周末算成「本该跑」。

        乍看反直觉——主任务现在周末也跑。但监控器数的是
        「错过了几次本该**成功**的 9:00」，用来发现「任务根本没跑」。
        周末与法定假日主任务照跑、照写 last_full_success（只是不投递），
        所以周末不计入既不会漏报，也不会在周一早上误报
        （用户的 Mac 工作日不关机、周末可能关机）。

        真把 count_weekends 打开，周末关机就会攒出「错过 2 次」而告警。
        """
        import watchdog
        self.assertFalse(watchdog.DEFAULTS["count_weekends"],
                         "周末关机是常态，把周末算成「本该跑」会周一误报")
        self.assertEqual(watchdog.DEFAULTS["schedule_hour"], 9,
                         "cron 是 9 点，监控器的判定基准也该是 9 点")


if __name__ == "__main__":
    unittest.main()
