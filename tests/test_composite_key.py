#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
主键支持组合键（key_field 收一个字段名或多个字段名的数组）。

═══════════════════════════════════════════════════════════════════════
🔴 2026-08-10 实测：三份企微台账**一个唯一列都没有**。

  舆情「编号」3 组重复 ｜ AI体检「序号」2 组 ｜ GEO「序号」1 组 + 4 行空值
  而「企业」这类名字列重复得更多 —— 同一家公司在不同分行各开一个项目很正常。

重复主键会让两个项目共用一条催办状态、互相静音，入口断言判它致命，
也就是说**不支持组合键，这三份台账一行都读不了**。
实测「编号/序号 + 企业」在三张表上全部唯一、0 重复。

放宽方式与 clock.fallback、repeat.weekday 完全一致（标量或数组都收）——
这是第三次走同一条路子，不另起一套。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import unittest
from datetime import date
from unittest import mock

from harness import core

from test_sentinel_rules import FakeSheet, base_ledger

TODAY = date(2026, 3, 2)


def sheet(rows):
    return FakeSheet(["序号", "企业", "已发货", "进入时间"], rows)


def ruleset():
    return {"nodes": [{
        "id": "ship", "name": "待发货", "enabled": True,
        "when": [{"field": "已发货", "op": "empty"}],
        "clock": {"field": "进入时间"},
        "threshold": {"days": 0, "boundary": "on"},
        "repeat": {"days": 1},
    }]}


def ledger(key_field):
    l = base_ledger(required_columns=["序号", "企业", "已发货", "进入时间"])
    l["key_field"] = key_field
    l["name_field"] = "企业"
    return l


DUP_NUMBER = [
    {"序号": "1", "企业": "甲公司", "已发货": "", "进入时间": TODAY},
    {"序号": "1", "企业": "乙公司", "已发货": "", "进入时间": TODAY},
]
DUP_NAME = [
    {"序号": "1", "企业": "甲公司", "已发货": "", "进入时间": TODAY},
    {"序号": "2", "企业": "甲公司", "已发货": "", "进入时间": TODAY},
]


class NormalizeTest(unittest.TestCase):

    def test_single_string_still_works(self):
        """🔴 现有六份台账全是单字段写法，行为必须一字不变。"""
        self.assertEqual(core.key_fields({"key_field": "序号"}), ["序号"])

    def test_default_is_still_序号(self):
        self.assertEqual(core.key_fields({}), ["序号"])

    def test_array_form(self):
        self.assertEqual(core.key_fields({"key_field": ["序号", "企业"]}),
                         ["序号", "企业"])

    def test_junk_is_dropped_defensively(self):
        for junk in (None, 7, [], ["", "  "], [3, "序号"]):
            with self.subTest(junk=junk):
                core.key_fields({"key_field": junk})   # 不抛异常即可


class RowKeyTest(unittest.TestCase):

    def test_joins_with_a_pipe(self):
        s = sheet([{"序号": "12", "企业": "甲公司", "已发货": "", "进入时间": TODAY}])
        self.assertEqual(core.row_key(s, 1, ["序号", "企业"]), "12|甲公司")

    def test_any_blank_part_makes_the_whole_key_blank(self):
        """
        🔴 缺一段就整个算空，交给入口断言的「主键读到空值」去抓。
           拼出 "|甲公司" 这种半截键的话，两行都缺序号时会撞在一起 ——
           而撞上的后果是两个项目共用一条催办状态、互相静音。
        """
        s = sheet([{"序号": "", "企业": "甲公司", "已发货": "", "进入时间": TODAY}])
        self.assertEqual(core.row_key(s, 1, ["序号", "企业"]), "")

    def test_label_reads_like_the_config(self):
        self.assertEqual(core.key_label(["序号", "企业"]), "序号+企业")
        self.assertEqual(core.key_label(["序号"]), "序号")


class AssertionTest(unittest.TestCase):
    """入口断言在组合键下的行为。"""

    def _fatal(self, rows, key_field):
        return core.assert_sheet(sheet(rows), ledger(key_field), ruleset()).fatal

    def test_duplicate_number_alone_is_still_fatal(self):
        """🔴 先证明这三份台账用单字段键真的读不了 —— 否则组合键是白加的。"""
        errs = self._fatal(DUP_NUMBER, "序号")
        self.assertTrue(any("重复值" in e for e in errs), errs)

    def test_duplicate_name_alone_is_still_fatal(self):
        errs = self._fatal(DUP_NAME, "企业")
        self.assertTrue(any("重复值" in e for e in errs), errs)

    def test_composite_key_resolves_both(self):
        for rows in (DUP_NUMBER, DUP_NAME):
            with self.subTest(rows=rows):
                errs = self._fatal(rows, ["序号", "企业"])
                self.assertEqual([e for e in errs if "重复值" in e], [])

    def test_composite_key_still_catches_a_real_duplicate(self):
        """两列都一样才是真重复 —— 这时必须照样致命。"""
        rows = [{"序号": "1", "企业": "甲公司", "已发货": "", "进入时间": TODAY},
                {"序号": "1", "企业": "甲公司", "已发货": "", "进入时间": TODAY}]
        errs = self._fatal(rows, ["序号", "企业"])
        self.assertTrue(any("重复值" in e for e in errs), errs)

    def test_blank_part_is_reported_as_blank_key(self):
        rows = [{"序号": "", "企业": "甲公司", "已发货": "", "进入时间": TODAY}]
        errs = self._fatal(rows, ["序号", "企业"])
        self.assertTrue(any("空值" in e for e in errs), errs)

    def test_message_names_both_columns(self):
        rows = [{"序号": "", "企业": "甲公司", "已发货": "", "进入时间": TODAY}]
        errs = self._fatal(rows, ["序号", "企业"])
        self.assertTrue(any("序号+企业" in e for e in errs),
                        f"报错要说清是哪个键，否则不知道去看哪一列：{errs}")

    def test_missing_key_column_is_reported_as_config_error(self):
        """
        组合键里写了一个不存在的列 —— 必须报「配置引用了不存在的列」，
        而不是绕一圈表现成「主键读到空值」。
        """
        errs = self._fatal(
            [{"序号": "1", "企业": "甲公司", "已发货": "", "进入时间": TODAY}],
            ["序号", "这一列不存在"])
        self.assertTrue(any("不存在的列" in e for e in errs), errs)


class JudgementTest(unittest.TestCase):
    """判定跑通，且状态键用的是组合值。"""

    def test_two_rows_sharing_a_number_are_two_projects(self):
        wd = core.WorkdayCalc({"exclude_weekends": True, "exclude_holidays": False}, None)
        with mock.patch.object(core, "read_ledger_sheet",
                               return_value=sheet(DUP_NUMBER)):
            rep, _ = core.evaluate_ledger(
                ledger(["序号", "企业"]), ruleset(), wd, TODAY, {}, {}, {})
        self.assertEqual(len(rep.due), 2, "两行应各自成项，不能互相静音")
        self.assertEqual({i.key for i in rep.due}, {"1|甲公司", "1|乙公司"})


if __name__ == "__main__":
    unittest.main()
