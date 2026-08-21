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

from harness import core, ledgers_cfg, rules_cfg

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


class TieBreakerNormalizeTest(unittest.TestCase):
    """
    key_tiebreakers 的解析。

    🔴 2026-08-21 复审发现：非法值（数字/对象/混合数组）原来会被静默过滤
    成空列表——配置打错，消歧功能悄悄不生效，等真撞车了才在运行时暴露成
    主键重复的致命错，那时完全看不出根因在这里。改成非法输入直接报错，
    这条测试类原来叫 test_junk_is_dropped_defensively、断言「不抛异常即可」，
    这次连同行为一起改。
    """

    def test_absent_is_empty(self):
        self.assertEqual(core.key_tiebreakers({}), [])

    def test_single_string(self):
        self.assertEqual(core.key_tiebreakers({"key_tiebreakers": "目标国家地区"}),
                         ["目标国家地区"])

    def test_array_form(self):
        self.assertEqual(
            core.key_tiebreakers({"key_tiebreakers": ["目标国家地区", "机构"]}),
            ["目标国家地区", "机构"])

    def test_empty_array_is_fine(self):
        self.assertEqual(core.key_tiebreakers({"key_tiebreakers": []}), [])

    def test_blank_string_scalar_means_not_configured(self):
        """单个空白字符串标量，跟 key_field 的既有宽容度一致：当没配。"""
        self.assertEqual(core.key_tiebreakers({"key_tiebreakers": "  "}), [])

    def test_non_string_scalar_errors(self):
        """🔴 这几种在旧实现里会静默变成 []，新实现要报错。"""
        with self.assertRaises(ValueError):
            core.key_tiebreakers({"key_tiebreakers": 7})
        with self.assertRaises(ValueError):
            core.key_tiebreakers({"key_tiebreakers": {"a": 1}})

    def test_array_with_non_string_element_errors(self):
        with self.assertRaises(ValueError):
            core.key_tiebreakers({"key_tiebreakers": [3, "目标国家地区"]})

    def test_array_with_blank_string_element_errors(self):
        """数组里每一项都该是精心列出的字段名，空字符串大概率是笔误。"""
        with self.assertRaises(ValueError):
            core.key_tiebreakers({"key_tiebreakers": ["", "  "]})


class TieBreakerRowKeyTest(unittest.TestCase):
    """
    key_tiebreakers：有值才拼进主键帮助消歧，没值完全不影响。

    ═══════════════════════════════════════════════════════════════════
    🔴 2026-08-20 接 AI外贸拓客台账实测：某个真实客户同一天
       有多个不同方向的项目并行跟进（企业+机构+访客需求时间三者全同），
       业务确认「每一行都是不同的项目」——不能挑一条留、也不能都不追踪。
       唯一能区分的字段是「目标国家地区」，但它同时是①客户填表节点要催
       的东西：全新线索这一列本来就是空的。塞进 key_field（必填）会让
       最该被催的新线索因为空主键报致命错；不塞，同一天并行的项目会撞车。
    ═══════════════════════════════════════════════════════════════════
    """

    def _sheet(self, rows):
        return FakeSheet(["企业", "机构", "访客时间", "目标国家地区"], rows)

    def test_tiebreaker_present_disambiguates_otherwise_identical_rows(self):
        s = self._sheet([
            {"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
             "目标国家地区": "欧洲"},
            {"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
             "目标国家地区": "日本"},
        ])
        k1 = core.row_key(s, 1, ["企业", "机构", "访客时间"], ["目标国家地区"])
        k2 = core.row_key(s, 2, ["企业", "机构", "访客时间"], ["目标国家地区"])
        self.assertNotEqual(k1, k2)
        self.assertTrue(k1 and k2, "两边都该有值，不该因为加了消歧字段反而变空")

    def test_tiebreaker_blank_does_not_blank_the_key(self):
        """🔴 这条是本能力存在的全部意义：消歧字段没填，主键不能因此作废。"""
        s = self._sheet([
            {"企业": "乙公司", "机构": "深圳分行", "访客时间": "46200",
             "目标国家地区": ""},
        ])
        k = core.row_key(s, 1, ["企业", "机构", "访客时间"], ["目标国家地区"])
        self.assertEqual(k, "乙公司|深圳分行|46200")

    def test_without_tiebreaker_configured_nothing_changes(self):
        """不传 tiebreakers（默认 None）时，行为必须跟这个能力出现之前完全一样。"""
        s = self._sheet([
            {"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
             "目标国家地区": "欧洲"},
        ])
        self.assertEqual(core.row_key(s, 1, ["企业", "机构", "访客时间"]),
                         "甲公司|杭州分行|46202")

    def test_still_collides_when_tiebreaker_is_also_blank_on_both_sides(self):
        """
        消歧字段帮不上忙时（两行都没填），不能假装分开了 ——
        真撞车就该被撞车检测抓住，这条能力不许把它悄悄摸平。
        """
        s = self._sheet([
            {"企业": "丙公司", "机构": "杭州分行", "访客时间": "46210",
             "目标国家地区": ""},
            {"企业": "丙公司", "机构": "杭州分行", "访客时间": "46210",
             "目标国家地区": ""},
        ])
        k1 = core.row_key(s, 1, ["企业", "机构", "访客时间"], ["目标国家地区"])
        k2 = core.row_key(s, 2, ["企业", "机构", "访客时间"], ["目标国家地区"])
        self.assertEqual(k1, k2, "两边消歧字段都是空的，就该继续撞车，不该被强行分开")

    def test_separator_does_not_collide_with_the_pipe(self):
        """消歧字段用不同分隔符，避免跟必填段的 | 凑巧拼出同一个字符串。"""
        s = self._sheet([{"企业": "甲", "机构": "乙", "访客时间": "1",
                         "目标国家地区": "丙"}])
        k = core.row_key(s, 1, ["企业", "机构", "访客时间"], ["目标国家地区"])
        self.assertEqual(k, "甲|乙|1‖丙")
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



class EvaluateLedgerAmbiguityFreezeTest(unittest.TestCase):
    """
    evaluate_ledger()：撞车触发歧义冻结时，既不猜 key、也不碰任何状态文件。

    ═══════════════════════════════════════════════════════════════════
    🔴 2026-08-21 P0 复现场景②：一个项目（甲公司|杭州分行|46202）已经在
    stage_entered/followup_state 里留了一条不带后缀的历史记录、正处在
    超期催办周期中；今天新增了一行同基础 key 的记录（目标国家地区＝
    「日本」，一条全新的、真实存在的并行线索）。这一撞车让两行的基础
    key 从"历史上唯一"变成"现在有两行"——程序层面无法判定旧记录到底
    对应哪一行，必须整体冻结、只报人工核对，而不是给两行都套上新 key
    （那样会让旧记录变孤儿、两行都被当成新项目各催一次）。
    ═══════════════════════════════════════════════════════════════════
    """

    def _sheet(self, rows):
        return FakeSheet(["企业", "机构", "访客时间", "目标国家地区", "已确认"], rows)

    def _ledger(self):
        l = base_ledger(
            key_field=["企业", "机构", "访客时间"], name_field="企业",
            required_columns=["企业", "机构", "访客时间", "目标国家地区", "已确认"],
            key_tiebreakers=["目标国家地区"])
        l["id"] = "trade_qq"
        return l

    def _ruleset(self):
        return {"nodes": [{
            "id": "fillin", "name": "客户填表", "enabled": True,
            "when": [{"field": "已确认", "op": "empty"}],
            "clock": {"field": "访客时间"},
            "threshold": {"days": 0, "boundary": "on"},
            "repeat": {"days": 1},
        }]}

    def test_colliding_rows_are_frozen_not_silently_rekeyed(self):
        s = self._sheet([
            {"企业": "甲公司", "机构": "杭州分行", "访客时间": TODAY,
             "目标国家地区": "", "已确认": ""},
            {"企业": "甲公司", "机构": "杭州分行", "访客时间": TODAY,
             "目标国家地区": "日本", "已确认": ""},
        ])
        base_key = "甲公司|杭州分行|" + core.iso(TODAY)
        state_key = f"trade_qq|{base_key}|fillin"
        stage_entered = {state_key: core.iso(TODAY)}
        followup_state = {state_key: {"first_overdue": core.iso(TODAY)}}
        stage_history: dict = {}
        wd = core.WorkdayCalc({"exclude_weekends": True, "exclude_holidays": False}, None)

        with mock.patch.object(core, "read_ledger_sheet", return_value=s):
            rep, _ = core.evaluate_ledger(
                self._ledger(), self._ruleset(), wd, TODAY,
                stage_entered, followup_state, {}, stage_history)

        self.assertEqual(rep.due, [], "撞车歧义时不该有任何一行照常催办")
        self.assertTrue(
            any("甲公司" in h for h in rep.review_hints), rep.review_hints)
        self.assertEqual(stage_entered, {state_key: core.iso(TODAY)},
                         "冻结场景绝不能改写/新增任何 state_key，历史记录必须原样保留")
        self.assertEqual(followup_state, {state_key: {"first_overdue": core.iso(TODAY)}})
        self.assertEqual(rep.identity_ambiguous, 2, "两行都被冻结，都要计进这个桶")
        self.assertEqual(
            rep.accounted, rep.total_rows,
            "🔴 2026-08-21 复审发现：冻结的行原来只写 review_hints，不计入任何桶，"
            "会让 accounted 少算、触发「各项之和 ≠ 总数，有行去向不明」的假警报——"
            "这批行不是去向不明，是主动暂停催办")

    def test_same_two_rows_without_prior_history_disambiguate_normally(self):
        """对照组：如果之前根本没有历史记录（比如两行本来就都是新的），不该被冻结。"""
        s = self._sheet([
            {"企业": "甲公司", "机构": "杭州分行", "访客时间": TODAY,
             "目标国家地区": "欧洲", "已确认": ""},
            {"企业": "甲公司", "机构": "杭州分行", "访客时间": TODAY,
             "目标国家地区": "日本", "已确认": ""},
        ])
        wd = core.WorkdayCalc({"exclude_weekends": True, "exclude_holidays": False}, None)
        with mock.patch.object(core, "read_ledger_sheet", return_value=s):
            rep, _ = core.evaluate_ledger(
                self._ledger(), self._ruleset(), wd, TODAY, {}, {}, {})
        self.assertEqual(len(rep.due), 2, "没有历史包袱时，两条并行线索该各自正常入催办")
        self.assertEqual(rep.review_hints, [])


class ScopeAwareKeyAssertionTest(unittest.TestCase):
    """
    🔴 主键断言只对**责任范围内**的行判致命。

    判定里只有范围内的行会产生催办状态（范围外的直接计入「范围外」跳过），
    所以范围外的空主键伤不到任何东西 —— 为它停掉整条业务线不成比例。

    2026-08-10 实测：GEO 表有 4 行把「提问关键词」误填进了企业列
    （无序号、无分行），责任范围内 0 行，却让整份 132 行的台账读不了。

    2026-08-18 业务决定：责任范围外的条目不再告警（此前会以「责任范围外
    还有 N 行…」的形式提示，业务反馈这类噪声没有意义 —— 反正不催）。
    真正的系统性读失败（整列读成空串）必然同时命中范围内的行，仍然致命。
    """

    def _assert(self, rows):
        s = FakeSheet(["序号", "企业", "分行", "已发货", "进入时间"], rows)
        l = base_ledger(required_columns=["序号", "企业", "分行", "已发货", "进入时间"])
        l["key_field"] = ["序号", "企业"]
        l["name_field"] = "企业"
        l["scope_filters"] = [{"field": "分行", "op": "in", "values": ["杭州分行"]}]
        return core.assert_sheet(s, l, ruleset())

    def _row(self, num, name, branch):
        return {"序号": num, "企业": name, "分行": branch,
                "已发货": "", "进入时间": TODAY}

    def test_blank_key_outside_scope_is_not_fatal_and_not_warned(self):
        """2026-08-18 业务决定：责任范围外的条目不告警，只是不致命。"""
        a = self._assert([
            self._row("1", "甲公司", "杭州分行"),
            self._row("", "提问关键词误填进企业列", ""),      # 范围外、没序号
        ])
        self.assertEqual([e for e in a.fatal if "空值" in e], [])
        self.assertEqual([w for w in a.warnings if "责任范围外" in w], [])

    def test_blank_key_inside_scope_is_still_fatal(self):
        """护栏的力度一点没减：范围内的空主键照样拦死。"""
        a = self._assert([
            self._row("1", "甲公司", "杭州分行"),
            self._row("", "乙公司", "杭州分行"),
        ])
        self.assertTrue(any("空值" in e for e in a.fatal), a.fatal)

    def test_systemic_read_failure_is_still_fatal(self):
        """整列读成空串（NUMBER 型用 string_value 读）必然命中范围内的行。"""
        a = self._assert([self._row("", "甲公司", "杭州分行"),
                          self._row("", "乙公司", "杭州分行")])
        self.assertTrue(any("空值" in e for e in a.fatal), a.fatal)

    def test_duplicate_outside_scope_is_not_fatal_and_not_warned(self):
        a = self._assert([
            self._row("1", "甲公司", "杭州分行"),
            self._row("9", "丙公司", "北京分行"),
            self._row("9", "丙公司", "北京分行"),
        ])
        self.assertEqual([e for e in a.fatal if "重复值" in e], [])
        self.assertEqual([w for w in a.warnings if "责任范围外" in w], [])

    def test_duplicate_inside_scope_is_still_fatal(self):
        a = self._assert([
            self._row("1", "甲公司", "杭州分行"),
            self._row("1", "甲公司", "杭州分行"),
        ])
        self.assertTrue(any("重复值" in e for e in a.fatal), a.fatal)

    def test_ledger_without_scope_filters_is_unchanged(self):
        """🔴 没配 scope_filters 的台账（盒子线以外都算）行为必须一字不变。"""
        s = FakeSheet(["序号", "企业", "已发货", "进入时间"],
                      [{"序号": "", "企业": "甲公司", "已发货": "", "进入时间": TODAY}])
        l = base_ledger(required_columns=["序号", "企业", "已发货", "进入时间"])
        l["key_field"] = ["序号", "企业"]
        l["name_field"] = "企业"
        a = core.assert_sheet(s, l, ruleset())
        self.assertTrue(any("空值" in e for e in a.fatal), a.fatal)


class ResolveRowKeysTest(unittest.TestCase):
    """
    resolve_row_keys()：tiebreakers 只在真的撞车时才参与 key。

    ═══════════════════════════════════════════════════════════════════
    🔴 2026-08-21 复审发现：直接给 row_key() 传 tiebreakers 会让 key 随
    消歧字段的值漂移——消歧字段（比如「目标国家地区」）往往是业务后填的，
    项目推进一步、这一列从空变有值，key 就跟着变，state_key 也跟着变，
    旧的 stage_entered / followup_state 记录在新 key 下查不到，项目被
    当成从没出现过，可能重发一次首次催办。

    实测复现：一个仍在静默期里的项目，只因为消歧字段被填上，第二天就
    重新进了待催清单。下面两条是这次修复要守住的两头：
      · 不撞车时，消歧字段怎么变，key 都不该变（这条测试要能在旧实现上
        真的翻红——旧实现里只要 tiebreakers 有值就往 key 上拼）
      · 真撞车时，两条记录依然要能被 tiebreakers 分开（不能因为堵了
        漂移这个洞，把「同一天并行多个项目」这个原始需求也堵死了）
    ═══════════════════════════════════════════════════════════════════
    """

    def _sheet(self, rows):
        return FakeSheet(["企业", "机构", "访客时间", "目标国家地区"], rows)

    def test_singleton_key_is_stable_even_after_tiebreaker_gets_filled_in(self):
        """🔴 本次修复要守住的核心场景：唯一一行，消歧字段从空变有值，key 不能变。"""
        s_before = self._sheet([
            {"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
             "目标国家地区": ""},
        ])
        s_after = self._sheet([
            {"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
             "目标国家地区": "欧洲"},
        ])
        fields = ["企业", "机构", "访客时间"]
        tb = ["目标国家地区"]
        k_before = core.resolve_row_keys(s_before, [1], fields, tb)[1]
        k_after = core.resolve_row_keys(s_after, [1], fields, tb)[1]
        self.assertEqual(k_before, k_after,
                         f"唯一一行不该因为消歧字段填了值就变 key：{k_before!r} vs {k_after!r}")
        self.assertEqual(k_before, "甲公司|杭州分行|46202",
                         "不撞车时 key 就该是纯基础 key，不带消歧字段的尾巴")

    def test_true_collision_still_gets_disambiguated(self):
        """真撞车（同一天并行两个项目）时，tiebreakers 仍要生效，两条不能共用一个 key。"""
        s = self._sheet([
            {"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
             "目标国家地区": "欧洲"},
            {"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
             "目标国家地区": "日本"},
        ])
        fields = ["企业", "机构", "访客时间"]
        tb = ["目标国家地区"]
        keys = core.resolve_row_keys(s, [1, 2], fields, tb)
        self.assertNotEqual(keys[1], keys[2], keys)
        self.assertTrue(keys[1] and keys[2], keys)

    def test_collision_with_blank_tiebreaker_on_both_sides_still_collides(self):
        """撞车了，但消歧字段两边都没填——帮不上忙，两条依然是同一个 key（撞车检测能抓住）。"""
        s = self._sheet([
            {"企业": "乙公司", "机构": "深圳分行", "访客时间": "46210",
             "目标国家地区": ""},
            {"企业": "乙公司", "机构": "深圳分行", "访客时间": "46210",
             "目标国家地区": ""},
        ])
        fields = ["企业", "机构", "访客时间"]
        tb = ["目标国家地区"]
        keys = core.resolve_row_keys(s, [1, 2], fields, tb)
        self.assertEqual(keys[1], keys[2])

    def test_no_tiebreakers_configured_falls_back_to_plain_key_fields(self):
        """没配 key_tiebreakers（tb=None/[]）时，行为必须跟这个能力出现之前完全一样。"""
        s = self._sheet([
            {"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
             "目标国家地区": "欧洲"},
        ])
        fields = ["企业", "机构", "访客时间"]
        self.assertEqual(core.resolve_row_keys(s, [1], fields, None)[1],
                         "甲公司|杭州分行|46202")
        self.assertEqual(core.resolve_row_keys(s, [1], fields, [])[1],
                         "甲公司|杭州分行|46202")

    def test_blank_base_key_rows_are_untouched(self):
        """基础主键本身就读到空值的行，resolve_row_keys 不该把它拼出一个假 key。"""
        s = self._sheet([
            {"企业": "", "机构": "杭州分行", "访客时间": "46202", "目标国家地区": "欧洲"},
        ])
        fields = ["企业", "机构", "访客时间"]
        self.assertEqual(core.resolve_row_keys(s, [1], fields, ["目标国家地区"])[1], "")


class ResolveRowKeysAmbiguityGuardTest(unittest.TestCase):
    """
    resolve_row_keys()：撞车前已有历史记录的基础 key，撞车后不能静默改名。

    ═══════════════════════════════════════════════════════════════════
    🔴 2026-08-21 第二轮复审指出的 P0：一个原本 singleton、已经在状态
    文件里留了不带后缀记录的项目，被新增的同基础 key 记录撞车后，
    旧实现不管三七二十一给两行都套上带后缀的新 key——历史记录在旧的
    不带后缀 key 下变成孤儿，两行都被当成"从没出现过"，新项目正确
    触发首次催办的同时，原来那个还在正常周期里的项目也被错误地当成
    新项目再催一次。

    这批测试直接对 resolve_row_keys() 断言：只要传了 ledger_id 与
    existing_state_keys，撞车前历史上是 singleton 的基础 key，撞车后
    该返回 None（"无法可靠判定，调用方必须整体跳过"），不能返回任何
    带后缀的猜测值。
    ═══════════════════════════════════════════════════════════════════
    """

    def _sheet(self, rows):
        return FakeSheet(["企业", "机构", "访客时间", "目标国家地区"], rows)

    def test_collision_after_prior_singleton_history_returns_none_for_both_rows(self):
        s = self._sheet([
            {"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
             "目标国家地区": ""},
            {"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
             "目标国家地区": "日本"},
        ])
        fields = ["企业", "机构", "访客时间"]
        tb = ["目标国家地区"]
        existing = {"trade_qq|甲公司|杭州分行|46202|客户填表"}
        keys = core.resolve_row_keys(s, [1, 2], fields, tb,
                                     ledger_id="trade_qq",
                                     existing_state_keys=existing)
        self.assertIsNone(keys[1], keys)
        self.assertIsNone(keys[2], keys)

    def test_collision_without_prior_history_is_unaffected(self):
        """撞车了，但这个基础 key 之前压根没在状态文件里出现过——正常消歧，不是歧义。"""
        s = self._sheet([
            {"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
             "目标国家地区": "欧洲"},
            {"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
             "目标国家地区": "日本"},
        ])
        fields = ["企业", "机构", "访客时间"]
        tb = ["目标国家地区"]
        keys = core.resolve_row_keys(s, [1, 2], fields, tb,
                                     ledger_id="trade_qq",
                                     existing_state_keys=set())
        self.assertIsNotNone(keys[1])
        self.assertIsNotNone(keys[2])
        self.assertNotEqual(keys[1], keys[2])

    def test_without_ledger_id_or_existing_keys_behavior_is_unchanged(self):
        """不传这两个新参数（其余调用方）时，永远不触发歧义分支——向后兼容。"""
        s = self._sheet([
            {"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
             "目标国家地区": ""},
            {"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
             "目标国家地区": "日本"},
        ])
        fields = ["企业", "机构", "访客时间"]
        tb = ["目标国家地区"]
        keys = core.resolve_row_keys(s, [1, 2], fields, tb)
        self.assertIsNotNone(keys[1])
        self.assertIsNotNone(keys[2])

    def test_different_ledger_id_prefix_does_not_false_positive(self):
        """historically_singleton 的判定要按 ledger_id 精确前缀匹配，不能跨台账误伤。"""
        s = self._sheet([
            {"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
             "目标国家地区": ""},
            {"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
             "目标国家地区": "日本"},
        ])
        fields = ["企业", "机构", "访客时间"]
        tb = ["目标国家地区"]
        existing = {"另一条台账|甲公司|杭州分行|46202|客户填表"}
        keys = core.resolve_row_keys(s, [1, 2], fields, tb,
                                     ledger_id="trade_qq",
                                     existing_state_keys=existing)
        self.assertIsNotNone(keys[1])
        self.assertIsNotNone(keys[2])


class TieBreakerFieldMustExistTest(unittest.TestCase):
    """
    key_tiebreakers 引用的列，字段名打错必须立即失败，不许悄悄失效。

    🔴 2026-08-21 复审发现：跟 scope_filters / terminal_states 这些配置字段
    不同，key_tiebreakers 原来没被纳入 assert_sheet 的 referenced 列存在性
    检查——字段名打错，sheet.text() 对不存在的列只会返回空字符串，消歧
    功能因此**悄悄**永远拿不到值、永远不生效，跟「没配置」表现完全一样。
    要等到真的出现并发撞车（这本来正是加 tiebreaker 要防的事）才会在运行时
    暴露成主键重复的致命错，那时候完全看不出根因是字段名打错了。
    """

    def test_misspelled_field_is_fatal(self):
        s = FakeSheet(["企业", "机构", "进入时间"],
                      [{"企业": "甲公司", "机构": "杭州分行", "进入时间": TODAY}])
        led = base_ledger(key_field=["企业", "机构"], name_field="企业",
                          required_columns=["企业", "机构", "进入时间"],
                          key_tiebreakers=["目标国家地区打错字"])
        a = core.assert_sheet(s, led, {"nodes": []})
        self.assertFalse(a.ok, "字段名打错不该悄悄通过")
        self.assertTrue(any("目标国家地区打错字" in e for e in a.fatal), a.fatal)

    def test_correct_field_name_is_not_flagged(self):
        """列名写对时一切照旧，不能误伤。"""
        s = FakeSheet(["企业", "机构", "进入时间", "目标国家地区"],
                      [{"企业": "甲公司", "机构": "杭州分行", "进入时间": TODAY,
                        "目标国家地区": ""}])
        led = base_ledger(key_field=["企业", "机构"], name_field="企业",
                          required_columns=["企业", "机构", "进入时间", "目标国家地区"],
                          key_tiebreakers=["目标国家地区"])
        a = core.assert_sheet(s, led, {"nodes": []})
        self.assertTrue(a.ok, a.fatal)


class TieBreakerOfflineValidationTest(unittest.TestCase):
    """key_tiebreakers 配错要在 --validate-config 当场报错，不许等到运行时才崩。"""

    def test_non_string_value_errors(self):
        errs = core.validate_configs(
            ledgers_cfg(key_tiebreakers=7), rules_cfg(), {})
        self.assertTrue(
            any("key_tiebreakers" in e for e in errs), errs)

    def test_well_formed_config_passes(self):
        errs = core.validate_configs(
            ledgers_cfg(key_tiebreakers=["目标国家地区"]), rules_cfg(), {})
        self.assertEqual([e for e in errs if "key_tiebreakers" in e], [])

    def test_absent_is_fine(self):
        errs = core.validate_configs(ledgers_cfg(), rules_cfg(), {})
        self.assertEqual([e for e in errs if "key_tiebreakers" in e], [])


if __name__ == "__main__":
    unittest.main()
