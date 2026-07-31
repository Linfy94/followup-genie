#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P1-C：阈值边界。

「满7天应提醒」到底是第 7 天还是第 8 天？光看 threshold.days 看不出来，
而差一天就是差一天的催办。口径（业务 2026-07-31 确认）：

  ①收资   原文「自上报日起满1周应提醒」   → boundary=on    → 第 7 天
  ②专家   原文「3天内有结果，没有则提醒」 → boundary=after → 第 4 天
  ③④     原文「3周内…没有则提醒」        → boundary=after → 第 22 天
  ⑤汇报   原文「1个月内」按 30 天算       → boundary=after → 第 31 天
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from harness import (make_sheet, row, temp_home, run_main, rules_cfg, core)

D = date(2026, 3, 1)


def at(n: int) -> date:
    return D + timedelta(days=n)


def due_names(sheet, today) -> set[str]:
    """跑一次，返回今天被催的企业名。用 --dry-run 保证不发不写。"""
    with temp_home():
        r = run_main([f"--today={today}", "--dry-run"], sheet)
        assert r.code == 0, r.err
        return {ln.split("、", 1)[1].split(" — ")[0]
                for ln in r.out.splitlines()
                if ln and ln[0].isdigit() and "、" in ln and " — 超期 " in ln}


class BoundaryTest(unittest.TestCase):

    def test_collect_boundary_on_fires_on_day_seven(self):
        """①收资：6 天不催、**7 天就催**、8 天照催。"""
        for n, expect in ((6, False), (7, True), (8, True)):
            sheet = make_sheet([row(1, "甲公司", tech="待收资",
                                    reported=at(0), progress=at(0))])
            got = "甲公司" in due_names(sheet, at(n))
            self.assertEqual(got, expect,
                             f"停滞 {n} 天时，①收资 应催={expect} 实际={got}")

    def test_expert_boundary_after_fires_on_day_four(self):
        """②专家评估：原文是「3天内没有才提醒」→ 第 4 天。"""
        for n, expect in ((2, False), (3, False), (4, True)):
            sheet = make_sheet([row(1, "甲公司", tech="",
                                    reported=at(0), progress=at(0))])
            got = "甲公司" in due_names(sheet, at(n))
            self.assertEqual(got, expect,
                             f"停滞 {n} 天时，②专家评估 应催={expect} 实际={got}")

    def test_install_boundary_after_fires_on_day_22(self):
        for n, expect in ((20, False), (21, False), (22, True)):
            sheet = make_sheet([row(1, "甲公司", tech="可行", install="",
                                    reported=at(0), progress=at(0))])
            got = "甲公司" in due_names(sheet, at(n))
            self.assertEqual(got, expect,
                             f"停滞 {n} 天时，③预调试/安装 应催={expect} 实际={got}")

    def test_efficiency_boundary_after_fires_on_day_22(self):
        for n, expect in ((20, False), (21, False), (22, True)):
            sheet = make_sheet([row(1, "甲公司", tech="可行", install="完成",
                                    test="", reported=at(0), progress=at(0))])
            got = "甲公司" in due_names(sheet, at(n))
            self.assertEqual(got, expect,
                             f"停滞 {n} 天时，④节能测试 应催={expect} 实际={got}")

    def test_report_node_30_days_fires_on_day_31(self):
        """⑤汇报按 30 天算（不用自然月：会引入月末与闰年边界）。"""
        node = {"threshold": {"days": 30, "boundary": "after"}}
        for n, expect in ((29, False), (30, False), (31, True)):
            self.assertEqual(core.is_overdue(n, node), expect,
                             f"停滞 {n} 天，期望 {expect}")


class FirstReminderDayTest(unittest.TestCase):
    """doctor 打印的边界表和判定必须用同一个算法，不能各算各的。"""

    def test_on_and_after(self):
        self.assertEqual(core.first_reminder_day(
            {"threshold": {"days": 7, "boundary": "on"}}), 7)
        self.assertEqual(core.first_reminder_day(
            {"threshold": {"days": 7, "boundary": "after"}}), 8)

    def test_missing_boundary_defaults_to_after(self):
        """
        缺省必须是 after（＝历史行为）。若缺省成 on，
        新增节点忘填 boundary 就会静默把口径提前一天。
        """
        self.assertEqual(core.first_reminder_day({"threshold": {"days": 7}}), 8)

    def test_first_reminder_day_matches_is_overdue(self):
        for days in (1, 3, 7, 21, 30):
            for b in ("on", "after"):
                node = {"threshold": {"days": days, "boundary": b}}
                d = core.first_reminder_day(node)
                self.assertFalse(core.is_overdue(d - 1, node))
                self.assertTrue(core.is_overdue(d, node))


class InvalidBoundaryTest(unittest.TestCase):
    def test_typo_in_boundary_is_fatal_not_silently_defaulted(self):
        """
        写错值不许静默按默认走 —— 那会悄悄改掉这个节点的口径，
        而配置文件看起来像是设置生效了。

        退出码 2 而非 1：自从有了启动阶段的 validate_configs，这类错误在
        **读台账之前**就被拦住了。更早失败更好 —— 口径写错时压根不该开跑。
        """
        rules = rules_cfg(collect={"threshold": {"days": 7, "boundary": "onn"}})
        sheet = make_sheet([row(1, "甲公司", tech="待收资",
                                reported=at(0), progress=at(0))])
        with temp_home(rules=rules):
            r = run_main([f"--today={at(30)}", "--force-push"], sheet)
            self.assertEqual(r.code, 2)
            self.assertIn("boundary", r.err)
            self.assertEqual(r.posts, [], "口径都没定准，更不该把清单发出去")


if __name__ == "__main__":
    unittest.main()
