#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
`scripts/diff_due.py`：改动前后待催名单的逐条比对。

═══════════════════════════════════════════════════════════════════════
这个脚本存在的意义是**替人做那件他会跳过的事**，所以它自己漏报一条，
后果就等于那件事没做过 —— 而且没人会发现。因此这里每一类差异都单钉一条：
少写哪一类，哪一类的变化就永远看不见。

最要紧的两条：
  · 「不再催办」必须报（少催一条谁都看不见，比多催危险得多）
  · 同一个项目从①走到②要算「一进一出」，不能算成「同一条改了属性」
    —— 否则节点迁移会被显示成「超期天数变了」，看着像无害的数字波动。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "diff_due.py"


def item(key="1|甲公司", name="甲公司", node="②评估", **kw):
    base = {"key": key, "name": name, "node": node, "stage": "待评估",
            "stalled_days": 30, "allowance_days": 7, "overdue_days": 23,
            "clock_from": "2026-07-13", "clock_source": "快照累积的节点进入时间",
            "action": "催评估"}
    base.update(kw)
    return base


def payload(due, *, date="2026-08-12", counts=None, warnings=None,
            total_rows=100, failures=None):
    return {
        "date": date, "reminders_write": False, "failures": failures or [],
        "ledgers": [{
            "id": "box", "name": "盒子台账", "line": "盒子",
            "total_rows": total_rows, "due": due,
            "counts": dict({"due": len(due), "overdue_muted": 0, "terminal": 0,
                            "paused": 0, "advanced": 0, "out_of_scope": 0,
                            "not_overdue": 0, "no_node": 0}, **(counts or {})),
            "disabled_nodes": [], "warnings": warnings or [],
            "notices": [], "review_hints": [],
        }],
    }


def run(before: dict, after: dict):
    with tempfile.TemporaryDirectory() as d:
        b, a = Path(d) / "b.json", Path(d) / "a.json"
        b.write_text(json.dumps(before, ensure_ascii=False), encoding="utf-8")
        a.write_text(json.dumps(after, ensure_ascii=False), encoding="utf-8")
        return subprocess.run([sys.executable, str(SCRIPT), str(b), str(a)],
                              capture_output=True, text=True)


class NoChangeTest(unittest.TestCase):

    def test_identical_is_clean_and_exit_zero(self):
        p = payload([item()])
        r = run(p, json.loads(json.dumps(p)))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("无差异", r.stdout)


class DueListTest(unittest.TestCase):

    def test_newly_due_is_reported(self):
        r = run(payload([]), payload([item()]))
        self.assertEqual(r.returncode, 1)
        self.assertIn("新增催办", r.stdout)
        self.assertIn("甲公司", r.stdout)

    def test_no_longer_due_is_reported(self):
        """🔴 少催一条业务永远发现不了，这条漏报等于工具白做。"""
        r = run(payload([item()]), payload([]))
        self.assertEqual(r.returncode, 1)
        self.assertIn("不再催办", r.stdout)

    def test_node_move_counts_as_one_in_one_out(self):
        """
        🔴 同一个 key 换了节点。若身份里不含节点，这里只会显示成
           「stage/action 变了」—— 一个项目整段跳到另一个节点，
           读起来却像文案微调。
        """
        r = run(payload([item(node="②评估")]),
                payload([item(node="③安装", stage="待安装")]))
        self.assertEqual(r.returncode, 1)
        self.assertIn("新增催办", r.stdout)
        self.assertIn("不再催办", r.stdout)

    def test_overdue_days_change_on_same_row(self):
        r = run(payload([item(overdue_days=23)]),
                payload([item(overdue_days=40)]))
        self.assertEqual(r.returncode, 1)
        self.assertIn("overdue_days", r.stdout)

    def test_clock_source_change_is_reported(self):
        """计时起点换了一级（比如从快照掉到 fallback）是判定层的变化，
        超期天数可能只差一两天，肉眼比对根本看不出来。"""
        r = run(payload([item(clock_source="快照累积的节点进入时间")]),
                payload([item(clock_source="人工播种起点")]))
        self.assertEqual(r.returncode, 1)
        self.assertIn("clock_source", r.stdout)


class BeyondDueListTest(unittest.TestCase):
    """名单之外的三样也得比 —— 它们变了同样意味着判定被动过。"""

    def test_counts_change_with_identical_due_list(self):
        """
        🔴 本轮基线正好是「全部台账 0 条待催」。若只比 due 列表，
           那一天任何改动都会显示成「无差异」—— 一个永远绿的检查。
        """
        r = run(payload([], counts={"terminal": 34}),
                payload([], counts={"terminal": 20}))
        self.assertEqual(r.returncode, 1)
        self.assertIn("terminal", r.stdout)

    def test_total_rows_change(self):
        r = run(payload([], total_rows=100), payload([], total_rows=114))
        self.assertEqual(r.returncode, 1)
        self.assertIn("总行数", r.stdout)

    def test_warning_appearing_and_disappearing(self):
        r = run(payload([], warnings=["旧警告"]), payload([], warnings=["新警告"]))
        self.assertEqual(r.returncode, 1)
        self.assertIn("新增警告", r.stdout)
        self.assertIn("消失警告", r.stdout)

    def test_ledger_added_or_removed(self):
        after = payload([])
        after["ledgers"] = []
        r = run(payload([]), after)
        self.assertEqual(r.returncode, 1)
        self.assertIn("只在改前有", r.stdout)


class GuardrailTest(unittest.TestCase):

    def test_cross_day_comparison_is_called_out(self):
        """跨天比对必然有差异，而那不是改动造成的。不喊出来会让人白查半天。"""
        r = run(payload([], date="2026-08-11"), payload([], date="2026-08-12"))
        self.assertIn("不是同一天", r.stdout)

    def test_read_failure_is_called_out(self):
        """有台账没读到时名单本身就不完整，这时候的「无差异」没有意义。"""
        r = run(payload([]), payload([], failures=["geo_wecom 读取失败"]))
        self.assertIn("读取失败", r.stdout)

    def test_bad_arguments_exit_two_not_one(self):
        """🔴 退出码 2（没跑成）必须与 1（跑成了、有差异）分开。"""
        r = subprocess.run([sys.executable, str(SCRIPT)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)

    def test_missing_file_exits_two(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "/no/such/a", "/no/such/b"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)

    def test_garbage_json_exits_two_with_the_usual_cause(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "bad.json"
            bad.write_text("── 台账读取中 ──\n{}", encoding="utf-8")
            r = subprocess.run([sys.executable, str(SCRIPT), str(bad), str(bad)],
                               capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("stderr", r.stderr)   # 提示日志与 JSON 混在了一起


if __name__ == "__main__":
    unittest.main()
