#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P1-B：节点回退必须开新计时周期。

旧实现把「台账|序号|节点」的进入时间只 setdefault、从不清理。
项目 A→B 时 key|A 的旧条目留在文件里；日后返工退回 A，直接命中它，
凭空算出几百天停滞 —— 而且不报错，只表现为「这条怎么突然停滞 200 天」。

口径（业务 2026-07-31 确认）：回退后用「最新进展日期」重新计时，
**但不早于上次快照日** —— 上次快照那天它确知还在别的节点。
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from harness import (make_sheet, row, temp_home, run_main, read_state,
                     rules_cfg)

# 基准日必须让所有 at(n) 都落在真实「今天」之前 ——
# --today 是未来日期时会被守卫拒绝（那会写出未来日期的快照污染计时）。
D = date(2026, 3, 1)


def at(n: int) -> date:
    return D + timedelta(days=n)


def sheet_install(progress: date):
    """卡在③预调试/安装：技术确认=可行、安装调试为空。"""
    return make_sheet([row(1, "甲公司", tech="可行", install="",
                           reported=D, progress=progress)])


def sheet_test(progress: date):
    """推进到④节能测试：安装调试=完成、节能测试未完成。"""
    return make_sheet([row(1, "甲公司", tech="可行", install="完成",
                           test="", reported=D, progress=progress)])


class StageClockTest(unittest.TestCase):

    @staticmethod
    def _fallback_rules():
        rules = rules_cfg()
        install = next(
            node for node in rules["rulesets"]["box"]["nodes"]
            if node["id"] == "install"
        )
        install["clock"]["fallback"] = ["最新进展日期", "需求上报日期"]
        rules["rulesets"]["box"]["nodes"] = [install]
        return rules

    def _run(self, home_kwargs, sheet, today):
        return run_main([f"--today={today}", "--force-push"], sheet)

    def test_first_entry_uses_progress_date(self):
        """
        首次运行没有任何快照。此时若把起点写成「今天」，停滞 171 天的项目
        会被算成 0 天，首日一条都不催 —— 而且不报错。必须回退到最新进展日期。
        """
        with temp_home() as home:
            r = run_main([f"--today={at(40)}", "--force-push"],
                         sheet_install(progress=at(0)))
            self.assertEqual(r.code, 0, r.err)
            se = read_state(home, "stage_entered.json")
            self.assertEqual(se["box|1|install"], at(0).isoformat(),
                             "起点必须是最新进展日期，不是今天")
            # ③预调试/安装 允许 21 天 → 在节点 40 天 = 超期 19 天。
            # 这一条同时守住了口径：起点错了这个数字立刻不对。
            self.assertIn("超期 19 天", r.out)

    def test_full_cycle_A_to_B_to_A_to_B(self):
        """A首次进入 → 推进到B → 退回A → 再推进到B，四步全走一遍。"""
        with temp_home() as home:
            # ① A 首次进入（③预调试/安装），最新进展日期 = day0
            run_main([f"--today={at(30)}", "--force-push"],
                     sheet_install(progress=at(0)))
            se = read_state(home, "stage_entered.json")
            self.assertEqual(se["box|1|install"], at(0).isoformat())

            # ② 推进到 B（④节能测试）
            run_main([f"--today={at(31)}", "--force-push"],
                     sheet_test(progress=at(31)))
            se = read_state(home, "stage_entered.json")
            self.assertNotIn("box|1|install", se,
                             "旧节点的周期必须关闭，不能留在文件里等着被复用")
            self.assertEqual(se["box|1|efficiency_test"], at(31).isoformat())
            hist = read_state(home, "stage_history.json")
            self.assertEqual(len(hist["box|1|install"]), 1)
            self.assertEqual(hist["box|1|install"][0]["entered"], at(0).isoformat())

            # ③ 🔴 返工退回 A。最新进展日期没更新（还是 day31）
            run_main([f"--today={at(60)}", "--force-push"],
                     sheet_install(progress=at(31)))
            se = read_state(home, "stage_entered.json")
            self.assertEqual(
                se["box|1|install"], at(31).isoformat(),
                "回退必须开新周期。沿用 day0 的话会算成停滞 60 天")

            # ④ 再推进到 B
            run_main([f"--today={at(61)}", "--force-push"],
                     sheet_test(progress=at(61)))
            hist = read_state(home, "stage_history.json")
            self.assertEqual(len(hist["box|1|install"]), 2,
                             "两轮 install 都要留在历史里，阶段耗时分析才有数据")

    def test_rollback_floor_is_last_snapshot_date(self):
        """
        回退时业务没更新「最新进展日期」，它还停在很久以前。
        直接用它会算出巨大的假停滞数 —— 必须以上次快照日为下限。
        """
        with temp_home() as home:
            run_main([f"--today={at(30)}", "--force-push"],
                     sheet_install(progress=at(0)))
            run_main([f"--today={at(31)}", "--force-push"],
                     sheet_test(progress=at(0)))   # 日期列一直没动
            # 上次快照日 = at(31)。回退时最新进展日期仍是 at(0)
            run_main([f"--today={at(35)}", "--force-push"],
                     sheet_install(progress=at(0)))
            se = read_state(home, "stage_entered.json")
            self.assertEqual(se["box|1|install"], at(31).isoformat(),
                             "起点应取上次快照日，而不是 at(0)")

    def test_rollback_clears_stale_notification_record(self):
        """
        回退时若不清掉旧的 last_notified，新周期一进来就被上一轮的
        复提醒间隔静默掉 —— 返工之后反而不催了。
        """
        with temp_home() as home:
            run_main([f"--today={at(30)}", "--force-push"],
                     sheet_install(progress=at(0)))
            fs = read_state(home, "followup_state.json")
            self.assertTrue(fs["box|1|install"]["last_notified"])

            run_main([f"--today={at(31)}", "--force-push"],
                     sheet_test(progress=at(31)))
            # 回退，且新周期立刻又超期
            run_main([f"--today={at(60)}", "--force-push"],
                     sheet_install(progress=at(31)))
            fs = read_state(home, "followup_state.json")
            self.assertEqual(fs["box|1|install"]["first_overdue"],
                             at(60).isoformat(),
                             "新周期的首次超期日应是新的，不是上一轮的")

    def test_progress_date_edits_do_not_move_an_established_clock(self):
        """
        「最新进展日期」会被打电话、客户回消息之类的无关更新重置。
        一旦 stage_entered 建立，就只认节点变化，不再看那一列。
        """
        with temp_home() as home:
            run_main([f"--today={at(30)}", "--force-push"],
                     sheet_install(progress=at(0)))
            run_main([f"--today={at(31)}", "--force-push"],
                     sheet_install(progress=at(31)))  # 日期列被顺手改了
            se = read_state(home, "stage_entered.json")
            self.assertEqual(se["box|1|install"], at(0).isoformat(),
                             "节点没变，计时起点就不该动")

    def test_secondary_fallback_counts_without_skip_warning(self):
        """主字段空时使用次级日期，不得误报「会被跳过」。"""
        rules = self._fallback_rules()
        sheet = sheet_install(progress=None)
        with temp_home(rules=rules):
            r = run_main([f"--today={at(40)}", "--dry-run"], sheet)
        self.assertEqual(r.code, 0, r.err)
        self.assertIn("超期 19 天", r.out)
        self.assertNotIn("会被跳过", r.out)
        self.assertNotIn("无可用计时起点", r.out)

    def test_all_fallbacks_empty_warns_no_clock(self):
        """所有候选都无效时，才提示「无可用计时起点」。"""
        rules = self._fallback_rules()
        sheet = make_sheet([row(1, "甲公司", tech="可行", install="",
                                reported=None, progress=None)])
        with temp_home(rules=rules):
            r = run_main([f"--today={at(40)}", "--dry-run"], sheet)
        self.assertEqual(r.code, 0, r.err)
        self.assertIn("无可用计时起点", r.out)
        self.assertNotIn("会被跳过", r.out)


class CorruptStateTest(unittest.TestCase):
    """状态文件损坏是重大降级，必须响一声，而且坏文件要留着。"""

    def test_corrupt_stage_entered_is_backed_up_and_alerted(self):
        with temp_home(state={"stage_entered.json": "{ 这不是合法 JSON "}) as home:
            r = run_main([f"--today={at(40)}", "--force-push"],
                         sheet_install(progress=at(0)))
            self.assertEqual(r.code, 0, "损坏不该让催办停摆")
            self.assertIn("状态文件 stage_entered.json 损坏", r.out,
                          "必须出现在清单里，不能只在 stderr 飘一行")
            self.assertTrue(r.alerted, "全部项目按最新进展日期重算，是重大降级")
            kept = [n for n in
                    (home / "followup" / "state").iterdir()
                    if "corrupt" in n.name]
            self.assertEqual(len(kept), 1, "坏文件必须原样保留，排查时它是唯一线索")

    def test_missing_state_is_normal_not_an_alert(self):
        """首次运行本来就没有状态文件，不该告警。"""
        with temp_home():
            r = run_main([f"--today={at(40)}", "--force-push"],
                         sheet_install(progress=at(0)))
            self.assertEqual(r.code, 0)
            self.assertFalse(r.alerted)

    def test_corrupt_followup_state_degrades_to_recatch(self):
        """催办记录损坏 → 退化成「重新催一遍」，绝不能退化成「不催」。"""
        with temp_home(state={"followup_state.json": "]["}) as home:
            r = run_main([f"--today={at(40)}", "--force-push"],
                         sheet_install(progress=at(0)))
            self.assertEqual(r.code, 0)
            self.assertIn("甲公司", r.out, "宁可重复催，不可漏催")


if __name__ == "__main__":
    unittest.main()
