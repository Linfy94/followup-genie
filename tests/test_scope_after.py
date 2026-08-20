#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scope_after：日期早于给定常量的行不算责任范围内。

═══════════════════════════════════════════════════════════════════════
🔴 业务 2026-08-20 接 AI外贸拓客台账时提出：排除 2026-06 前登记的历史数据 ——
   不是随口一说，实测过：不排除的话，2026-06 前、且在杭州/深圳范围内的
   老数据里，39 条会被①客户填表误判成「没填联系方式」、11 条会被
   ②确认回报误判成「没立项」，全是早该收尾但台账没清的旧线索。

   与 not_before（比较两个字段）不同，这条约束比较的是一个字段 vs 一个
   写死的日期常量 —— 语义是 scope_filters 的「日期版」，判定结果走同一个
   out_of_scope 计数，不是终止、不是暂缓。

🔴 「不能证伪就不排除」：日期取不到（空/解析不出）时不排除，
   跟 P0-2 主键护栏、not_before 的两处兜底是同一条道理 ——
   宁可多催一条被人发现，也不能让数据质量问题悄悄躲进「范围外」。

这是全新能力，不是修复既有 bug：没有可比的「旧代码错误行为」，
所以下面没有传统意义上的红绿对照（旧代码上只会是 AttributeError，
证明不了任何判断逻辑），验证靠的是行为覆盖本身：早于/不早于/取不到，
三条分支都要能各自命中。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import unittest
from datetime import date
from unittest import mock

from harness import ledgers_cfg, rules_cfg  # noqa: F401 —— 挂 sys.path
from test_sentinel_rules import FakeSheet, base_ledger

import core

TODAY = date(2026, 8, 20)
CUTOFF = date(2026, 7, 1)
BEFORE = date(2026, 6, 15)   # 早于 CUTOFF —— 该被排除
AFTER = date(2026, 7, 10)    # 不早于 CUTOFF —— 照常判定

RULESET = {"nodes": [{
    "id": "wait", "name": "待登记", "enabled": True,
    "when": [{"field": "状态", "op": "equals", "value": "待登记"}],
    "clock": {"field": "进入时间"},
    "threshold": {"days": 0, "boundary": "on"},
    "repeat": {"days": 1},
}]}


def _run(rows, scope_after_cfg=None):
    sheet = FakeSheet(["企业名称", "状态", "进入时间"], rows)
    over = {"scope_after": scope_after_cfg} if scope_after_cfg else {}
    led = base_ledger(**over)
    wd = core.WorkdayCalc({"exclude_weekends": True, "exclude_holidays": False}, None)
    with mock.patch.object(core, "read_ledger_sheet", return_value=sheet):
        rep, _ = core.evaluate_ledger(led, RULESET, wd, TODAY, {}, {}, {})
    return rep


def _due_names(rep):
    return {i.name for i in rep.due}


class OfflineValidationTest(unittest.TestCase):
    """scope_after 配错要在 --validate-config 当场报错，不许等到运行时才崩。"""

    def test_missing_field_errors(self):
        errs = core.validate_configs(
            ledgers_cfg(scope_after={"date": "2026-07-01"}), rules_cfg(), {})
        self.assertTrue(any("scope_after" in e and "field" in e for e in errs), errs)

    def test_missing_date_errors(self):
        errs = core.validate_configs(
            ledgers_cfg(scope_after={"field": "需求上报日期"}), rules_cfg(), {})
        self.assertTrue(any("scope_after" in e and "date" in e for e in errs), errs)

    def test_malformed_date_format_errors(self):
        errs = core.validate_configs(
            ledgers_cfg(scope_after={"field": "需求上报日期", "date": "2026/07/01"}),
            rules_cfg(), {})
        self.assertTrue(any("scope_after" in e for e in errs), errs)

    def test_not_a_dict_errors(self):
        errs = core.validate_configs(
            ledgers_cfg(scope_after="2026-07-01"), rules_cfg(), {})
        self.assertTrue(any("scope_after" in e for e in errs), errs)

    def test_well_formed_config_passes(self):
        errs = core.validate_configs(
            ledgers_cfg(scope_after={"field": "需求上报日期", "date": "2026-07-01"}),
            rules_cfg(), {})
        self.assertEqual([e for e in errs if "scope_after" in e], [])

    def test_absent_is_fine(self):
        """没配 scope_after 就是原来的行为，不该报任何错。"""
        errs = core.validate_configs(ledgers_cfg(), rules_cfg(), {})
        self.assertEqual([e for e in errs if "scope_after" in e], [])


class FieldMustExistTest(unittest.TestCase):
    """scope_after.field 指向的列在台账里不存在，必须立即失败，不许继续判定。"""

    def test_missing_column_is_fatal(self):
        s = FakeSheet(["企业名称", "状态", "进入时间"],
                      [{"企业名称": "甲公司", "状态": "待登记", "进入时间": AFTER}])
        led = base_ledger(scope_after={"field": "不存在的列", "date": "2026-07-01"})
        a = core.assert_sheet(s, led, {"nodes": []})
        self.assertFalse(a.ok)
        self.assertTrue(any("不存在的列" in e for e in a.fatal), a.fatal)


class JudgmentTest(unittest.TestCase):
    """核心行为：早于常量 → 范围外；不早于 → 照常判定；取不到日期 → 不排除。"""

    CFG = {"field": "进入时间", "date": CUTOFF.isoformat()}

    def test_before_cutoff_is_out_of_scope(self):
        rep = _run([{"企业名称": "老线索", "状态": "待登记", "进入时间": BEFORE}], self.CFG)
        self.assertEqual(rep.out_of_scope, 1)
        self.assertNotIn("老线索", _due_names(rep))
        self.assertTrue(
            any("进入时间" in k and "早于" in k for k in rep.out_of_scope_detail),
            rep.out_of_scope_detail)

    def test_on_or_after_cutoff_is_judged_normally(self):
        rep = _run([{"企业名称": "新线索", "状态": "待登记", "进入时间": AFTER}], self.CFG)
        self.assertEqual(rep.out_of_scope, 0)
        self.assertIn("新线索", _due_names(rep))

    def test_missing_date_is_not_excluded(self):
        """不能证伪就不排除：这一行没填「进入时间」，不该被 scope_after 悄悄吞掉。"""
        rep = _run([{"企业名称": "没填日期", "状态": "待登记", "进入时间": None}], self.CFG)
        self.assertEqual(rep.out_of_scope, 0,
                         "空日期不该被 scope_after 判成范围外")

    def test_without_scope_after_configured_nothing_changes(self):
        """没配这项时，行为必须跟这个能力出现之前完全一样。"""
        rep = _run([{"企业名称": "老线索", "状态": "待登记", "进入时间": BEFORE}])
        self.assertEqual(rep.out_of_scope, 0)
        self.assertIn("老线索", _due_names(rep))


class PrimaryKeyInteractionTest(unittest.TestCase):
    """
    scope_after 排除的行，跟 scope_filters 排除的行一样，不该拖累主键断言。

    同 P0-2 那条护栏一个道理：只对**责任范围内**的行判致命。

    🔴 「企业名称」blank 会被 assert_sheet 在 name_field 这一步就整行滤掉
       （跟 scope_after 无关，是更早的一道过滤），测不出 scope_after 与主键
       断言的交互。这里改用「序号」当主键分量、「企业」当 name_field——
       企业非空、序号为空，行才会走到 scope_after 这一步。
    """

    def test_blank_key_before_cutoff_is_not_fatal(self):
        s = FakeSheet(
            ["序号", "企业", "进入时间"],
            [{"序号": "", "企业": "老线索", "进入时间": BEFORE},   # 范围外：空主键，不致命
             {"序号": "1", "企业": "甲公司", "进入时间": AFTER}])
        led = base_ledger(
            key_field=["序号", "企业"], name_field="企业",
            required_columns=["序号", "企业", "进入时间"],
            scope_after={"field": "进入时间", "date": CUTOFF.isoformat()})
        a = core.assert_sheet(s, led, {"nodes": []})
        self.assertTrue(a.ok, a.fatal)


if __name__ == "__main__":
    unittest.main()
