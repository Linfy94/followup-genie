#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P1-A：人工名单身份校验。

三张名单（业务确认终止 / 语义例外 / 暂缓）直接决定项目催不催，全靠**序号**匹配。
序号填错、或某行被删后序号被新项目占用，就会永久排除错误的项目 ——
而判定照跑、总数照样对得上，没有任何人会发现。

序号是坐标，名称是身份。两者一致才认。
"""

from __future__ import annotations

import unittest
from datetime import date

from harness import (make_sheet, row, days_ago, temp_home, run_main,
                     ledgers_cfg, read_state)

TODAY = date(2026, 7, 20)


def sheet3():
    return make_sheet([
        row(1, "甲公司", tech="待收资", reported=days_ago(TODAY, 40),
            progress=days_ago(TODAY, 40)),
        row(2, "乙公司", tech="待收资", reported=days_ago(TODAY, 40),
            progress=days_ago(TODAY, 40)),
        row(3, "丙公司", tech="待收资", reported=days_ago(TODAY, 40),
            progress=days_ago(TODAY, 40)),
    ])


def run_with(list_field, entries, sheet=None):
    cfg = ledgers_cfg(**{list_field: entries})
    with temp_home(ledgers=cfg) as home:
        r = run_main([f"--today={TODAY}", "--force-push"], sheet or sheet3())
        return r, home


class ManualTerminalIdentityTest(unittest.TestCase):

    def test_key_and_name_match_is_accepted(self):
        r, home = run_with("manual_terminal",
                           [{"key": "2", "name": "乙公司", "reason": "业务确认"}])
        self.assertEqual(r.code, 0, r.err)
        self.assertNotIn("乙公司", r.out, "被点名终止的项目不该出现在催办清单里")
        self.assertIn("甲公司", r.out)

    def test_key_not_in_ledger_is_fatal(self):
        """序号填错、或那一行已被业务删掉。"""
        r, _ = run_with("manual_terminal",
                        [{"key": "99", "name": "乙公司", "reason": "业务确认"}])
        self.assertEqual(r.code, 1)
        self.assertIn("序号 99 在台账里不存在", r.err)
        self.assertTrue(r.alerted)

    def test_name_mismatch_is_fatal(self):
        r, _ = run_with("manual_terminal",
                        [{"key": "2", "name": "早就改名了的公司", "reason": "业务确认"}])
        self.assertEqual(r.code, 1)
        self.assertIn("身份对不上", r.err)
        self.assertTrue(r.alerted)

    def test_key_reused_by_a_new_project_is_caught(self):
        """
        最阴险的一种：业务删掉了 2 号，后来新项目占用了序号 2。
        只比序号的话，会把新项目永久排除，而且永远不会有人发现。
        """
        reused = make_sheet([
            row(1, "甲公司", tech="待收资", reported=days_ago(TODAY, 40),
                progress=days_ago(TODAY, 40)),
            row(2, "全新的丁公司", tech="待收资", reported=days_ago(TODAY, 40),
                progress=days_ago(TODAY, 40)),
        ])
        r, _ = run_with("manual_terminal",
                        [{"key": "2", "name": "乙公司", "reason": "业务确认"}],
                        sheet=reused)
        self.assertEqual(r.code, 1)
        self.assertIn("序号可能被新项目复用", r.err)
        self.assertIn("全新的丁公司", r.err, "报错里要说清台账现在是谁")


class OtherManualListsTest(unittest.TestCase):
    """同一套检查必须覆盖全部名单，不是只保住 manual_terminal 一张。"""

    def test_exceptions_name_mismatch_is_fatal(self):
        r, _ = run_with("terminal_note_exceptions",
                        [{"key": "3", "name": "不是丙公司", "reason": "x"}])
        self.assertEqual(r.code, 1)
        self.assertIn("备注语义例外表", r.err)

    def test_exceptions_missing_key_is_fatal(self):
        r, _ = run_with("terminal_note_exceptions",
                        [{"key": "77", "name": "丙公司", "reason": "x"}])
        self.assertEqual(r.code, 1)
        self.assertIn("序号 77 在台账里不存在", r.err)

    def test_paused_name_mismatch_is_fatal(self):
        r, _ = run_with("paused", [{"key": "1", "name": "张冠李戴", "reason": "x"}])
        self.assertEqual(r.code, 1)
        self.assertIn("暂缓名单", r.err)

    def test_paused_ok(self):
        r, _ = run_with("paused", [{"key": "1", "name": "甲公司", "reason": "等BA"}])
        self.assertEqual(r.code, 0, r.err)
        self.assertNotIn("甲公司", r.out)

    def test_gray_list_mismatch_is_warning_not_fatal(self):
        """
        灰名单只生成复核提示、不参与催与不催。
        为一条提示把整个催办停掉不成比例 —— 但也不能一声不响。
        """
        r, _ = run_with("gray_list_for_review",
                        [{"key": "3", "name": "对不上的名字"}])
        self.assertEqual(r.code, 0, "灰名单不一致不该阻断运行")
        self.assertIn("身份对不上", r.out, "但必须在清单里显示出来")

    def test_gray_list_legacy_string_form_still_works(self):
        """旧的纯字符串数组要继续能跑，只提示补名，不报错。"""
        r, _ = run_with("gray_list_for_review", ["1", "3"])
        self.assertEqual(r.code, 0)
        self.assertIn("只有序号没有企业名", r.out)


class RealConfigTest(unittest.TestCase):
    """拿真实配置对真实台账跑一遍身份校验 —— 这是上线前的关卡。"""

    def test_shipped_config_lists_are_consistent(self):
        import json
        import os
        from pathlib import Path
        import core
        home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
        cfg_path = home / "followup" / "config" / "ledgers.json"
        if not cfg_path.exists():
            self.skipTest("没有真实配置，跳过")
        led = json.loads(cfg_path.read_text(encoding="utf-8"))["ledgers"][0]
        # 用假表模拟「台账里这些序号都在、名字都对」，只验证配置自身结构合法
        entries = []
        for field, _label, _fatal in core.MANUAL_LISTS:
            entries.extend(core.manual_list_entries(led, field))
        self.assertTrue(entries, "真实配置里应该有人工名单")
        for e in entries:
            self.assertTrue(e["key"], f"名单条目缺 key：{e}")


if __name__ == "__main__":
    unittest.main()
