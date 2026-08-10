#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
复提醒节律：业务改提醒时间时，**不该被迫改代码**。

2026-08-10 的教训：业务把哨兵④发货从「每周三提醒」改成「每周一和每周四」，
一个纯口径改动，却因为 repeat.weekday 只收单个字符串而必须动判定引擎。
在那之前的一周里，受影响的项目每周一都从清单上消失，而业务以为系统漏了。

所以这里钉四件事：
  1. 四种节律写法都能用（days / workdays / weekday / monthday）
  2. 🔴 旧写法一字不改照常工作 —— 其余台账还在用单日节律
  3. 🔴 同时配两种必须在配置校验期报错。现在的判定是
     `if weekday: … elif workdays: … else days`，同时配了 weekday 和 days
     的话 days 被**静默忽略** —— 业务改配置最可能的动作就是
     「加一行新的、忘了删旧的」，而后果是节律看着改了、实际没改
  4. 节律文案是给人看的，两套渲染和日志共用同一份
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta
from unittest import mock

from harness import core

from test_sentinel_rules import FakeSheet, base_ledger

D = date(2026, 3, 2)  # 周一


def _run(repeat: dict, today: date, entered: date = D):
    """跑一个「进入即算超期」的节点，只让 repeat 决定今天催不催。"""
    sheet = FakeSheet(
        ["企业名称", "已发货", "进入时间"],
        [{"企业名称": "甲公司", "已发货": "", "进入时间": entered}],
    )
    ruleset = {"nodes": [{
        "id": "ship", "name": "待发货", "enabled": True,
        "when": [{"field": "已发货", "op": "empty"}],
        "clock": {"field": "进入时间"},
        "threshold": {"days": 0, "boundary": "on"},
        "repeat": repeat,
    }]}
    wd = core.WorkdayCalc({"exclude_weekends": True, "exclude_holidays": False}, None)
    ledger = base_ledger(required_columns=["企业名称", "已发货", "进入时间"])
    with mock.patch.object(core, "read_ledger_sheet", return_value=sheet):
        rep, _ = core.evaluate_ledger(ledger, ruleset, wd, today, {}, {}, {})
    return rep


def _fires(repeat: dict, today: date, entered: date = D) -> bool:
    return len(_run(repeat, today, entered).due) == 1


class WeekdayListTest(unittest.TestCase):
    """repeat.weekday 收数组 —— 本轮的直接需求。"""

    def test_fires_on_every_configured_weekday(self):
        # D=周一。配 [Mon, Thu] → 周一、周四催，周二/周三/周五静默。
        want = {0: True, 1: False, 2: False, 3: True, 4: False}
        for offset, expect in want.items():
            day = D + timedelta(days=offset)
            with self.subTest(day=day.strftime("%A")):
                self.assertEqual(_fires({"weekday": ["Mon", "Thu"]}, day), expect)

    def test_single_string_still_works(self):
        """
        🔴 向后兼容。放宽成数组时最容易顺手把旧写法弄坏，
           而生产配置里其余节点还在用单日节律。
        """
        want = {0: False, 1: False, 2: True, 3: False, 4: False}
        for offset, expect in want.items():
            day = D + timedelta(days=offset)
            with self.subTest(day=day.strftime("%A")):
                self.assertEqual(_fires({"weekday": "Wed"}, day), expect)

    def test_full_names_and_mixed_forms(self):
        self.assertTrue(_fires({"weekday": ["Monday", "Thu"]}, D))


class MonthdayTest(unittest.TestCase):
    """repeat.monthday：每月固定几号。"""

    def test_fires_only_on_configured_days(self):
        for day, expect in ((1, True), (2, False), (15, True), (16, False)):
            with self.subTest(day=day):
                self.assertEqual(
                    _fires({"monthday": [1, 15]}, date(2026, 4, day),
                           entered=date(2026, 3, 1)),
                    expect)

    def test_day_31_falls_back_to_month_end(self):
        """
        🔴 配 31 而当月只有 30 天时，按当月最后一天算。
           静默跳过整个月又是一次「看着配了、实际不发」。
        """
        self.assertTrue(_fires({"monthday": [31]}, date(2026, 4, 30),
                               entered=date(2026, 3, 1)), "4月30日应视为月末")
        self.assertFalse(_fires({"monthday": [31]}, date(2026, 4, 29),
                                entered=date(2026, 3, 1)))
        # 有 31 号的月份就还是 31 号，不能提前到 30
        self.assertFalse(_fires({"monthday": [31]}, date(2026, 5, 30),
                                entered=date(2026, 3, 1)))
        self.assertTrue(_fires({"monthday": [31]}, date(2026, 5, 31),
                               entered=date(2026, 3, 1)))


class ExclusivityTest(unittest.TestCase):
    """
    🔴 同时配两种节律必须报错，不许静默挑一种执行。

    旧行为：`if weekday: … elif workdays: … else days` ——
    配了 {"weekday": [...], "days": 7} 的话 days 被整个忽略，
    而配置看起来「两种都写了」，人会以为是两者取其一或取其严。
    """

    def _validate(self, repeat: dict) -> list[str]:
        sheet = FakeSheet(["企业名称", "已发货", "进入时间"], [])
        ruleset = {"nodes": [{
            "id": "ship", "name": "待发货", "enabled": True,
            "when": [{"field": "已发货", "op": "empty"}],
            "clock": {"field": "进入时间"},
            "threshold": {"days": 0, "boundary": "on"},
            "repeat": repeat,
        }]}
        # 🔴 required_columns 必须与 FakeSheet 的表头一致。不一致的话
        #    assert_sheet 在「表头缺少必需列」那步就 return 了，**根本走不到
        #    repeat 校验**，这组用例会变成在断言列检查——看着有断言，
        #    实际什么都没测。2026-08-10 写这个文件时真踩了一次。
        ledger = base_ledger(required_columns=["企业名称", "已发货", "进入时间"])
        a = core.assert_sheet(sheet, ledger, ruleset)
        return list(a.fatal)

    def test_weekday_plus_days_is_rejected(self):
        # 用字符串写法，避开「list 不可哈希」那个崩溃 —— 这条要测的是
        # 排他性本身，混进另一个缺陷会让红色说不清是哪个原因。
        errs = self._validate({"weekday": "Mon", "days": 7})
        self.assertTrue(any("只能配一种" in e for e in errs), errs)

    def test_monthday_plus_workdays_is_rejected(self):
        errs = self._validate({"monthday": [1], "workdays": 2})
        self.assertTrue(any("只能配一种" in e for e in errs), errs)

    def test_single_form_passes(self):
        for good in ({"days": 7}, {"workdays": 2},
                     {"weekday": ["Mon", "Thu"]}, {"monthday": [1, 15]}):
            with self.subTest(good=good):
                self.assertEqual(
                    [e for e in self._validate(good) if "repeat" in e], [])

    def test_illegal_weekday_member_is_rejected(self):
        errs = self._validate({"weekday": ["Mon", "Funday"]})
        self.assertTrue(any("Funday" in e for e in errs), errs)

    def test_empty_list_is_rejected(self):
        """空数组等于「永远不催」，而缺 repeat 本身就是禁止的。"""
        errs = self._validate({"weekday": []})
        self.assertTrue(errs, "空数组必须报错，不能当成没配")

    def test_non_string_member_is_rejected(self):
        errs = self._validate({"weekday": ["Mon", 3]})
        self.assertTrue(errs, errs)

    def test_illegal_monthday_is_rejected(self):
        for bad in ([0], [32], ["一号"], []):
            with self.subTest(bad=bad):
                self.assertTrue(self._validate({"monthday": bad}),
                                f"monthday={bad!r} 必须报错")


class CadenceTextTest(unittest.TestCase):
    """
    节律文案：业务改了规则，推送里要能看出来改成了什么。
    只有一份实现，两套渲染和静默期日志共用。
    """

    def test_reads_like_chinese(self):
        cases = [
            ({"weekday": ["Mon", "Thu"]}, "周一/周四提醒"),
            ({"weekday": "Wed"}, "周三提醒"),
            ({"days": 7}, "每 7 天提醒"),
            ({"days": 1}, "每天提醒"),
            ({"workdays": 2}, "每 2 个工作日提醒"),
            ({"monthday": [1, 15]}, "每月 1/15 号提醒"),
        ]
        for repeat, want in cases:
            with self.subTest(repeat=repeat):
                self.assertEqual(core.cadence_text(repeat), want)

    def test_unknown_shape_is_empty_not_a_guess(self):
        """认不出来就不说，别编一个看着对的文案出来。"""
        self.assertEqual(core.cadence_text({}), "")

    def test_item_carries_cadence(self):
        rep = _run({"weekday": ["Mon", "Thu"]}, D)
        self.assertEqual(rep.due[0].extra.get("cadence"), "周一/周四提醒")

    def test_muted_item_carries_cadence_too(self):
        """静默期那份日志也要显示节律 —— 正是它解释了「今天为什么没有它」。"""
        rep = _run({"weekday": ["Mon", "Thu"]}, D + timedelta(days=1))
        self.assertEqual(len(rep.overdue_muted), 1)
        self.assertEqual(rep.overdue_muted[0].extra.get("cadence"), "周一/周四提醒")


if __name__ == "__main__":
    unittest.main()
