#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
超期天数口径（业务 2026-07-31 提出）。

业务原话：「安装日+21天才开始启动提醒，**超出 21 天才开始算停滞日期**」。

前半句本来就是现状。她说的是后半句 —— 显示的那个数字。
旧口径显示「在这个节点待了多久」，新口径显示「超出允许期多久」。

她的理由比字面更强：旧口径下「节能测试 24 天」和「待收资 29 天」看着差不多，
实际一个超期 3 天、一个超期 22 天 —— **同一个数字在不同阶段含义完全不同，
没法横向比较**。

═══════════════════════════════════════════════════════════════════════
🔴 本文件最重要的一条是 SemanticsOnlyTest：**证明只动了显示，没动判定。**

判定逻辑是这个工具唯一不能出错的部分。一次「只是改个数字」的需求，
如果顺手动了 is_overdue，业务会在完全不知情的情况下漏催或多催。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import re
import unittest
from datetime import date, timedelta

from harness import (make_sheet, row, temp_home, run_main, rules_cfg,
                     output_cfg, check_followup)

import core  # noqa: E402 —— 必须在 harness 之后

TODAY = date(2026, 7, 20)


def nodes() -> list[dict]:
    return rules_cfg()["rulesets"]["box"]["nodes"]


def node(nid: str) -> dict:
    return next(n for n in nodes() if n["id"] == nid)


class AllowanceTest(unittest.TestCase):
    """允许天数 = 首次提醒日 − 1。两种边界都要对。"""

    def test_after_boundary(self):
        # ④节能测试 days=21 boundary=after → 首提第 22 天 → 允许 21
        self.assertEqual(core.first_reminder_day(node("efficiency_test")), 22)
        self.assertEqual(core.allowance_days(node("efficiency_test")), 21)

    def test_on_boundary(self):
        # ①收资 days=7 boundary=on → 首提第 7 天 → 允许 6
        self.assertEqual(core.first_reminder_day(node("collect")), 7)
        self.assertEqual(core.allowance_days(node("collect")), 6)

    def test_never_negative(self):
        self.assertEqual(core.allowance_days({"threshold": {"days": 0,
                                                            "boundary": "on"}}), 0)
        self.assertEqual(core.allowance_days({}), 0)

    def test_overdue_never_goes_below_zero(self):
        it = core.Item("box", "盒子", "1", "甲", "n", "①收资", 3, TODAY,
                       "x", "", allowance=6)
        self.assertEqual(it.overdue_days, 0, "不许出现负数天")


class FirstReminderShowsOneDayTest(unittest.TestCase):
    """
    🔴 **每个节点在首次提醒那天，超期天数必须正好是 1。**

    这正是允许天数取 `first_reminder_day - 1` 而不是 `threshold.days` 的原因：
    ①收资 是 boundary=on，按 threshold.days 算首推那天正好是 0，
    而一条刚刚触发的催办显示「超期 0 天」，业务会当成 bug。
    """

    CASES = [
        # (节点 id, 造数用的字段, 首次提醒是第几天)
        ("collect", dict(tech="待收资"), 7),
        ("expert", dict(tech=""), 4),
        ("install", dict(tech="可行", install=""), 22),
        ("efficiency_test", dict(tech="可行", install="完成", test=""), 22),
    ]

    def test_every_node_shows_one_on_its_first_reminder_day(self):
        for nid, fields, day in self.CASES:
            with self.subTest(node=nid):
                start = TODAY - timedelta(days=day)
                sheet = make_sheet([row(1, "甲公司", reported=start,
                                        progress=start, **fields)])
                with temp_home():
                    r = run_main([f"--today={TODAY}", "--dry-run"], sheet)
                    self.assertEqual(r.code, 0, r.err)
                    self.assertIn("超期 1 天", r.out,
                                  f"{nid} 首次提醒当天应显示「超期 1 天」，"
                                  f"实际输出：\n{r.out}")

    def test_day_before_is_not_due_at_all(self):
        for nid, fields, day in self.CASES:
            with self.subTest(node=nid):
                start = TODAY - timedelta(days=day - 1)
                sheet = make_sheet([row(1, "甲公司", reported=start,
                                        progress=start, **fields)])
                with temp_home():
                    r = run_main([f"--today={TODAY}", "--dry-run"], sheet)
                    self.assertNotIn("超期", r.out, f"{nid} 早一天不该催")


class SemanticsOnlyTest(unittest.TestCase):
    """
    🔴 **口径改动只碰显示，不碰判定。**

    用同一份假表，断言「谁被催、什么时候开始被催」与阈值定义完全吻合 ——
    这些全部来自 is_overdue()，而 is_overdue() 用的仍是 stalled_days。
    """

    def test_trigger_day_is_unchanged_for_every_node(self):
        for nid, fields, day in FirstReminderShowsOneDayTest.CASES:
            for n, expect_due in ((day - 1, False), (day, True), (day + 1, True)):
                with self.subTest(node=nid, days=n):
                    start = TODAY - timedelta(days=n)
                    sheet = make_sheet([row(1, "甲公司", reported=start,
                                            progress=start, **fields)])
                    with temp_home():
                        r = run_main([f"--today={TODAY}", "--dry-run"], sheet)
                        got = "甲公司" in r.out
                        self.assertEqual(
                            got, expect_due,
                            f"{nid} 在节点 {n} 天：应催={expect_due} 实际={got}。"
                            f"触发时机变了就说明动到了判定逻辑")

    def test_stalled_days_still_counts_from_the_node_entry(self):
        """内部的 stalled_days 仍是「在节点待了多久」，没被改成超期天数。"""
        start = TODAY - timedelta(days=40)
        sheet = make_sheet([row(1, "甲公司", tech="可行", install="",
                                reported=start, progress=start)])
        with temp_home():
            r = run_main([f"--today={TODAY}", "--json", "--dry-run"], sheet)
            d = json.loads(r.out)
            it = d["ledgers"][0]["due"][0]
            self.assertEqual(it["stalled_days"], 40, "在节点 40 天")
            self.assertEqual(it["allowance_days"], 21)
            self.assertEqual(it["overdue_days"], 19, "超期 19 天")
            self.assertEqual(it["clock_from"], start.isoformat(),
                             "起点仍是节点进入日，没被挪成「应完成日」")


class JsonExposesBothTest(unittest.TestCase):
    def test_both_numbers_present_and_consistent(self):
        sheet = make_sheet([
            row(1, "甲公司", tech="待收资", reported=TODAY - timedelta(days=30),
                progress=TODAY - timedelta(days=30)),
            row(2, "乙公司", tech="可行", install="",
                reported=TODAY - timedelta(days=50),
                progress=TODAY - timedelta(days=50)),
        ])
        with temp_home():
            r = run_main([f"--today={TODAY}", "--json"], sheet)
            d = json.loads(r.out)
            due = d["ledgers"][0]["due"]
            self.assertEqual(len(due), 2)
            for it in due:
                self.assertEqual(
                    it["stalled_days"] - it["allowance_days"], it["overdue_days"],
                    "三个数必须自洽，否则下游只能自己反推")


class RenderersAgreeTest(unittest.TestCase):
    """两套渲染必须给出同一个数字 —— 防止只改了一边。"""

    def test_terminal_and_wecom_show_the_same_overdue_numbers(self):
        sheet = make_sheet([
            row(i, f"公司{d}", tech="待收资",
                reported=TODAY - timedelta(days=d),
                progress=TODAY - timedelta(days=d))
            for i, d in enumerate([30, 45, 70], start=1)
        ])
        cfg = output_cfg()
        import qqdoc
        from unittest import mock
        from harness import ledgers_cfg
        with mock.patch.object(qqdoc, "read_sheet", lambda *a: sheet):
            wd = core.WorkdayCalc(rules_cfg()["workday"], None)
            rep, _ = core.evaluate_ledger(
                ledgers_cfg()["ledgers"][0], rules_cfg()["rulesets"]["box"],
                wd, TODAY, {}, {}, {}, {})
        md = check_followup.render_wecom([rep], TODAY, cfg)
        txt = check_followup.render([rep], TODAY, False, cfg)

        def nums(t):
            return re.findall(r"超期 (\d+) 天", t)

        self.assertEqual(nums(md), nums(txt))
        self.assertEqual(nums(txt), ["24", "39", "64"], "30/45/70 天 − 允许 6 天")
        self.assertNotIn("停滞", md, "措辞要跟着数字一起改，不能字面说停滞")
        self.assertNotIn("停滞", txt)


class TerminalHintUsesOverdueTest(unittest.TestCase):
    """业务确认：60 天这条线按超期天数算。"""

    HINT = "超2个月 可考虑终止"

    def _out(self, days: int) -> str:
        sheet = make_sheet([row(1, "甲公司", tech="待收资",
                                reported=TODAY - timedelta(days=days),
                                progress=TODAY - timedelta(days=days))])
        with temp_home():
            return run_main([f"--today={TODAY}", "--dry-run"], sheet).out

    def test_sixty_in_node_is_not_enough_anymore(self):
        """旧口径下 60 天出线，新口径下不出 —— 这是刻意的行为变化。"""
        out = self._out(60)
        self.assertIn("超期 54 天", out)
        self.assertNotIn(self.HINT, out)

    def test_sixty_six_in_node_crosses_the_line(self):
        out = self._out(66)
        self.assertIn("超期 60 天", out)
        self.assertIn(self.HINT, out)


if __name__ == "__main__":
    unittest.main()
