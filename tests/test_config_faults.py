#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置坏了要说人话，不许抛裸 traceback。

这一组守的是一类特别别扭的坏法：**合法 JSON，但结构不对**。

    ledgers.json 的内容是 []      ← json.loads 完全通过
    → load_json 放行
    → 下游第一个 .get() 炸成 AttributeError
    → 业务看到的是一段 Python 堆栈，指向 core.py 某一行

崩溃现场不是病根。真正该说的是「ledgers.json 的顶层应该是一个对象」。

而且这类故障在诊断模式下同样不许发消息、不许写状态 —— 配置坏掉的那一刻，
人往往正在用 --dry-run 排查，那时候更不该有副作用。
"""

from __future__ import annotations

import unittest
from datetime import date

from harness import (make_sheet, row, temp_home, run_main, run_doctor,
                     state_files, ledgers_cfg, rules_cfg, output_cfg)

TODAY = date(2026, 7, 20)


def sheet():
    return make_sheet([row(1, "甲公司", tech="待收资",
                           reported=date(2026, 6, 1), progress=date(2026, 6, 1))])


def write_cfg(home, name: str, raw: str | bytes) -> None:
    p = home / "followup" / "config" / name
    if isinstance(raw, bytes):
        p.write_bytes(raw)
    else:
        p.write_text(raw, encoding="utf-8")


class TopLevelNotObjectTest(unittest.TestCase):
    """三份配置各自被写成 [] / null 时，都必须是「说人话 + 退出码 2」。"""

    def _check(self, name: str, raw: str):
        with temp_home() as home:
            write_cfg(home, name, raw)
            r = run_main([f"--today={TODAY}", "--force-push"], sheet())
            self.assertEqual(r.code, 2, f"{name}={raw} 应该是启动阶段故障\n{r.err}")
            self.assertIn("顶层应该是一个 JSON 对象", r.err)
            self.assertNotIn("Traceback", r.err, "🔴 绝不能把裸 traceback 甩给业务")
            self.assertNotIn("AttributeError", r.err)
            self.assertEqual(r.posts, [], "配置坏了不该发出任何企微消息")

    def test_ledgers_is_array(self):
        self._check("ledgers.json", "[]")

    def test_ledgers_is_null(self):
        self._check("ledgers.json", "null")

    def test_rules_is_array(self):
        self._check("rules.json", "[]")

    def test_output_is_array(self):
        self._check("output.json", "[]")

    def test_output_is_a_bare_number(self):
        self._check("output.json", "42")


class EncodingAndIOTest(unittest.TestCase):

    def test_not_utf8(self):
        """记事本按 GBK 另存 —— 中文字段名会直接解不出来。"""
        with temp_home() as home:
            write_cfg(home, "ledgers.json",
                      '{"ledgers": [{"name": "台账"}]}'.encode("gbk"))
            r = run_main([f"--today={TODAY}", "--force-push"], sheet())
            self.assertEqual(r.code, 2)
            self.assertIn("UTF-8", r.err)
            self.assertNotIn("Traceback", r.err)

    def test_unreadable_file(self):
        """配置被改成目录（或权限出问题）时走 OSError 分支，不是裸崩。"""
        with temp_home() as home:
            p = home / "followup" / "config" / "rules.json"
            p.unlink()
            p.mkdir()
            r = run_main([f"--today={TODAY}", "--force-push"], sheet())
            self.assertEqual(r.code, 2)
            self.assertNotIn("Traceback", r.err)


class FieldTypeTest(unittest.TestCase):
    """顶层是对象只是及格线，关键字段的类型也得对。"""

    def _run_bad(self, *, ledgers=None, rules=None, output=None):
        with temp_home(ledgers=ledgers, rules=rules, output=output):
            return run_main([f"--today={TODAY}", "--force-push"], sheet())

    def test_ledgers_field_is_object_not_array(self):
        r = self._run_bad(ledgers={"ledgers": {"id": "box"}})
        self.assertEqual(r.code, 2)
        self.assertIn("应该是数组", r.err)

    def test_ledger_entry_is_a_string(self):
        r = self._run_bad(ledgers={"ledgers": ["box"]})
        self.assertEqual(r.code, 2)
        self.assertIn("应该是对象", r.err)

    def test_ledger_missing_id(self):
        """id 是状态文件名的一部分，为空会让快照写成 snapshot_last_.json。"""
        cfg = ledgers_cfg()
        cfg["ledgers"][0].pop("id")
        r = self._run_bad(ledgers=cfg)
        self.assertEqual(r.code, 2)
        self.assertIn("缺少 id", r.err)

    def test_nodes_is_a_string(self):
        r = self._run_bad(rules={"rulesets": {"box": {"nodes": "收资"}}})
        self.assertEqual(r.code, 2)
        self.assertIn("nodes 应该是数组", r.err)

    def test_threshold_is_a_number_not_object(self):
        """`"threshold": 7` 是很自然的手误，而它会让边界口径整个读不到。"""
        rules = rules_cfg()
        rules["rulesets"]["box"]["nodes"][0]["threshold"] = 7
        r = self._run_bad(rules=rules)
        self.assertEqual(r.code, 2)
        self.assertIn("threshold 应该是对象", r.err)

    def test_threshold_days_not_a_number(self):
        rules = rules_cfg()
        rules["rulesets"]["box"]["nodes"][0]["threshold"] = {"days": "一周"}
        r = self._run_bad(rules=rules)
        self.assertEqual(r.code, 2)
        self.assertIn("不是数字", r.err)

    def test_bad_boundary_value(self):
        """写错 boundary 会静默改口径 —— 必须报错，不许猜。"""
        rules = rules_cfg()
        rules["rulesets"]["box"]["nodes"][0]["threshold"] = {
            "days": 7, "boundary": "满7天"}
        r = self._run_bad(rules=rules)
        self.assertEqual(r.code, 2)
        self.assertIn("boundary", r.err)

    def test_output_segment_is_a_string(self):
        cfg = output_cfg()
        cfg["wecom_webhook"] = "https://example.com"
        r = self._run_bad(output=cfg)
        self.assertEqual(r.code, 2)
        self.assertIn("wecom_webhook 应该是对象", r.err)

    def test_good_config_still_passes(self):
        """基线：别把校验写得太严，把正常配置也拦下来。"""
        with temp_home():
            r = run_main([f"--today={TODAY}", "--force-push"], sheet())
            self.assertEqual(r.code, 0, r.err)


# 「诊断模式撞上坏配置也不许发不许写」在 test_readonly.py，那是只读闸门的职责


class DoctorDoesNotCrashTest(unittest.TestCase):
    """
    诊断工具在被诊断的东西坏掉时自己崩掉，等于没有诊断工具。

    旧实现里 check_configs 后面每一行都在 .get() 链上，
    `"ledgers": {}` 会让 doctor 本身抛 AttributeError。
    """

    def _doctor_on(self, name: str, raw: str) -> tuple[int, str]:
        with temp_home() as home:
            write_cfg(home, name, raw)
            return run_doctor(["--validate-config"])

    def test_top_level_array(self):
        code, out = self._doctor_on("ledgers.json", "[]")
        self.assertEqual(code, 2)
        self.assertIn("顶层应该是一个 JSON 对象", out)

    def test_ledgers_is_object(self):
        code, out = self._doctor_on(
            "ledgers.json", '{"ledgers": {"id": "box"}}')
        self.assertEqual(code, 2)
        self.assertIn("应该是数组", out)
        self.assertIn("后续检查全部跳过", out)

    def test_rulesets_is_array(self):
        code, out = self._doctor_on("rules.json", '{"rulesets": []}')
        self.assertEqual(code, 2)
        self.assertIn("应该是对象", out)

    def test_broken_json(self):
        code, out = self._doctor_on("rules.json", "{ 坏 ")
        self.assertEqual(code, 2)
        self.assertIn("不是合法 JSON", out)

    def test_good_config_passes_validation(self):
        with temp_home():
            code, out = run_doctor(["--validate-config"])
            self.assertEqual(code, 0, out)
            self.assertIn("配置结构校验通过", out)


if __name__ == "__main__":
    unittest.main()
