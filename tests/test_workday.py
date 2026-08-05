#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
工作日与法定节假日。

只有 ②专家评估 的复提醒（每 2 个工作日）用到。它是全方案唯一
需要每年人工更新一次的东西，所以「表过期/损坏」的降级路径必须测住 ——
静默退化成「只排周末」会让人以为算得准，而实际上国庆期间照常提醒。
"""

from __future__ import annotations

import unittest
import json
from datetime import date
from pathlib import Path

from harness import core, holidays_cfg, temp_home, run_main, make_sheet, row

CFG = {"exclude_weekends": True, "exclude_holidays": True}
ROOT = Path(__file__).resolve().parent.parent


def calc(holidays=None):
    return core.WorkdayCalc(CFG, holidays if holidays is not None else holidays_cfg())


class WorkdayCountTest(unittest.TestCase):

    def test_release_template_contains_verified_2026_calendar(self):
        preset = json.loads(
            (ROOT / "templates" / "holidays.example.json")
            .read_text(encoding="utf-8")
        )
        self.assertTrue(preset["verified"])
        self.assertEqual(preset["covers_year"], 2026)
        self.assertEqual(len(preset["holidays"]), 33)
        self.assertEqual(
            set(preset["workdays"]),
            {"2026-01-04", "2026-02-14", "2026-02-28",
             "2026-05-09", "2026-09-20", "2026-10-10"},
        )
        release_calc = core.WorkdayCalc(CFG, preset)
        self.assertFalse(release_calc.is_workday(date(2026, 10, 1)))
        self.assertTrue(release_calc.is_workday(date(2026, 10, 10)))

    def test_weekend_is_not_a_workday(self):
        c = calc()
        self.assertFalse(c.is_workday(date(2026, 7, 4)))   # 周六
        self.assertFalse(c.is_workday(date(2026, 7, 5)))   # 周日
        self.assertTrue(c.is_workday(date(2026, 7, 6)))    # 周一

    def test_spring_festival_run(self):
        """2/15–2/23 连休 9 天。周五催办 → 要跨过整个春节才满 2 个工作日。"""
        c = calc()
        for d in range(15, 24):
            self.assertFalse(c.is_workday(date(2026, 2, d)),
                             f"2026-02-{d} 是春节假期")
        # 2/13 是周五。之后第一个工作日是 2/14（补班日），第二个是 2/24
        self.assertEqual(c.count_between(date(2026, 2, 13), date(2026, 2, 23)), 1)
        self.assertEqual(c.count_between(date(2026, 2, 13), date(2026, 2, 24)), 2)

    def test_makeup_workdays_count(self):
        """补班日是周末但要算工作日 —— 漏掉 6 个补班日会让节奏整体偏慢。"""
        c = calc()
        self.assertTrue(c.is_workday(date(2026, 2, 14)),  # 周六补班
                        "2/14 是春节前的补班日")
        self.assertTrue(c.is_workday(date(2026, 10, 10)), "10/10 是国庆后的补班日")
        self.assertFalse(c.is_workday(date(2026, 10, 3)), "10/3 是国庆假期")

    def test_makeup_day_beats_weekend_rule(self):
        """补班日必须优先于「周末不算」，否则设了也白设。"""
        self.assertEqual(date(2026, 10, 10).weekday(), 5, "确认它确实是周六")
        self.assertTrue(calc().is_workday(date(2026, 10, 10)))

    def test_count_excludes_start_day(self):
        c = calc()
        self.assertEqual(c.count_between(date(2026, 7, 6), date(2026, 7, 6)), 0)
        self.assertEqual(c.count_between(date(2026, 7, 6), date(2026, 7, 7)), 1)


class HolidayTableDegradeTest(unittest.TestCase):
    """表缺失/未核对/过期，都必须报警，绝不静默降级。"""

    def test_missing_table_warns(self):
        c = core.WorkdayCalc(CFG, None)
        self.assertIsNotNone(c.holiday_warning)
        self.assertIn("退化", c.holiday_warning)

    def test_unverified_table_warns(self):
        h = holidays_cfg()
        h["verified"] = False
        self.assertIn("verified", core.WorkdayCalc(CFG, h).holiday_warning)

    def test_wrong_year_warns(self):
        h = holidays_cfg()
        h["covers_year"] = 2020
        self.assertIn("2020", core.WorkdayCalc(CFG, h).holiday_warning)

    def test_verified_current_year_is_silent(self):
        h = holidays_cfg()
        h["covers_year"] = date.today().year
        self.assertIsNone(core.WorkdayCalc(CFG, h).holiday_warning)

    def test_corrupt_table_is_a_startup_fault_not_a_traceback(self):
        """
        🔴 旧实现把节假日表的读取放在 try 之外，写坏了会抛裸 traceback。
           现在它必须走统一故障出口：退出码 2 + 告警。
        """
        sheet = make_sheet([row(1, "甲公司", tech="待收资",
                                reported=date(2026, 3, 1), progress=date(2026, 3, 1))])
        with temp_home(holidays="{ 这不是 JSON "):
            r = run_main(["--today=2026-06-01", "--force-push"], sheet)
            self.assertEqual(r.code, 2)
            self.assertIn("节假日表", r.err)
            self.assertTrue(r.alerted)

    def test_weekend_only_mode_is_not_a_warning(self):
        """明确配置成「只排周末」是合法选择，不该报警 —— **前提是没人依赖它**。"""
        c = core.WorkdayCalc({"exclude_weekends": True,
                              "exclude_holidays": False}, None)
        self.assertIsNone(c.holiday_warning)

    def test_weekend_only_mode_warns_when_a_node_repeats_by_workdays(self):
        """
        🔴 装机时最容易漏的一步：模板的 exclude_holidays 默认 false、
           holidays.json 默认是空的，漏拷之后**原本一句提示都没有**。

        而只要有节点按「每 N 个工作日」复提醒，业务读到的口径就和实际
        算的对不上 —— 国庆连休会被整段算成工作日。这必须说话。
        """
        c = core.WorkdayCalc({"exclude_weekends": True,
                              "exclude_holidays": False}, None,
                             ["待专家评估"])
        self.assertIsNotNone(c.holiday_warning)
        self.assertIn("待专家评估", c.holiday_warning)
        self.assertIn("exclude_holidays", c.holiday_warning)


class NodesUsingWorkdaysTest(unittest.TestCase):
    """doctor 与每日运行共用同一份「谁按工作日复提醒」的判断。"""

    @staticmethod
    def _rules(nodes):
        return {"rulesets": {"box": {"nodes": nodes}}}

    def test_picks_up_nodes_whose_repeat_is_in_workdays(self):
        r = self._rules([
            {"stage": "待收资", "repeat": {"days": 7}},
            {"stage": "待专家评估", "repeat": {"workdays": 2}},
        ])
        self.assertEqual(core.nodes_using_workdays(r), ["待专家评估"])

    def test_disabled_nodes_do_not_count(self):
        """禁用的节点不跑，不该因为它逼人去维护节假日表。"""
        r = self._rules([{"stage": "汇报", "enabled": False,
                          "repeat": {"workdays": 2}}])
        self.assertEqual(core.nodes_using_workdays(r), [])

    def test_all_natural_days_means_no_holiday_table_needed(self):
        r = self._rules([{"stage": "待收资", "repeat": {"days": 7}}])
        self.assertEqual(core.nodes_using_workdays(r), [])

    def test_survives_a_malformed_rules_file(self):
        """配置写坏时这个判断本身不该崩 —— 它跑在故障提示的路径上。"""
        for bad in ({}, {"rulesets": None}, {"rulesets": {"box": None}},
                    {"rulesets": {"box": {"nodes": None}}},
                    {"rulesets": {"box": {"nodes": ["不是对象"]}}}):
            self.assertEqual(core.nodes_using_workdays(bad), [])


class RepeatIntervalTest(unittest.TestCase):
    """②的复提醒真的按工作日走。"""

    def test_workday_repeat_skips_weekend(self):
        sheet = make_sheet([row(1, "甲公司", tech="",
                                reported=date(2026, 3, 1), progress=date(2026, 3, 1))])
        # 2026-07-02 是周四
        with temp_home(state={"followup_state.json": {
                "box|1|expert": {"first_overdue": "2026-07-02",
                                 "last_notified": "2026-07-02"}}}):
            for day, expect in (("2026-07-03", False),   # 周五，1 个工作日
                                ("2026-07-04", False),   # 周六
                                ("2026-07-05", False),   # 周日
                                ("2026-07-06", True)):   # 周一，满 2 个工作日
                r = run_main([f"--today={day}", "--dry-run"], sheet)
                got = "甲公司" in r.out
                self.assertEqual(got, expect, f"{day} 应催={expect} 实际={got}")


if __name__ == "__main__":
    unittest.main()
