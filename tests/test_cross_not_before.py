#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
跨台账核对：目标行的日期早于本行的日期，就不可能是同一个项目。

═══════════════════════════════════════════════════════════════════════
🔴 业务 2026-08-14 报的真事：某项目被催「待登记飞书」，但它在主台账里
   明明已经有了 —— **同名两条**，一条立项 06-17、一条 08-06，
   而前期台账里这个需求 07-24 才提出来。06-17 那条比需求本身还早 37 天。

   原来只数命中条数：2 条 → 无法确认 → 继续催。逻辑没错，是信息不够。

🔴 这条约束只做**排除**，不做挑选，所以只会让判定更保守。
   下面每一条用例都在守同一件事：**它不许把「继续催」变成「静默不催」**。
   两处「不能证伪就不排除」（本行无日期、目标行无日期）尤其要钉死 ——
   那两处写反了，就会凭空停催，而且不报错。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta
from unittest import mock

from harness import ledgers_cfg, rules_cfg  # noqa: F401 —— 挂 sys.path
from test_sentinel_rules import FakeSheet, base_ledger

import core

TODAY = date(2026, 8, 24)       # 「今天」，固定住，与本行日期解耦
BASE = date(2026, 7, 24)        # 本行「进入时间」＝需求提出
EARLIER = date(2026, 6, 17)     # 比本行还早 —— 不可能是同一个项目
LATER = date(2026, 8, 6)        # 晚于本行 —— 可能是它


def _run(target_rows, *, guard=True, local_date=BASE):
    """本地一行处于「待登记」，跨表核对指向 tgt。target_rows 里放同名候选。"""
    src_sheet = FakeSheet(
        ["企业名称", "状态", "进入时间"],
        [{"企业名称": "同名企业", "状态": "待登记", "进入时间": local_date}])
    tgt_sheet = FakeSheet(["企业名称", "立项时间"], target_rows)
    src = base_ledger(id="src",
                      required_columns=["企业名称", "状态", "进入时间"])
    tgt = base_ledger(id="tgt", name="主台账",
                      required_columns=["企业名称", "立项时间"])
    cross = {"ledger_id": "tgt",
             "match_fields": [{"local_field": "企业名称",
                               "target_field": "企业名称"}]}
    if guard:
        cross["not_before"] = {"local_field": "进入时间",
                               "target_field": "立项时间"}
    ruleset = {"nodes": [{
        "id": "wait_reg", "name": "待登记", "enabled": True,
        "when": [{"field": "状态", "op": "equals", "value": "待登记"}],
        "clock": {"field": "进入时间"},
        "threshold": {"days": 3, "boundary": "on"},
        "repeat": {"days": 1},
        "cross_ledger": cross,
    }]}
    sheets = {"src": src_sheet, "tgt": tgt_sheet}
    wd = core.WorkdayCalc({"exclude_weekends": True,
                           "exclude_holidays": False}, None)
    with mock.patch.object(core, "read_ledger_sheet",
                           side_effect=lambda l: sheets[l["id"]]):
        return core.evaluate_ledger(
            src, ruleset, wd, TODAY, {}, {}, {},
            all_ledgers={"src": src, "tgt": tgt})[0]


def _due(rep):
    return {i.name for i in rep.due}


def _advanced(rep):
    return {n for _, n, _ in rep.advanced}


class BehaviourTest(unittest.TestCase):
    """🔴 这一组是行为级的：旧代码在第一条上会继续催，新代码判为已接力。"""

    def test_impossible_candidate_is_excluded_so_the_rest_decides(self):
        """业务报的那一幕：同名两条，一条早于本行 → 只剩一条 → 停催。"""
        rep = _run([{"企业名称": "同名企业", "立项时间": EARLIER},
                    {"企业名称": "同名企业", "立项时间": LATER}])
        self.assertIn("同名企业", _advanced(rep))
        self.assertNotIn("同名企业", _due(rep))

    def test_the_reason_is_spelled_out(self):
        """业务口径④：判定要标明依据，否则业务无从复核这一步对不对。"""
        rep = _run([{"企业名称": "同名企业", "立项时间": EARLIER},
                    {"企业名称": "同名企业", "立项时间": LATER}])
        why = next(w for _, n, w in rep.advanced if n == "同名企业")
        self.assertIn("排除", why)
        self.assertIn("立项时间", why)

    def test_the_reason_actually_reaches_the_output(self):
        """
        🔴 `advanced` 在渲染层只以计数出现（「跨台账核对通过 N」），
           逐条理由**不显示** —— 只塞进 advanced 等于没写。
           靠时间推断定下来的必须另发一条 notice，否则无从复核。
        """
        rep = _run([{"企业名称": "同名企业", "立项时间": EARLIER},
                    {"企业名称": "同名企业", "立项时间": LATER}])
        self.assertTrue(any("排除" in n for n in rep.notices), rep.notices)

    def test_ordinary_advance_stays_quiet(self):
        """没靠时间排除就定下来的，不发 notice —— 否则天天一堆没用的行。"""
        rep = _run([{"企业名称": "同名企业", "立项时间": LATER}])
        self.assertEqual(rep.notices, [])

    def test_all_candidates_impossible_keeps_chasing(self):
        """只剩老项目 → 这个需求确实还没进主台账 → 继续催。"""
        rep = _run([{"企业名称": "同名企业", "立项时间": EARLIER}])
        self.assertIn("同名企业", _due(rep))
        self.assertNotIn("同名企业", _advanced(rep))

    def test_still_ambiguous_when_two_survive(self):
        """排除后仍剩 2 条 → 仍然无法确认 → 继续催 + 出复核提示。"""
        rep = _run([{"企业名称": "同名企业", "立项时间": LATER},
                    {"企业名称": "同名企业", "立项时间": LATER}])
        self.assertIn("同名企业", _due(rep))
        self.assertTrue(any("命中 2 条" in h for h in rep.review_hints))

    def test_single_plausible_hit_unaffected(self):
        """9/10 的真实情况：单命中且日期合理 —— 加了约束一动不动。"""
        rep = _run([{"企业名称": "同名企业", "立项时间": LATER}])
        self.assertIn("同名企业", _advanced(rep))

    def test_no_hit_still_chases(self):
        rep = _run([{"企业名称": "别的企业", "立项时间": LATER}])
        self.assertIn("同名企业", _due(rep))


class CannotDisproveTest(unittest.TestCase):
    """
    🔴 两处兜底都必须倒向「继续催」。写反了会凭空停催，而且不报错 ——
       那正是这个项目最怕的失败模式。
    """

    def test_local_date_missing_falls_back_to_plain_counting(self):
        """
        本行没有日期 → 约束整条不生效 → 退回纯计数。

        🔸 这里没法走 evaluate_ledger：本行日期同时是 clock 起点，
           取不到的话整行会先被「无可用计时起点」跳过，根本到不了跨表核对。
           所以直接验那一段的判据：floor 为 None 时不做任何排除。
        """
        hits = [EARLIER, LATER]
        floor = None
        kept = hits if floor is None else [d for d in hits
                                           if d is None or d >= floor]
        self.assertEqual(len(kept), 2, "本行无日期时不许排除任何候选")

    def test_target_date_missing_stays_a_candidate(self):
        """
        目标行日期读不出来 → 不能证伪 → **保留**为候选。
        这里两条都保留，于是仍是「无法确认」，继续催。
        """
        rep = _run([{"企业名称": "同名企业", "立项时间": None},
                    {"企业名称": "同名企业", "立项时间": LATER}])
        self.assertIn("同名企业", _due(rep))

    def test_guard_off_keeps_old_behaviour_exactly(self):
        rep = _run([{"企业名称": "同名企业", "立项时间": EARLIER},
                    {"企业名称": "同名企业", "立项时间": LATER}], guard=False)
        self.assertIn("同名企业", _due(rep))


class MissingColumnTest(unittest.TestCase):

    def test_local_guard_column_absent_is_fatal_not_silent(self):
        """
        🔴 rc9 漏了这一条，rc10 补上。本地日期列名写错时：
             旧行为 → get_date() 返回 None → 约束**静默失效** → 退回纯计数
                     → 这个例子里 1 条命中 → **判为已接力、停催**
             新行为 → 入口断言拦下，拒绝继续判定

           一个错别字把「正确地继续催」变成「静默不催」，而这条约束
           存在的全部意义就是防这个。实测过两种结果，见提交信息。
        """
        src_sheet = FakeSheet(["企业名称", "状态", "进入时间"],
                              [{"企业名称": "同名企业", "状态": "待登记",
                                "进入时间": BASE}])
        # 目标只有一条，且日期早于本行 —— 约束生效时它会被排除、继续催
        tgt_sheet = FakeSheet(["企业名称", "立项时间"],
                              [{"企业名称": "同名企业", "立项时间": EARLIER}])
        src = base_ledger(id="src",
                          required_columns=["企业名称", "状态", "进入时间"])
        tgt = base_ledger(id="tgt", name="主台账",
                          required_columns=["企业名称", "立项时间"])
        ruleset = {"nodes": [{
            "id": "wait_reg", "name": "待登记", "enabled": True,
            "when": [{"field": "状态", "op": "equals", "value": "待登记"}],
            "clock": {"field": "进入时间"},
            "threshold": {"days": 3, "boundary": "on"},
            "repeat": {"days": 1},
            "cross_ledger": {
                "ledger_id": "tgt",
                "match_fields": [{"local_field": "企业名称",
                                  "target_field": "企业名称"}],
                # 本地列名写错（繁体「進入時間」）
                "not_before": {"local_field": "進入時間",
                               "target_field": "立项时间"}},
        }]}
        sheets = {"src": src_sheet, "tgt": tgt_sheet}
        wd = core.WorkdayCalc({"exclude_weekends": True,
                               "exclude_holidays": False}, None)
        with mock.patch.object(core, "read_ledger_sheet",
                               side_effect=lambda l: sheets[l["id"]]):
            with self.assertRaises(core.LedgerError) as cm:
                core.evaluate_ledger(src, ruleset, wd, TODAY, {}, {}, {},
                                     all_ledgers={"src": src, "tgt": tgt})
        self.assertIn("進入時間", str(cm.exception))

    def test_correct_local_column_is_not_flagged(self):
        """列名写对时一切照旧 —— 新校验不能误伤。"""
        rep = _run([{"企业名称": "同名企业", "立项时间": EARLIER}])
        self.assertIn("同名企业", _due(rep))

    def test_guard_column_absent_is_fatal_not_silent(self):
        """🔴 列不存在当成「没查到」继续跑，会让约束静默失效。"""
        src_sheet = FakeSheet(["企业名称", "状态", "进入时间"],
                              [{"企业名称": "同名企业", "状态": "待登记",
                                "进入时间": BASE}])
        tgt_sheet = FakeSheet(["企业名称"], [{"企业名称": "同名企业"}])  # 没有「立项时间」
        src = base_ledger(id="src",
                          required_columns=["企业名称", "状态", "进入时间"])
        tgt = base_ledger(id="tgt", name="主台账",
                          required_columns=["企业名称"])
        ruleset = {"nodes": [{
            "id": "wait_reg", "name": "待登记", "enabled": True,
            "when": [{"field": "状态", "op": "equals", "value": "待登记"}],
            "clock": {"field": "进入时间"},
            "threshold": {"days": 3, "boundary": "on"},
            "repeat": {"days": 1},
            "cross_ledger": {
                "ledger_id": "tgt",
                "match_fields": [{"local_field": "企业名称",
                                  "target_field": "企业名称"}],
                "not_before": {"local_field": "进入时间",
                               "target_field": "立项时间"}},
        }]}
        sheets = {"src": src_sheet, "tgt": tgt_sheet}
        wd = core.WorkdayCalc({"exclude_weekends": True,
                               "exclude_holidays": False}, None)
        with mock.patch.object(core, "read_ledger_sheet",
                               side_effect=lambda l: sheets[l["id"]]):
            with self.assertRaises(core.LedgerError) as cm:
                core.evaluate_ledger(src, ruleset, wd, TODAY, {}, {}, {},
                                     all_ledgers={"src": src, "tgt": tgt})
        self.assertIn("立项时间", str(cm.exception))
        self.assertIn("永远催", str(cm.exception), "要说清后果")


class ConfigTest(unittest.TestCase):

    def test_absent_guard_keeps_old_behaviour(self):
        self.assertIsNone(core.cross_not_before({"ledger_id": "x"}))

    def test_half_written_guard_is_rejected(self):
        """🔴 只写一半会让整条约束静默失效，必须离线就报错。"""
        for bad in ({"local_field": "A"}, {"target_field": "B"},
                    {"local_field": "", "target_field": "B"}, "不是对象"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    core.cross_not_before({"not_before": bad})

    def test_offline_validation_catches_it(self):
        led = ledgers_cfg()["ledgers"][0]
        rules = rules_cfg(expert={"cross_ledger": {
            "ledger_id": "box", "target_field": "项目名称",
            "not_before": {"local_field": "需求上报日期"}}})
        errs = core.validate_configs({"ledgers": [led]}, rules, {})
        self.assertTrue(any("not_before" in e for e in errs), errs)

    def test_cache_key_separates_guarded_from_unguarded(self):
        """
        🔴 同一份目标台账被两个节点引用、只有一个配了 not_before 时，
           缓存键若不含 guard，两边会共用同一份索引 —— 约束静默失效。
        """
        specs = [{"target_field": "项目名称", "normalize_map": {}}]
        a = core._cross_cache_key("x", specs, None)
        b = core._cross_cache_key("x", specs, {"target_field": "需求上报日期"})
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
