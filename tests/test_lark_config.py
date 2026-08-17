#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞书台账的配置校验：写错了必须停下来，而且要说人话。

═══════════════════════════════════════════════════════════════════════
这一类错误此前的表现是**裸 traceback**：

    lark_base.read_sheet(l["base_token"], ...)   → KeyError: 'base_token'
    for spec in link_date_fields: spec["link_field"]  → KeyError / TypeError

业务看到的是一屏英文堆栈，完全不知道该改哪个文件的哪一行。

还有两种更坏的，**根本不报错**：
  · 两份台账用同一个 id → 共用状态文件 → 互相静音，而总数照样对得上
  · cross_ledger 的字段名写错 → 目标取值集合是空的 → 「一条都没进主台账」
    → 那个节点永远催下去
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import unittest
from unittest import mock

from harness import core, make_sheet, row  # noqa: F401 —— 也为挂 sys.path

from qqdoc import LedgerError  # noqa: E402


def lark_ledger(**over):
    l = {
        "id": "sentinel_lark", "name": "飞书主台账", "enabled": True,
        "source": "lark_cli", "base_token": "bascnXXXX",
        "table_id": "tblXXXX", "profile": "sentinel",
        "ruleset": "rs", "key_field": "_record_id", "name_field": "企业名称",
    }
    l.update(over)
    return l


def cfgs(ledgers, rules=None):
    return ({"ledgers": ledgers},
            rules or {"rulesets": {"rs": {"nodes": []}}},
            {})


def errors(ledgers, rules=None):
    return core.validate_configs(*cfgs(ledgers, rules))


class LarkRequiredFieldsTest(unittest.TestCase):
    """缺字段要在自检时说清楚，不是等取数时抛 KeyError。"""

    def test_complete_config_passes(self):
        self.assertEqual(errors([lark_ledger()]), [])

    def test_missing_base_token(self):
        e = errors([lark_ledger(base_token="")])
        self.assertTrue(any("base_token" in x for x in e), e)

    def test_missing_table_id(self):
        e = errors([lark_ledger(table_id=None)])
        self.assertTrue(any("table_id" in x for x in e), e)

    def test_missing_profile(self):
        """
        profile 决定用谁的授权去读。缺了以前会默默套一个默认值 ——
        默认值恰好对得上时没事，对不上就是「读不到，像是没数据」。
        """
        d = lark_ledger()
        del d["profile"]
        e = errors([d])
        self.assertTrue(any("profile" in x for x in e), e)

    def test_message_names_the_ledger(self):
        e = errors([lark_ledger(base_token="")])
        self.assertTrue(any("飞书主台账" in x for x in e),
                        f"报错要指名是哪一份台账，否则多台账时无从下手：{e}")

    def test_unknown_source_is_rejected(self):
        # 这条原来拿 "wecom_doc" 当「不支持的 source」举例 —— 0.4.0-rc10 起
        # 它是真的支持了，再拿它当反例就变成在测「企微台账缺 url」。
        # 换一个不会成真的值。
        e = errors([lark_ledger(source="根本不存在的数据源")])
        self.assertTrue(any("根本不存在的数据源" in x for x in e), e)
        self.assertTrue(
            any("tencent_mcp" in x and "lark_cli" in x and "wecom_doc" in x for x in e),
            f"要列出支持哪些，否则不知道该填什么：{e}")

    def test_tencent_ledger_still_needs_its_own_fields(self):
        e = errors([{"id": "box", "source": "tencent_mcp", "enabled": True,
                     "file_id": "", "sheet_id": "000001"}])
        self.assertTrue(any("file_id" in x for x in e), e)


class LinkDateFieldsTest(unittest.TestCase):

    def test_valid_spec_passes(self):
        self.assertEqual(errors([lark_ledger(link_date_fields=[
            {"link_field": "发货表", "child_table_id": "tblC",
             "child_date_field": "发货时间"}])]), [])

    def test_absent_is_fine(self):
        self.assertEqual(errors([lark_ledger()]), [])

    def test_not_a_list(self):
        """整段写成对象时，for 会去遍历它的键，spec 变成字符串 → TypeError。"""
        e = errors([lark_ledger(link_date_fields={"link_field": "x"})])
        self.assertTrue(any("应该是数组" in x for x in e), e)

    def test_item_not_an_object(self):
        e = errors([lark_ledger(link_date_fields=["发货表"])])
        self.assertTrue(any("应该是对象" in x for x in e), e)

    def test_missing_child_date_field(self):
        e = errors([lark_ledger(link_date_fields=[
            {"link_field": "发货表", "child_table_id": "tblC"}])])
        self.assertTrue(any("child_date_field" in x for x in e), e)

    def test_reports_which_item(self):
        e = errors([lark_ledger(link_date_fields=[
            {"link_field": "a", "child_table_id": "b", "child_date_field": "c"},
            {"link_field": "d"}])])
        self.assertTrue(any("第 2 项" in x for x in e),
                        f"多项时要说清是第几项：{e}")


class DuplicateLedgerIdTest(unittest.TestCase):
    """
    🔴 重复的 id 不报错，只会让两份台账共用同一套状态文件 ——
    stage_entered / followup_state / 快照全部串台，
    一份的「已通知」把另一份静音掉，而各项总数照样对得上。
    """

    def test_duplicate_is_rejected(self):
        e = errors([lark_ledger(), lark_ledger(name="另一份")])
        self.assertTrue(any("两份台账都叫" in x for x in e), e)

    def test_message_says_why_it_matters(self):
        e = errors([lark_ledger(), lark_ledger(name="另一份")])
        joined = "\n".join(e)
        self.assertIn("状态", joined, "要说清后果，不然会以为只是洁癖")

    def test_distinct_ids_pass(self):
        self.assertEqual(errors([lark_ledger(),
                                 lark_ledger(id="other", name="另一份")]), [])


class CrossLedgerRefTest(unittest.TestCase):

    def rules(self, cross):
        # when 不能省：启用的节点空条件恒假、永远不命中，离线校验会拦
        # （见 test_condition_malformed）。这里只是要一个形状真实的节点，
        # 好让 cross_ledger 的校验有东西可挂。
        return {"rulesets": {"rs": {"nodes": [
            {"id": "reg", "name": "③分行扫码登记", "enabled": True,
             "when": [{"field": "安装情况", "op": "empty"}],
             "threshold": {"days": 3}, "repeat": {"days": 1},
             "cross_ledger": cross},
        ]}}}

    def test_valid_reference_passes(self):
        e = errors([lark_ledger(), lark_ledger(id="qq", name="前期台账")],
                   self.rules({"ledger_id": "qq", "match_field": "企业名称",
                               "target_field": "企业名称"}))
        self.assertEqual(e, [])

    def test_dangling_reference_is_rejected(self):
        e = errors([lark_ledger()],
                   self.rules({"ledger_id": "打错的id",
                               "target_field": "企业名称"}))
        self.assertTrue(any("打错的id" in x for x in e), e)
        self.assertTrue(any("永远催" in x for x in e),
                        f"要说清后果 —— 查不到目标台账时这个节点不会停：{e}")

    def test_missing_ledger_id(self):
        e = errors([lark_ledger()], self.rules({"target_field": "企业名称"}))
        self.assertTrue(any("缺少 ledger_id" in x for x in e), e)

    def test_missing_both_field_names(self):
        e = errors([lark_ledger()], self.rules({"ledger_id": "sentinel_lark"}))
        self.assertTrue(any("target_field" in x for x in e), e)

    def test_cross_ledger_not_an_object(self):
        e = errors([lark_ledger()], self.rules("sentinel_lark"))
        self.assertTrue(any("应该是对象" in x for x in e), e)

    def test_valid_multi_field_reference_passes(self):
        cross = {
            "ledger_id": "qq",
            "match_fields": [
                {"local_field": "企业名称", "target_field": "企业名称"},
                {"local_field": "分行", "target_field": "分行/地区",
                 "normalize_map": {"杭州": "杭州", "杭州分行": "杭州"}},
            ],
        }
        e = errors([lark_ledger(), lark_ledger(id="qq", name="前期台账")],
                   self.rules(cross))
        self.assertEqual(e, [])

    def test_mixed_or_invalid_match_fields_are_rejected(self):
        for value in ({}, [], ["企业名称"],
                      [{"local_field": "企业名称"}],
                      [{"local_field": "企业名称", "target_field": "企业名称",
                        "normalize_map": {"杭州": 1}}]):
            with self.subTest(value=value):
                e = errors([lark_ledger()], self.rules({
                    "ledger_id": "sentinel_lark", "match_fields": value}))
                self.assertTrue(any("cross_ledger 配置无效" in x for x in e), e)


class CrossLedgerFieldMustExistTest(unittest.TestCase):
    """
    离线只能查「台账在不在」，「列在不在」要等读到表。
    而这一层坏掉的方式最隐蔽：取值集合是空的，判定照跑，节点永远催。
    """

    def test_missing_target_field_raises(self):
        target = {"id": "qq", "name": "前期台账", "source": "tencent_mcp",
                  "name_field": "项目名称"}
        sheet = make_sheet([row(1, "甲公司")])
        orig = core.read_ledger_sheet
        core.read_ledger_sheet = lambda l: sheet
        try:
            with self.assertRaises(LedgerError) as cm:
                core._cross_ledger_values(
                    {"ledger_id": "qq", "target_field": "并不存在的列"},
                    {"qq": target}, {})
        finally:
            core.read_ledger_sheet = orig
        msg = str(cm.exception)
        self.assertIn("并不存在的列", msg)
        self.assertIn("永远催", msg, "要说清后果，否则会被当成小问题")

    def test_existing_field_returns_values(self):
        target = {"id": "qq", "name": "前期台账", "source": "tencent_mcp",
                  "name_field": "项目名称"}
        sheet = make_sheet([row(1, "甲公司"), row(2, "乙公司")])
        orig = core.read_ledger_sheet
        core.read_ledger_sheet = lambda l: sheet
        try:
            index, specs, _guard = core._cross_ledger_values(
                {"ledger_id": "qq", "target_field": "项目名称"},
                {"qq": target}, {})
        finally:
            core.read_ledger_sheet = orig
        self.assertEqual(index, {("甲公司",): [None], ("乙公司",): [None]})
        self.assertEqual(specs[0]["target_field"], "项目名称")

    def test_scope_filter_is_applied_before_indexing(self):
        target = {"id": "qq", "name": "前期台账", "source": "tencent_mcp",
                  "name_field": "项目名称",
                  "scope_filters": [{"field": "地点", "op": "equals", "value": "杭州"}]}
        sheet = make_sheet([
            row(1, "范围内公司", place="杭州"),
            row(2, "范围外公司", place="宁波"),
        ])
        with mock.patch.object(core, "read_ledger_sheet", return_value=sheet):
            index, _, _guard = core._cross_ledger_values(
                {"ledger_id": "qq", "target_field": "项目名称"},
                {"qq": target}, {})
        self.assertEqual(index, {("范围内公司",): [None]})

    def test_cache_key_includes_target_fields(self):
        target = {"id": "qq", "name": "前期台账", "source": "tencent_mcp",
                  "name_field": "项目名称"}
        sheet = make_sheet([row(1, "甲公司", place="杭州")])
        cache = {}
        with mock.patch.object(core, "read_ledger_sheet", return_value=sheet) as read:
            names, _, _g1 = core._cross_ledger_values(
                {"ledger_id": "qq", "target_field": "项目名称"}, {"qq": target}, cache)
            regions, _, _g2 = core._cross_ledger_values(
                {"ledger_id": "qq", "target_field": "地点"}, {"qq": target}, cache)
        self.assertEqual(names, {("甲公司",): [None]})
        self.assertEqual(regions, {("杭州",): [None]})
        self.assertEqual(read.call_count, 2)


class PureLarkNeedsNoTencentTokenTest(unittest.TestCase):
    """
    🔴 纯飞书用户没有、也不该有 TENCENT_DOCS_TOKEN。

    以前自检无条件查它，会给这类用户一条红色的「腾讯文档凭证缺失」。
    自检报红是最强的「别用」信号 —— 业务会卡在一件跟他完全无关的事情上，
    而正确的做法本来就是不配。
    """

    def setUp(self):
        import doctor
        self.doctor = doctor

    def test_lark_only_does_not_need_it(self):
        self.assertFalse(self.doctor.needs_tencent_token([lark_ledger()]))

    def test_any_tencent_ledger_needs_it(self):
        self.assertTrue(self.doctor.needs_tencent_token(
            [lark_ledger(), {"id": "box", "source": "tencent_mcp"}]))

    def test_source_defaults_to_tencent(self):
        """没写 source 的台账算腾讯文档 —— 默认值不能在这里和取数层分家。"""
        self.assertTrue(self.doctor.needs_tencent_token([{"id": "box"}]))

    def test_empty_list(self):
        self.assertFalse(self.doctor.needs_tencent_token([]))

    def test_junk_entries_do_not_crash(self):
        self.assertFalse(self.doctor.needs_tencent_token([lark_ledger(), "坏数据"]))


if __name__ == "__main__":
    unittest.main()
