#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
责任范围把整张台账过滤光时，必须出声。

🔴 2026-08-06 真实故障：业务把「分行」从「深圳分行」改成「深圳」，
   而 scope_filters 里写的还是带后缀的写法 → AI体检 / GEO 两条线唯一那行
   被判成范围外 → 推送里显示「总任务量：0 个项目里，0 个要催办」。

   known_values 只校验状态列，scope_filters 的取值一直没有任何校验，
   所以整表落空这件事**完全静默**，和「今天确实没有要催的」长得一模一样。

方案里早就预言过这个坑（「每张表的取值写法都要单独实测枚举，照抄会一条都
匹配不上、把整表静默过滤光」），但当时只写进了文档，没变成代码里的护栏。
"""

from __future__ import annotations

import unittest
from datetime import date

from harness import (make_sheet, row, days_ago, temp_home, run_main)  # noqa: I001

TODAY = date(2026, 7, 20)

WARNING_MARK = "整张台账都被责任范围过滤掉了"


def _run(rows):
    with temp_home():
        return run_main([f"--today={TODAY}", "--verbose"], make_sheet(rows))


class ScopeEmptiedWarningTest(unittest.TestCase):

    def test_all_rows_filtered_out_warns(self):
        """台账里换了写法（杭州分行 / 深圳分行），配置还是旧的 → 全表落空。"""
        r = _run([
            row(1, "甲公司", place="杭州分行", tech="待收资",
                reported=days_ago(TODAY, 40), progress=days_ago(TODAY, 40)),
            row(2, "乙公司", place="深圳分行", tech="待收资",
                reported=days_ago(TODAY, 40), progress=days_ago(TODAY, 40)),
        ])
        self.assertIn(WARNING_MARK, r.out)
        self.assertIn("地点＝杭州分行", r.out, "要点名实际观测到的取值")
        self.assertIn("地点＝深圳分行", r.out)

    def test_warning_does_not_change_exit_code(self):
        """
        「今天这条线确实一个项目都不在责任范围内」是合法状态
        （新业务线刚接入时就是）。报故障会制造狼来了。
        """
        r = _run([row(1, "甲公司", place="北京", tech="待收资",
                      reported=days_ago(TODAY, 40))])
        self.assertIn(WARNING_MARK, r.out)
        self.assertEqual(r.code, 0)

    def test_partial_filtering_is_silent(self):
        """还有行留在范围内 → 这是日常，不该出声。"""
        r = _run([
            row(1, "甲公司", place="杭州", tech="待收资",
                reported=days_ago(TODAY, 40), progress=days_ago(TODAY, 40)),
            row(2, "乙公司", place="北京", tech="待收资",
                reported=days_ago(TODAY, 40), progress=days_ago(TODAY, 40)),
        ])
        self.assertNotIn(WARNING_MARK, r.out)

    def test_empty_ledger_is_silent(self):
        """空台账是另一回事，不归这条护栏管。"""
        r = _run([])
        self.assertNotIn(WARNING_MARK, r.out)

    def test_warning_does_not_change_the_due_list(self):
        """护栏只是多说一句话，不许影响判定。"""
        rows = [
            row(1, "甲公司", place="杭州", tech="待收资",
                reported=days_ago(TODAY, 40), progress=days_ago(TODAY, 40)),
            row(2, "乙公司", place="北京", tech="待收资",
                reported=days_ago(TODAY, 40), progress=days_ago(TODAY, 40)),
        ]
        with_scope_hit = _run(rows)
        self.assertIn("甲公司", with_scope_hit.out)
        self.assertNotIn("乙公司", with_scope_hit.out)


if __name__ == "__main__":
    unittest.main()
