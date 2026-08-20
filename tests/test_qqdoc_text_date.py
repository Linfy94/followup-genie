#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qqdoc.cell_date()：STRING 类型、"YYYY/M/D" 形状的单元格也要能读出日期。

═══════════════════════════════════════════════════════════════════════
🔴 2026-08-20 接 AI外贸拓客台账实测：「访客/需求时间」列 210/1312 行
   （16%）是 STRING 类型、内容形如 "2025/9/16"——不是 cells 模式该有的
   样子（本文件头部 P0-3 注释原话是「CSV 模式下才是这种字符串」），
   大概率是手工录入或旧版 RPA 导入时绕开了日期选择器。

   在此之前 cell_date() 只认 NUMBER 类型（serial），这 210 行全部返回
   None——不报错，只是让依赖这一列的判断（scope_after 的日期截止、
   clock 计时起点）悄悄把这些行当成"读不出日期，不能证伪就不排除"，
   于是它们绕过了本该排除它们的规则。

   206/210 能解析出合法年月日，**全部**早于 2026-06——不是随机噪声，
   是一整批同源的历史数据。业务 2026-08-20 确认把这种解析能力推广到
   所有腾讯文档台账，不只这一条线。

只认 "YYYY/M/D" 这一种**无歧义**格式：年在前、斜杠分隔。不猜
"M/D/YYYY" 这类会跟月份/日期顺序冲突的写法——形状像但顺序不同的日期，
猜错比读不出更危险（P0-3 一直以来的立场）。

这是全新能力，不是修复行为偏差的 bug；NUMBER 分支的既有行为一字未动，
下面同时覆盖两个分支，确认新增的 STRING 分支没有影响原来的 NUMBER 分支。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import unittest
from datetime import date

from harness import SERIAL_EPOCH  # noqa: F401 —— 挂 sys.path

import qqdoc


def _string_cell(text: str) -> dict:
    return {"value_type": "STRING", "string_value": text}


def _number_cell(n) -> dict:
    return {"value_type": "NUMBER", "number_value": n, "string_value": ""}


class TextDateParsingTest(unittest.TestCase):

    def test_yyyy_slash_m_slash_d_is_parsed(self):
        self.assertEqual(qqdoc.cell_date(_string_cell("2025/9/16")),
                         date(2025, 9, 16))

    def test_zero_padded_form_is_also_parsed(self):
        self.assertEqual(qqdoc.cell_date(_string_cell("2025/09/16")),
                         date(2025, 9, 16))

    def test_surrounding_whitespace_is_tolerated(self):
        self.assertEqual(qqdoc.cell_date(_string_cell("  2025/9/16  ")),
                         date(2025, 9, 16))

    def test_invalid_calendar_date_is_not_guessed(self):
        """形状对但不是合法日期（13 月、40 号）——认出了形状，不猜成别的。"""
        self.assertIsNone(qqdoc.cell_date(_string_cell("2025/13/40")))

    def test_ambiguous_month_first_form_is_not_guessed(self):
        """
        🔴 只认「年在前」。9/16/2025 这种美式写法形状也像日期，
        但猜它是哪种顺序就是在赌，读不出比猜错安全。
        """
        self.assertIsNone(qqdoc.cell_date(_string_cell("9/16/2025")))

    def test_free_text_is_not_a_date(self):
        self.assertIsNone(qqdoc.cell_date(_string_cell("待确认")))

    def test_embedded_date_inside_other_text_is_not_matched(self):
        """跟 lark 那边的「已完成,2026年08月07日」不是一回事——这里要求整格只有日期。"""
        self.assertIsNone(qqdoc.cell_date(_string_cell("已完成,2025/9/16")))

    def test_blank_string_cell_is_none(self):
        self.assertIsNone(qqdoc.cell_date(_string_cell("")))


class NumberBranchUnaffectedTest(unittest.TestCase):
    """新增的 STRING 分支不该动到既有的 NUMBER 分支一个字。"""

    def test_number_cell_still_parses_via_serial(self):
        serial = (date(2025, 9, 16) - SERIAL_EPOCH).days
        self.assertEqual(qqdoc.cell_date(_number_cell(serial)), date(2025, 9, 16))

    def test_out_of_range_serial_still_rejected(self):
        self.assertIsNone(qqdoc.cell_date(_number_cell(1)))

    def test_none_cell_still_none(self):
        self.assertIsNone(qqdoc.cell_date(None))

    def test_bool_cell_still_none(self):
        self.assertIsNone(
            qqdoc.cell_date({"value_type": "BOOL", "bool_value": True}))


if __name__ == "__main__":
    unittest.main()
