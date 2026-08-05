#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI哨兵接入用到的三个新能力：

  · threshold.workdays —— 阈值本身按工作日算（不只是复提醒间隔）
  · repeat.weekday      —— 日历节律复提醒（只在某个星期几催）
  · cross_ledger        —— 跨台账核对（这个节点的"完成"信号在另一张台账里）

不复用 harness.py 的 make_sheet/row —— 那套是按「box」腾讯文档台账的列
和 qqdoc 单元格格式设计的，跟这里要测的东西无关。这里用一个最简单的假
Sheet，只测 core.py 的判定逻辑本身。
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta
from unittest import mock

from harness import core


class FakeSheet:
    """满足 core.py 需要的 Sheet 接口：header/data_rows/has_column/text/date。"""

    def __init__(self, header: list[str], rows: list[dict]):
        self.header = header
        self._rows = {i: r for i, r in enumerate(rows, start=1)}
        self.duplicate_columns: list[str] = []

    @property
    def data_rows(self):
        return sorted(self._rows)

    def has_column(self, name):
        return name in self.header

    def text(self, row, col):
        v = self._rows.get(row, {}).get(col, "")
        return "" if v is None else (v if isinstance(v, str) else str(v))

    def date(self, row, col):
        v = self._rows.get(row, {}).get(col)
        return v if isinstance(v, date) else None


D = date(2026, 3, 2)  # 一个周一


def base_ledger(**over):
    led = {
        "id": "src", "name": "源台账", "line": "哨兵",
        "key_field": "企业名称", "name_field": "企业名称",
        "required_columns": ["企业名称", "状态", "进入时间"],
    }
    led.update(over)
    return led


class ThresholdWorkdaysTest(unittest.TestCase):
    """threshold.workdays：阈值本身按工作日数，不是自然日。"""

    def _run(self, entered: date, today: date):
        sheet = FakeSheet(
            ["企业名称", "状态", "进入时间"],
            [{"企业名称": "甲公司", "状态": "待登记", "进入时间": entered}],
        )
        ruleset = {"nodes": [{
            "id": "wait", "name": "待登记", "enabled": True,
            "when": [{"field": "状态", "op": "equals", "value": "待登记"}],
            "clock": {"field": "进入时间"},
            "threshold": {"workdays": 3, "boundary": "after"},
            "repeat": {"days": 1},
        }]}
        wd = core.WorkdayCalc({"exclude_weekends": True, "exclude_holidays": False}, None)
        with mock.patch.object(core, "read_ledger_sheet", return_value=sheet):
            rep, _ = core.evaluate_ledger(base_ledger(), ruleset, wd, today, {}, {}, {})
        return rep

    def test_weekend_does_not_count_toward_workday_threshold(self):
        # 进入时间是周一(3/2)。3 个自然日=周四(3/5)已经"满3天"，
        # 但按工作日算，3/5 也才 3 个工作日 —— boundary=after 要求"超过3个
        # 工作日"，所以周四(第3个工作日)还不该催，周五(第4个工作日)才催。
        rep_thu = self._run(D, D + timedelta(days=3))   # 周四
        self.assertEqual(len(rep_thu.due) + len(rep_thu.overdue_muted), 0,
                         "第 3 个工作日还不该进入超期")
        rep_fri = self._run(D, D + timedelta(days=4))   # 周五，第4个工作日
        self.assertEqual(len(rep_fri.due), 1, "第 4 个工作日应该开始催")

    def test_across_weekend_workday_count_is_smaller_than_calendar_days(self):
        # 进入时间周一，跨过一个周末到下周二 = 自然日 8 天，但只有 6 个工作日。
        # 阈值 3 个工作日早就过了（第4个工作日就该催），这里只确认不会因为
        # "按自然日算成 8 天" 而得出别的结论——只验证确实被算作催办对象。
        rep = self._run(D, D + timedelta(days=8))
        self.assertEqual(len(rep.due), 1)


class RepeatWeekdayTest(unittest.TestCase):
    """repeat.weekday：日历节律，只在指定星期几提醒，其余日子静默。"""

    def _run(self, today: date):
        sheet = FakeSheet(
            ["企业名称", "已发货", "进入时间"],
            [{"企业名称": "甲公司", "已发货": "", "进入时间": D}],
        )
        ruleset = {"nodes": [{
            "id": "ship", "name": "待发货", "enabled": True,
            "when": [{"field": "已发货", "op": "empty"}],
            "clock": {"field": "进入时间"},
            "threshold": {"days": 0, "boundary": "on"},
            "repeat": {"weekday": "Wed"},
        }]}
        wd = core.WorkdayCalc({"exclude_weekends": True, "exclude_holidays": False}, None)
        ledger = base_ledger(required_columns=["企业名称", "已发货", "进入时间"])
        with mock.patch.object(core, "read_ledger_sheet", return_value=sheet):
            rep, _ = core.evaluate_ledger(ledger, ruleset, wd, today, {}, {}, {})
        return rep

    def test_only_fires_on_the_configured_weekday(self):
        # D = 2026-03-02 周一。周一/周二/周四/周五都不该催，只有周三催。
        for offset, expect in ((0, False), (1, False), (2, True), (3, False), (4, False)):
            rep = self._run(D + timedelta(days=offset))
            got = len(rep.due) == 1
            self.assertEqual(got, expect,
                             f"D+{offset}（{(D + timedelta(days=offset)).strftime('%A')}）"
                             f"应催={expect} 实际={got}")

    def test_fires_again_on_the_next_matching_weekday(self):
        # 连续两周三都应该催（"直到出现"——没有"只催一次"这回事）。
        rep1 = self._run(D + timedelta(days=2))    # 第一个周三
        self.assertEqual(len(rep1.due), 1)


class CrossLedgerTest(unittest.TestCase):
    """cross_ledger：这个节点是否"结束"要去另一张台账查，不是看本表字段。"""

    def _ledgers_and_sheets(self):
        src_sheet = FakeSheet(
            ["企业名称", "状态", "进入时间"],
            [
                {"企业名称": "已登记公司", "状态": "待登记", "进入时间": D},
                {"企业名称": "未登记公司", "状态": "待登记", "进入时间": D},
            ],
        )
        tgt_sheet = FakeSheet(["企业名称"], [{"企业名称": "已登记公司"}])
        src = base_ledger(id="src")
        tgt = base_ledger(id="tgt", name="目标台账", required_columns=["企业名称"])
        ruleset = {"nodes": [{
            "id": "wait_reg", "name": "待登记", "enabled": True,
            "when": [{"field": "状态", "op": "equals", "value": "待登记"}],
            "clock": {"field": "进入时间"},
            "threshold": {"workdays": 3, "boundary": "after"},
            "repeat": {"days": 1},
            "cross_ledger": {"ledger_id": "tgt", "match_field": "企业名称", "target_field": "企业名称"},
        }]}
        return src, tgt, ruleset, {"src": src_sheet, "tgt": tgt_sheet}

    def test_row_found_in_target_ledger_is_advanced_not_due(self):
        src, tgt, ruleset, sheets = self._ledgers_and_sheets()
        wd = core.WorkdayCalc({"exclude_weekends": True, "exclude_holidays": False}, None)
        with mock.patch.object(core, "read_ledger_sheet", side_effect=lambda l: sheets[l["id"]]):
            rep, _ = core.evaluate_ledger(
                src, ruleset, wd, D + timedelta(days=10), {}, {}, {},
                all_ledgers={"src": src, "tgt": tgt},
            )
        names_due = {i.name for i in rep.due}
        names_advanced = {n for _, n, _ in rep.advanced}
        self.assertIn("已登记公司", names_advanced, "已出现在目标台账，不该再催")
        self.assertNotIn("已登记公司", names_due)
        self.assertIn("未登记公司", names_due, "没出现在目标台账，超期了就该催")

    def test_missing_all_ledgers_raises_instead_of_silently_never_matching(self):
        """不给 all_ledgers 时必须报错，不能悄悄当成"永远没查到"从而永远催下去。"""
        src, tgt, ruleset, sheets = self._ledgers_and_sheets()
        wd = core.WorkdayCalc({"exclude_weekends": True, "exclude_holidays": False}, None)
        with mock.patch.object(core, "read_ledger_sheet", side_effect=lambda l: sheets[l["id"]]):
            with self.assertRaises(core.LedgerError):
                core.evaluate_ledger(src, ruleset, wd, D + timedelta(days=10), {}, {}, {})


if __name__ == "__main__":
    unittest.main()
