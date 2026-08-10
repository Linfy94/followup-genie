#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
提醒节律撞上法定假期之后，怎么恢复。

═══════════════════════════════════════════════════════════════════════
业务 2026-08-10 提的两条要求：

  ①「每天提醒」要跳过节假日
  ②「每 7 天提醒」遇到节假日，则在节假日之后触发

两条都是**现在已有的行为**，本文件把它们钉住 —— 已有行为最容易在
后续改动里被无声改掉，而症状是「假期后该催的没催」，业务发现不了。

它们成立靠的是两件事配合，缺一不可：

  · 非工作日整个不投递（`_deliver` 的工作日闸门）
  · 没投递就**不写 `last_notified`**（两级状态提交）

第二条是关键。若假期当天照样记「已通知」，那么：
  · 每天提醒 → 假期每天都被记一次，节后要等到下一个间隔才响
  · 每 7 天提醒 → 第 7 天落在假期里被记掉，真正的提醒直接跳过一轮
两种都是**静默漏催**：清单上什么都不少，业务只是再也没收到。

判定用的是「距上次提醒的**自然日**数 ≥ N」，所以假期一过、天数早就够了，
下一个工作日立刻补上 —— 不会「再等一个完整周期」。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from harness import (make_sheet, row, days_ago, temp_home, run_main,
                     read_state, rules_cfg)

# 春节连休 2026-02-15 ~ 02-23。用春节而不是国庆：`--today` 是未来日期时
# 会被参数护栏挡下，而这套用例必须走真实运行才验得到投递与状态提交。
BEFORE = date(2026, 2, 13)      # 假期前最后一个工作日（周五）
IN_HOLIDAY = date(2026, 2, 18)  # 假期正中间（周三，法定假日）
AFTER = date(2026, 2, 24)       # 假期后第一个工作日（周二）


def sheet(today):
    return make_sheet([
        row(1, "甲公司", tech="待收资",
            reported=days_ago(today, 60), progress=days_ago(today, 60)),
    ])


def run_on(day, rules=None):
    return run_main([f"--today={day}", "--force-push"], sheet(day))


def notified(home) -> dict:
    fs = read_state(home, "followup_state.json") or {}
    return {k: v.get("last_notified") for k, v in fs.items()
            if v.get("last_notified")}


class DailyCadenceTest(unittest.TestCase):
    """①「每天提醒」跳过节假日。"""

    RULES = rules_cfg(collect={"repeat": {"days": 1}})

    def test_no_push_during_the_holiday(self):
        with temp_home(rules=self.RULES):
            r = run_main([f"--today={IN_HOLIDAY}", "--force-push"], sheet(IN_HOLIDAY))
            self.assertEqual(r.posts, [], "假期里不该推")
            self.assertEqual(r.code, 0, "但这不是故障")

    def test_holiday_does_not_burn_the_cadence(self):
        """
        🔴 假期那天不能记「已通知」。记了的话，节后第一天会因为
           「昨天刚催过」而静默 —— 而业务假期里根本没收到过。
        """
        with temp_home(rules=self.RULES) as home:
            run_main([f"--today={IN_HOLIDAY}", "--force-push"], sheet(IN_HOLIDAY))
            self.assertEqual(notified(home), {},
                             "假期没投递，就不该有投递凭证")

    def test_fires_again_on_the_first_workday_after(self):
        with temp_home(rules=self.RULES) as home:
            run_on(BEFORE)
            before = notified(home)
            self.assertTrue(before, "假期前应该催过一次")

            run_on(IN_HOLIDAY)
            self.assertEqual(notified(home), before, "假期里凭证不该变")

            r = run_on(AFTER)
            self.assertTrue(r.posts, "假期后第一个工作日必须补上")
            self.assertNotEqual(notified(home), before)


class WeeklyCadenceTest(unittest.TestCase):
    """②「每 7 天提醒」遇到节假日，在节假日之后触发。"""

    RULES = rules_cfg(collect={"repeat": {"days": 7}})

    def test_due_day_falling_in_the_holiday_is_not_lost(self):
        """
        🔴 本文件的核心。2/13 催一次 → 下一次该在 2/20，而 2/20 在春节假期里。
           正确行为是**假期后第一个工作日（2/24）补上**，
           不是「跳过这一轮、再等到 2/27」。
        """
        due_day = BEFORE + timedelta(days=7)          # 2026-02-20
        self.assertIn(due_day, (date(2026, 2, 20),))  # 钉住算式本身没写错

        with temp_home(rules=self.RULES) as home:
            run_on(BEFORE)
            first = notified(home)
            self.assertTrue(first)

            # 到期那天正好在假期里 —— 不推、也不消耗节律
            run_on(due_day)
            self.assertEqual(notified(home), first,
                             "到期日落在假期里，不该被记成已提醒")

            # 假期后第一个工作日：立刻补上，而不是再等 7 天
            r = run_on(AFTER)
            self.assertTrue(r.posts, "假期一过就该补这一次提醒")
            self.assertNotEqual(notified(home), first)

    def test_not_fired_before_the_interval_is_up(self):
        """反面：还没到 7 天就不该催 —— 否则上面那条会因为「天天都催」而假绿。"""
        with temp_home(rules=self.RULES) as home:
            run_on(BEFORE)
            first = notified(home)
            r = run_on(BEFORE + timedelta(days=3))
            self.assertEqual(r.posts, [], "才过 3 天，不该催")
            self.assertEqual(notified(home), first)


class WhyItWorksTest(unittest.TestCase):
    """
    上面两组成立的前提，单独钉住。这两条任何一条被改掉，
    上面的用例会红得莫名其妙，所以在这里说清是哪根柱子。
    """

    def test_pillar_1_non_workday_never_delivers(self):
        with temp_home():
            r = run_main([f"--today={IN_HOLIDAY}", "--force-push"], sheet(IN_HOLIDAY))
            self.assertEqual(r.posts, [])
            self.assertIn("法定节假日", r.out, "本地要留痕说明为什么没推")

    def test_pillar_2_no_delivery_means_no_receipt(self):
        with temp_home() as home:
            run_main([f"--today={IN_HOLIDAY}", "--force-push"], sheet(IN_HOLIDAY))
            self.assertEqual(notified(home), {})


if __name__ == "__main__":
    unittest.main()
