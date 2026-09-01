#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qqdoc.read_sheet()：加粗过的表头格，cells 模式读出来是空——见 P0-5。

═══════════════════════════════════════════════════════════════════════
2026-08-31 实测：AI安全员/舆情/体检/GEO 四张前期台账（同一份腾讯文档
里的四个子表）表头第 0 列（本该是「企业名称」）同时读成空字符串，
入口断言当场拦下「表头缺少必需列」——业务确认那一格文字确实在、没有
合并单元格，只是加粗了。直接调 sheet.get_cell_data 复现：cells 模式
（return_csv=false）这一格的 value_type / string_value 都是空；
同一个请求把 return_csv 换成 true，CSV 文本里这一列的文字完全正常。

数据行（第 1 行往后）完全没受影响——这就是为什么只把表头这一行单独
换成 CSV 模式取，不把整个读取管线都换掉（P0-3 已经说明为什么数据行
不能整体走 CSV：日期在 CSV 模式下依赖单元格显示格式）。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import unittest
from unittest import mock

from harness import core  # noqa: F401 —— 挂 sys.path

import qqdoc
from qqdoc import LedgerError


INFO = {
    "sheets": [{"sheet_id": "S1", "sheet_name": "AI安全员前期",
               "row_count": 3, "col_count": 3}],
}


def _cells_response(rows_cells):
    return {"cells": rows_cells}


class BoldHeaderCellTest(unittest.TestCase):

    def _read(self, header_csv_data, data_cells):
        """
        header_csv_data：CSV 模式下这一行该返回的 csv_data 字符串。
        data_cells：cells 模式下（含表头行，表头这一行故意留错/留空，
                    模拟实测踩到的现象）返回的 cells 列表。
        """
        def fake_call_tool(name, args):
            if name == "sheet.get_sheet_info":
                return INFO
            if name == "sheet.get_cell_data":
                if args.get("return_csv"):
                    self.assertEqual(args["start_row"], 0)
                    self.assertEqual(args["end_row"], 0)
                    return {"cells": [], "csv_data": header_csv_data}
                return _cells_response(data_cells)
            raise AssertionError(f"未预期的调用：{name}")

        with mock.patch.object(qqdoc, "call_tool", side_effect=fake_call_tool):
            return qqdoc.read_sheet("F1", "S1")

    def test_bold_header_cell_blank_in_cells_mode_is_recovered_via_csv(self):
        """
        🔴 实测复现：表头第 0 列在 cells 模式下 value_type/string_value
        都是空（跟真实响应完全一样），但 CSV 模式读到的是「企业名称」。
        """
        data_cells = [
            # 表头行（第0行）：第0列故意留成「加粗读空」这种坏形状
            {"row": 0, "col": 0, "value_type": "", "string_value": ""},
            {"row": 0, "col": 1, "value_type": "STRING", "string_value": "分行"},
            {"row": 0, "col": 2, "value_type": "STRING", "string_value": "当前状态"},
            # 数据行：完全正常，没受影响
            {"row": 1, "col": 0, "value_type": "STRING", "string_value": "示例企业甲"},
            {"row": 1, "col": 1, "value_type": "STRING", "string_value": "杭州"},
            {"row": 1, "col": 2, "value_type": "STRING", "string_value": "已申请"},
            {"row": 2, "col": 0, "value_type": "STRING", "string_value": "示例企业乙"},
            {"row": 2, "col": 1, "value_type": "STRING", "string_value": "深圳"},
            {"row": 2, "col": 2, "value_type": "STRING", "string_value": "终止"},
        ]
        s = self._read("企业名称,分行,当前状态\n", data_cells)
        self.assertEqual(s.header, ["企业名称", "分行", "当前状态"])
        self.assertTrue(s.has_column("企业名称"))
        self.assertEqual(s.text(1, "企业名称"), "示例企业甲")
        self.assertEqual(s.text(2, "企业名称"), "示例企业乙")

    def test_data_rows_still_come_from_cells_mode_not_csv(self):
        """
        数据行必须还是走 cells 模式——不能因为表头换了 CSV，就把数据也
        换掉（P0-3：日期在 CSV 模式下依赖单元格显示格式，不能整体换）。
        这里表头 CSV 和 cells 模式的数据内容特意写得不一样，
        断言读到的是 cells 模式那一份。
        """
        data_cells = [
            {"row": 0, "col": 0, "value_type": "", "string_value": ""},
            {"row": 1, "col": 0, "value_type": "STRING", "string_value": "cells模式的值"},
        ]
        s = self._read("企业名称\n", [c for c in data_cells])
        self.assertEqual(s.text(1, "企业名称"), "cells模式的值")

    def test_header_shorter_than_col_count_is_padded(self):
        """CSV 那一行字段数比 col_count 少（尾部空列）时补空，不报错。"""
        s = self._read("企业名称,分行\n", [
            {"row": 0, "col": 0, "value_type": "", "string_value": ""},
            {"row": 1, "col": 0, "value_type": "STRING", "string_value": "x"},
        ])
        self.assertEqual(s.header, ["企业名称", "分行", ""])

    def test_header_field_with_embedded_newline_is_not_split_apart(self):
        """
        🔴 实测踩过（AI外贸拓客台账）：表头格里业务自己写了带换行的说明文字
        （"企业\\n底色的含义：……"），CSV 规范里这种字段会被引号包起来，
        物理行数比逻辑行数多。按物理行切会在字段中间断开，后面的列
        全部错位——必须用 csv.reader 解析整段文本，让带引号的换行被
        正确当成同一个字段，不能简单取 splitlines() 的第一行。
        """
        csv_text = '"企业\n底色说明：白色→浅绿→绿",分行,当前状态\n'
        s = self._read(csv_text, [
            {"row": 0, "col": 0, "value_type": "", "string_value": ""},
            {"row": 1, "col": 0, "value_type": "STRING", "string_value": "x"},
        ])
        self.assertEqual(s.header,
                         ["企业\n底色说明：白色→浅绿→绿", "分行", "当前状态"])

    def test_normal_unbolded_header_is_unaffected(self):
        """没加粗的正常表头（cells 模式本来就读得对）走这条新路径结果不变。"""
        data_cells = [
            {"row": 0, "col": 0, "value_type": "STRING", "string_value": "企业名称"},
            {"row": 1, "col": 0, "value_type": "STRING", "string_value": "x"},
        ]
        s = self._read("企业名称\n", data_cells)
        self.assertEqual(s.header, ["企业名称", "", ""])


if __name__ == "__main__":
    unittest.main()
