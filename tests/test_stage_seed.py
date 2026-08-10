#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
计时起点的人工播种。

有的台账**一个可靠的日期列都没有**：GEO 那张的「需求提出时间」写成 `6.12`
（没有年份）且一大半是空，「结束优化时间」混着「优化中」「7.9有成效」「/」，
「启动优化时间」名字像日期、其实是状态列。没有起点就算不出「卡了多久」，
而催办的全部内容就是「卡了多久」。

播种表是人工读一遍之后把起点定下来的那份结论，落在配置仓里（有 git 留痕、
每条写明依据、重装状态目录也不丢）。

这一组钉三件事：
  1. 🔴 播种**永远排在真实快照之后**。反过来的话，程序观测到的节点变更
     会被一条写死的旧日期永久盖住，超期天数越算越大而没人看得出不对
  2. 播种没覆盖到的条目照常走 clock.fallback，不受影响
  3. 🔴 播种表写坏了必须在**离线校验**就报错，不许静默丢掉 ——
     丢掉的后果是那一条「无可用起点」被跳过，又是一次「配置里配了、实际没生效」
"""

from __future__ import annotations

import unittest
from datetime import date

from harness import core

NODE = {"name": "③交付", "clock": {"stage_entered": True, "fallback": "需求提报日期"}}


def _resolve(stage_entered, seed, key="甲公司", fallback=None,
             last_snap_date=None, last_snap_node=None):
    return core.resolve_clock(
        NODE,
        get_text=lambda f: "",
        get_date=lambda f: fallback,
        stage_entered=stage_entered,
        state_key=f"{key}|deliver",
        last_snapshot_date=last_snap_date,
        last_snapshot_node=last_snap_node,
        node_id="deliver",
        seed=seed,
        seed_key=key,
    )


SEED = {("甲公司", "deliver"): (date(2026, 6, 12), "由启动优化时间读出")}


class PrecedenceTest(unittest.TestCase):

    def test_real_snapshot_beats_the_seed(self):
        """
        🔴 这条是本组的核心。快照是**程序自己观测到**的节点进入时间；
           播种是人一年前写下的一个数。观测到的必须赢，否则项目推进了
           还按旧起点算，超期天数只会越来越大 —— 而那看起来很像「真的很久没动」。
        """
        start, src = _resolve({"甲公司|deliver": "2026-07-20"}, SEED)
        self.assertEqual(start, date(2026, 7, 20))
        self.assertIn("快照", src)

    def test_seed_used_when_no_snapshot(self):
        start, src = _resolve({}, SEED)
        self.assertEqual(start, date(2026, 6, 12))
        self.assertIn("播种", src)

    def test_seed_reason_is_carried_into_the_source_text(self):
        """起点来源要能追到依据 —— 业务复核时问的就是「这个日期哪来的」。"""
        _, src = _resolve({}, SEED)
        self.assertIn("由启动优化时间读出", src)

    def test_seed_beats_fallback(self):
        start, _ = _resolve({}, SEED, fallback=date(2026, 1, 1))
        self.assertEqual(start, date(2026, 6, 12))

    def test_falls_back_when_seed_misses_this_row(self):
        """播种只覆盖存量那几条，其余照常走 fallback。"""
        start, src = _resolve({}, SEED, key="乙公司", fallback=date(2026, 1, 1))
        self.assertEqual(start, date(2026, 1, 1))
        self.assertNotIn("播种", src)

    def test_no_seed_at_all_behaves_exactly_as_before(self):
        """🔴 没配播种的台账（现有六份全都是）行为必须一字不变。"""
        for seed in (None, {}):
            with self.subTest(seed=seed):
                start, src = _resolve({}, seed, fallback=date(2026, 2, 3))
                self.assertEqual(start, date(2026, 2, 3))
                self.assertIn("需求提报日期", src)

    def test_seed_does_not_override_node_change_within_snapshot_window(self):
        """
        节点在两次运行之间推进了：已有「上次快照日」这条更准的线索时，
        它比播种更接近事实（播种是存量初始化用的）。
        """
        start, src = _resolve({}, {}, last_snap_date=date(2026, 8, 1),
                              last_snap_node="other", fallback=None)
        self.assertEqual(start, date(2026, 8, 1))


class ValidationTest(unittest.TestCase):
    """🔴 写坏了必须离线报错，不许静默丢掉。"""

    def _errs(self, seeds):
        return [e for e in core.stage_seed_errors({"id": "x", "manual_stage_entered": seeds},
                                                  "台账「测试」")]

    def test_good_entry_passes(self):
        self.assertEqual(self._errs([
            {"key": "甲公司", "node": "deliver", "entered": "2026-06-12",
             "confirmed": "2026-08-10 由某列读出"}]), [])

    def test_absent_key_is_fine(self):
        self.assertEqual(core.stage_seed_errors({"id": "x"}, "台账「测试」"), [])

    def test_bad_date_is_rejected(self):
        for bad in ("6.12", "2026/6/12", "昨天", "2026-13-01"):
            with self.subTest(bad=bad):
                errs = self._errs([{"key": "甲", "node": "n", "entered": bad,
                                    "confirmed": "c"}])
                self.assertTrue(any("不是 YYYY-MM-DD" in e for e in errs), errs)

    def test_missing_fields_are_rejected(self):
        for missing in ("key", "node", "entered"):
            with self.subTest(missing=missing):
                entry = {"key": "甲", "node": "n", "entered": "2026-06-12",
                         "confirmed": "c"}
                entry.pop(missing)
                self.assertTrue(any(missing in e for e in self._errs([entry])))

    def test_missing_confirmed_is_rejected(self):
        """
        依据必须写下来。一年后没人记得这个日期是怎么来的，
        而它正在决定某个项目每天催不催。
        """
        errs = self._errs([{"key": "甲", "node": "n", "entered": "2026-06-12"}])
        self.assertTrue(any("confirmed" in e for e in errs), errs)

    def test_wrong_shape_is_rejected(self):
        for bad in ("一条", 7, {"key": "甲"}):
            with self.subTest(bad=bad):
                self.assertTrue(core.stage_seed_errors(
                    {"id": "x", "manual_stage_entered": bad}, "台账「测试」"))

    def test_offline_validator_catches_it(self):
        """🔴 doctor --validate-config 也要拦得住，不能只有运行时那道。"""
        ledgers = {"ledgers": [{
            "id": "geo_wecom", "source": "wecom_doc", "url": "U", "sheet_id": "S",
            "ruleset": "geo", "enabled": True,
            "manual_stage_entered": [{"key": "甲", "node": "n", "entered": "6.12"}],
        }]}
        errs = core.validate_configs(ledgers, {"rulesets": {}, "workday": {}}, {})
        self.assertTrue(any("manual_stage_entered" in e for e in errs), errs)


class WecomSourceValidationTest(unittest.TestCase):

    def _errs(self, ledger):
        ledger.setdefault("id", "x")
        ledger.setdefault("ruleset", "r")
        return core.validate_configs({"ledgers": [ledger]},
                                     {"rulesets": {}, "workday": {}}, {})

    def test_wecom_doc_requires_url_and_sheet_id(self):
        for missing in ("url", "sheet_id"):
            with self.subTest(missing=missing):
                l = {"source": "wecom_doc", "url": "U", "sheet_id": "S"}
                l.pop(missing)
                self.assertTrue(any(missing in e for e in self._errs(l)))

    def test_complete_wecom_ledger_passes_source_check(self):
        errs = self._errs({"source": "wecom_doc", "url": "U", "sheet_id": "S"})
        self.assertEqual([e for e in errs if "wecom_doc" in e], [])

    def test_wecom_doc_is_listed_as_supported(self):
        """source 写错时的提示要把三种都列出来，别让人以为企微不支持。"""
        errs = self._errs({"source": "打错了"})
        self.assertTrue(any("wecom_doc" in e for e in errs), errs)


if __name__ == "__main__":
    unittest.main()
