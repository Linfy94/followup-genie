#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
`when` / `scope_filters` 条件写坏时，必须**离线**就拦下来。

═══════════════════════════════════════════════════════════════════════
业务会自己改催办节点（改天数、改状态取值、增删节点），所以这些写法迟早会出现。
它们的共同点是：**合法 JSON、离线校验放行、运行时也不报错**，
只是让那个节点从此不再产生催办 —— 与 rc9 修的 `{"days": -3}` 一模一样，
配置看起来是配过的，实际已经失效。

    {"op": "in"} 漏写 values      → `取值 in []` 恒假，节点永不命中
    {"op": "contains"} 漏写 value → `"" in 取值` 恒真，节点吞掉所有项目，
                                     后面的节点全部轮空
    条件缺 field                   → 取值恒为空串，判据静默失效
    启用节点 when: []              → `conds and all(...)` 恒假，节点永不命中
    enabled: "false"               → 字符串是真值，想停用却把节点静默开着
    op 写错                        → 运行时抛错退出 1（可见，但要等到次日 9:00）

前四类一条错误日志都不会有，业务只是再也收不到那条提醒。

🔴 `scope_filters` 走的是**同一个** match_condition，所以同样的写法在那边
   也会失效，而且后果更大：整张台账被过滤光，表现只是「今天没有要催的」。
   因此两处共用同一份 condition_errors —— 不各写一遍（坑 #12 已中三次）。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import unittest

from harness import ledgers_cfg, rules_cfg, output_cfg   # 先导它，它把 scripts/ 挂上 sys.path

import core


def cfg_with_when(when, *, enabled=True):
    """把 box 规则集的第一个节点换成给定的 when，其余保持真实形状。"""
    rules = rules_cfg()
    node = rules["rulesets"]["box"]["nodes"][0]
    node["when"] = when
    node["enabled"] = enabled
    return rules


def errors(rules=None, ledgers=None):
    return core.validate_configs(ledgers or ledgers_cfg(),
                                 rules if rules is not None else rules_cfg(),
                                 output_cfg())


def joined(errs) -> str:
    return "\n".join(errs)


class BaselineTest(unittest.TestCase):
    """先证明这套配置本来是干净的 —— 否则下面每一条「报错了」都不成立。"""

    def test_real_shaped_config_passes(self):
        self.assertEqual(errors(), [])

    def test_every_op_actually_used_in_production_is_accepted(self):
        """
        生产配置实测用到 in / empty / equals / not_equals / not_empty /
        contains / not_contains 七种。收紧校验最容易的翻车方式是把
        **正在用的写法**也判成错，那会让每天的催办直接停摆。
        """
        for cond in ({"field": "技术确认", "op": "in", "values": ["待收资"]},
                     {"field": "技术确认", "op": "empty"},
                     {"field": "技术确认", "op": "not_empty"},
                     {"field": "技术确认", "op": "equals", "value": "可行"},
                     {"field": "技术确认", "op": "not_equals", "value": "可行"},
                     {"field": "备注", "op": "contains", "value": "已完成"},
                     {"field": "备注", "op": "not_contains", "value": "已完成"}):
            with self.subTest(op=cond["op"]):
                self.assertEqual(errors(cfg_with_when([cond])), [],
                                 f"{cond['op']} 是生产在用的写法，不能被判成错")

    def test_op_defaults_to_equals_when_omitted(self):
        """不写 op 时 match_condition 按 equals 处理，校验必须跟着这条默认走。"""
        self.assertEqual(errors(cfg_with_when([{"field": "技术确认",
                                                "value": "可行"}])), [])


class SilentlyNeverMatchesTest(unittest.TestCase):
    """🔴 这一组是本文件的核心：全部静默、全部不报错。"""

    def test_in_without_values(self):
        errs = errors(cfg_with_when([{"field": "技术确认", "op": "in"}]))
        self.assertTrue(errs)
        self.assertIn("values", joined(errs))
        self.assertIn("恒假", joined(errs), "要说清后果，不能只说「格式不对」")

    def test_in_with_empty_values(self):
        self.assertTrue(errors(cfg_with_when(
            [{"field": "技术确认", "op": "in", "values": []}])))

    def test_not_in_without_values(self):
        self.assertTrue(errors(cfg_with_when([{"field": "技术确认",
                                               "op": "not_in"}])))

    def test_contains_without_value(self):
        errs = errors(cfg_with_when([{"field": "备注", "op": "contains"}]))
        self.assertTrue(errs)
        self.assertIn("恒真", joined(errs))

    def test_equals_without_value(self):
        self.assertTrue(errors(cfg_with_when([{"field": "技术确认",
                                               "op": "equals"}])))

    def test_condition_without_field(self):
        errs = errors(cfg_with_when([{"op": "not_empty"}]))
        self.assertTrue(errs)
        self.assertIn("field", joined(errs))

    def test_enabled_node_with_empty_when(self):
        errs = errors(cfg_with_when([]))
        self.assertTrue(errs)
        self.assertIn("恒假", joined(errs))
        self.assertIn("enabled: false", joined(errs), "要给出正确的停用写法")

    def test_disabled_node_with_empty_when_is_fine(self):
        """停用的节点允许字段不全 —— 沿用 repeat 那条既有口径，不许收得更严。"""
        self.assertEqual(errors(cfg_with_when([], enabled=False)), [])


class EnabledMustBeBooleanTest(unittest.TestCase):

    def test_string_false_is_rejected(self):
        """🔴 字符串 "false" 是**真**值，想停用却把节点静默开着。"""
        errs = errors(cfg_with_when([{"field": "技术确认", "op": "empty"}],
                                    enabled="false"))
        self.assertTrue(errs)
        self.assertIn("enabled", joined(errs))

    def test_string_true_is_rejected_too(self):
        self.assertTrue(errors(cfg_with_when(
            [{"field": "技术确认", "op": "empty"}], enabled="true")))

    def test_missing_enabled_is_still_allowed(self):
        """缺 enabled 一直等同于停用，本轮不改这条 —— 收紧要单独决定。"""
        rules = rules_cfg()
        for n in rules["rulesets"]["box"]["nodes"]:
            n.pop("enabled", None)
        self.assertEqual(errors(rules), [])


class BadOpTest(unittest.TestCase):

    def test_typo_op_is_caught_offline(self):
        errs = errors(cfg_with_when([{"field": "技术确认", "op": "contain",
                                      "value": "可行"}]))
        self.assertTrue(errs, "现在只有运行时才炸，要等到次日 9:00 才知道")
        self.assertIn("contain", joined(errs))

    def test_error_lists_the_supported_ops(self):
        """报错要顺手给出正确答案，否则业务只知道错了、不知道该写什么。"""
        errs = errors(cfg_with_when([{"field": "技术确认", "op": "eq"}]))
        self.assertIn("not_contains", joined(errs))

    def test_offline_and_runtime_share_one_op_list(self):
        """
        🔴 校验必须离线一道 + 运行时一道，但**共用同一份**（坑 #12 已中三次）。
           这里直接钉住：match_condition 认的 op，与 CONDITION_OPS 一字不差。
        """
        accepted = set()
        for op in list(core.CONDITION_OPS) + ["contain", "eq", "包含"]:
            try:
                core.match_condition(lambda f: "x",
                                     {"field": "f", "op": op, "value": "x",
                                      "values": ["x"]})
                accepted.add(op)
            except core.LedgerError:
                pass
        self.assertEqual(accepted, set(core.CONDITION_OPS))


class ShapeTest(unittest.TestCase):
    """条件本身不是对象 / 数组时，也不许裸崩。"""

    def test_when_is_a_string(self):
        self.assertTrue(errors(cfg_with_when("技术确认为空")))

    def test_condition_is_a_string(self):
        self.assertTrue(errors(cfg_with_when(["技术确认为空"])))

    def test_values_is_a_string_not_a_list(self):
        self.assertTrue(errors(cfg_with_when(
            [{"field": "技术确认", "op": "in", "values": "待收资"}])))

    def test_value_is_a_number(self):
        """台账取值一律按文本比对，数字 7 与文本「7」不相等 —— 也是静默的。"""
        errs = errors(cfg_with_when(
            [{"field": "序号", "op": "equals", "value": 7}]))
        self.assertTrue(errs)
        self.assertIn("字符串", joined(errs))

    def test_empty_op_carrying_a_stray_value_is_flagged(self):
        """`{"op":"empty","value":"待收资"}` 读起来像「等于待收资」，实际只判空。"""
        self.assertTrue(errors(cfg_with_when(
            [{"field": "技术确认", "op": "empty", "value": "待收资"}])))


class ScopeFiltersTest(unittest.TestCase):
    """🔴 同一批写法用在 scope_filters 上，后果是整张台账被过滤光。"""

    def _with(self, filters):
        led = ledgers_cfg()
        led["ledgers"][0]["scope_filters"] = filters
        return errors(ledgers=led)

    def test_in_without_values_filters_the_whole_ledger_away(self):
        errs = self._with([{"field": "地点", "op": "in"}])
        self.assertTrue(errs)
        self.assertIn("整张台账", joined(errs))

    def test_bad_op(self):
        self.assertTrue(self._with([{"field": "地点", "op": "包含",
                                     "value": "杭州"}]))

    def test_missing_field(self):
        self.assertTrue(self._with([{"op": "in", "values": ["杭州"]}]))

    def test_real_scope_filter_still_passes(self):
        self.assertEqual(
            self._with([{"field": "地点", "op": "in",
                         "values": ["杭州", "深圳"]}]), [])

    def test_no_scope_filters_at_all_is_fine(self):
        led = ledgers_cfg()
        led["ledgers"][0].pop("scope_filters")
        self.assertEqual(errors(ledgers=led), [])


class DoctorReportsItTest(unittest.TestCase):
    """离线校验的价值在于 doctor 会讲出来，否则等于没查。"""

    def test_validate_config_surfaces_the_error(self):
        from harness import temp_home, run_doctor
        with temp_home(rules=cfg_with_when([{"field": "技术确认", "op": "in"}])):
            code, out = run_doctor(["--validate-config"])
        self.assertNotEqual(code, 0, "配置会让节点静默失效，不该报「通过」")
        self.assertIn("values", out)


if __name__ == "__main__":
    unittest.main()
