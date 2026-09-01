#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
台账被责任范围整表过滤光时，必须有人被告知 —— 而不只是本地日志里一行字。

0.4.0-rc4 只做到「报告里写一行」。那一刻待催数是 0：企微无事不发、
告警通道碰不到、看门狗只看到「任务成功了」。没人主动翻本地日志的话，
这条业务线已经全量失效而无人知晓 —— 又变回它本要消灭的那个形状。

🔴 同时要守住反面：这条警告**不能**让 last_full_success 停写，
   否则看门狗两天后会误报「任务根本没跑」。
"""

from __future__ import annotations

import unittest
from datetime import date
from unittest import mock

from harness import (make_sheet, row, days_ago, temp_home, run_main,  # noqa: I001
                     read_state)
import check_followup

TODAY = date(2026, 7, 20)

ALERT_MARK = "有业务线被责任范围整表过滤掉了"


def all_out_of_scope():
    """台账里换了写法，配置还是旧的 → 全表落空。"""
    return make_sheet([
        row(1, "甲公司", place="杭州分行", tech="待收资",
            reported=days_ago(TODAY, 40), progress=days_ago(TODAY, 40)),
    ])


def in_scope():
    return make_sheet([
        row(1, "甲公司", place="杭州", tech="待收资",
            reported=days_ago(TODAY, 40), progress=days_ago(TODAY, 40)),
    ])


class ScopeEmptiedAlertTest(unittest.TestCase):

    def test_alerts_once_when_a_line_goes_empty(self):
        with temp_home():
            r = run_main([f"--today={TODAY}", "--force-push"], all_out_of_scope())
            self.assertTrue(any(ALERT_MARK in " ".join(a) for a in r.alerts),
                            "整表落空必须有人被告知，不能只写本地日志")

    def test_does_not_repeat_while_still_empty(self):
        """一直空着就别天天念 —— 念多了和不念一样没人看。"""
        with temp_home():
            run_main([f"--today={TODAY}", "--force-push"], all_out_of_scope())
            second = run_main([f"--today={TODAY}", "--force-push"], all_out_of_scope())
            self.assertFalse(any(ALERT_MARK in " ".join(a) for a in second.alerts))

    def test_failed_alert_is_retried_and_not_deduplicated(self):
        """
        告警没发出去不能登记为已告警，否则故障会永久静默。

        🔴 这个 fixture 的「地点」写法（杭州分行）本身也不在 known_values
           白名单里，2026-09-01 起 _notify_data_quality 会为它另发一条数据
           质量提示——同一次 run_main 里 alert() 会被调用不止一次，
           这条测试只关心「scope_emptied 那条」的重试次数，按消息内容筛出来数，
           不能再数 alert() 总调用次数。
        """
        with temp_home() as home:
            with mock.patch.object(check_followup, "alert",
                                   return_value=(False, "模拟发送失败")) as send:
                run_main([f"--today={TODAY}", "--force-push"], all_out_of_scope())
                run_main([f"--today={TODAY}", "--force-push"], all_out_of_scope())
            scope_alerts = [c for c in send.call_args_list
                           if ALERT_MARK in c.args[0]]
            self.assertEqual(len(scope_alerts), 2,
                             "第一次没发出去，下一次真实运行必须重试")
            health = read_state(home, "health.json")
            self.assertNotIn("scope_emptied", health,
                             "发送失败不能写进已告警去重登记")

    def test_alerts_again_after_recovery(self):
        """恢复后再次落空要重新告警，否则第二次故障永远静默。"""
        with temp_home():
            run_main([f"--today={TODAY}", "--force-push"], all_out_of_scope())
            run_main([f"--today={TODAY}", "--force-push"], in_scope())
            again = run_main([f"--today={TODAY}", "--force-push"], all_out_of_scope())
            self.assertTrue(any(ALERT_MARK in " ".join(a) for a in again.alerts))

    def test_recovery_is_recorded(self):
        with temp_home() as home:
            run_main([f"--today={TODAY}", "--force-push"], all_out_of_scope())
            run_main([f"--today={TODAY}", "--force-push"], in_scope())
            h = read_state(home, "health.json")
            self.assertEqual(h.get("scope_emptied"), {})
            self.assertIn("box", h["last_scope_recovery"]["ledgers"])

    def test_normal_run_never_alerts(self):
        with temp_home():
            r = run_main([f"--today={TODAY}", "--force-push"], in_scope())
            self.assertFalse(any(ALERT_MARK in " ".join(a) for a in r.alerts))

    def test_full_success_is_still_recorded(self):
        """
        🔴 反面护栏。塞进 run_warnings 会让 last_full_success 停写，
           看门狗两天后误报「任务根本没跑」—— 修好一个静默换来一个假警报。
           节假日闸门当初踩的就是这个坑。
        """
        with temp_home() as home:
            r = run_main([f"--today={TODAY}", "--force-push"], all_out_of_scope())
            self.assertEqual(r.code, 0, "空业务线是合法状态，不许报故障")
            h = read_state(home, "health.json")
            self.assertTrue(h.get("last_full_success"),
                            "本次仍是一次完整成功，看门狗依赖这个字段")

    def test_diagnostic_run_neither_alerts_nor_writes(self):
        with temp_home() as home:
            r = run_main([f"--today={TODAY}"], all_out_of_scope())
            self.assertEqual(r.alerts, [])
            self.assertIsNone(read_state(home, "health.json"))


if __name__ == "__main__":
    unittest.main()
