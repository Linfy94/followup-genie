#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地运行回执 + health.json 自身损坏。

═══════════════════════════════════════════════════════════════════════
两个问题，同一个病根：**该出声的时候没出声。**

  · 零待催时 stdout 为空 → Hermes 记成 [SILENT] → 业务点一下「毫无反应」，
    而那和「今天压根没跑起来」长得一模一样
  · health.json 自己坏掉时，损坏检查跑在它第一次被读之前 → 永远漏报，
    还被静默重建，连「上次成功是什么时候」都一起丢掉

第二条尤其别扭：**健康记录骨折了，反而是最没人发现的那种坏法** ——
它恰恰是用来发现「根本没跑」的那个机制。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import unittest
from datetime import date

from harness import (make_sheet, row, days_ago, temp_home, run_main,
                     read_state, state_files, run_doctor)

import core  # noqa: E402 —— 必须在 harness 之后

TODAY = date(2026, 7, 20)


def fresh_sheet():
    """一条刚上报的项目：不会超期，用来造「今天没有要催的」。"""
    return make_sheet([row(1, "甲公司", tech="待收资",
                           reported=TODAY, progress=TODAY)])


def overdue_sheet():
    return make_sheet([
        row(1, "甲公司", tech="待收资", reported=days_ago(TODAY, 40),
            progress=days_ago(TODAY, 40)),
    ])


class ReceiptTest(unittest.TestCase):

    def test_zero_due_prints_a_receipt(self):
        with temp_home():
            r = run_main([f"--today={TODAY}", "--force-push"], fresh_sheet())
            self.assertEqual(r.code, 0, r.err)
            self.assertIn("检查完成", r.out)
            self.assertIn("读取 1 项", r.out)
            self.assertIn("待催 0 项", r.out)
            self.assertEqual(r.posts, [], "零待催仍然不发企微")

    def test_receipt_says_whether_it_was_delivered(self):
        """
        有清单时清单本身就是回执，但**清单不能证明它发出去了** ——
        末尾那行投递结果才能。
        """
        with temp_home():
            r = run_main([f"--today={TODAY}", "--force-push"], overdue_sheet())
            self.assertEqual(r.code, 0, r.err)
            self.assertIn("甲公司", r.out)
            self.assertIn("投递：", r.out, "清单末尾必须交代发没发出去")

    def test_failed_delivery_is_visible_in_the_receipt(self):
        with temp_home():
            r = run_main([f"--today={TODAY}", "--force-push"], overdue_sheet(),
                         post_results=False)
            self.assertEqual(r.code, 1)
            self.assertIn("投递：", r.out)
            self.assertNotIn("完整送达", r.out, "没送到就不许说送到了")

    def test_summary_lands_in_health(self):
        """Hermes --no-agent 下 stdout 可能没人看，摘要必须落盘。"""
        with temp_home() as home:
            run_main([f"--today={TODAY}", "--force-push"], overdue_sheet())
            s = (read_state(home, "health.json") or {}).get("last_run_summary")
            self.assertIsInstance(s, dict, "health 里必须有 last_run_summary")
            for k in ("at", "read", "due", "muted", "messages",
                      "delivery", "data_quality_warnings"):
                self.assertIn(k, s, f"摘要缺字段 {k}")
            self.assertEqual(s["read"], 1)
            self.assertEqual(s["due"], 1)

    def test_doctor_shows_the_summary(self):
        with temp_home():
            run_main([f"--today={TODAY}", "--force-push"], overdue_sheet())
            code, out = run_doctor(["--validate-config"])
            # --validate-config 不查 health，所以这里只确认不崩；
            # 展示逻辑由下面的 DoctorHealthTest 直接调函数验证
            self.assertIn(code, (0, 1))

    def test_diagnostic_modes_write_no_summary(self):
        for argv in ([f"--today={TODAY}", "--dry-run"], [f"--today={TODAY}"]):
            with self.subTest(argv=argv):
                with temp_home() as home:
                    run_main(argv, overdue_sheet())
                    self.assertNotIn("health.json", state_files(home),
                                     f"{argv} 不许创建健康记录")


class DoctorHealthTest(unittest.TestCase):
    """直接验 doctor 的展示逻辑，不依赖 --validate-config 的分支。"""

    def _render(self, health: dict) -> str:
        import doctor
        with temp_home(state={"health.json": health}):
            doc = doctor.Doc()
            doctor.check_health(doc)
            return doc.render()

    def test_summary_is_shown(self):
        out = self._render({
            "last_full_success": core.now_iso(),
            "last_run_summary": {"at": "2026-07-20T09:00:00+08:00", "read": 101,
                                 "due": 0, "muted": 23, "messages": 0,
                                 "delivery": "企微未发送",
                                 "data_quality_warnings": 2},
        })
        self.assertIn("最近一次运行摘要", out)
        self.assertIn("读取 101 项", out)
        self.assertIn("待催 0 项", out)
        self.assertIn("企微未发送", out)

    def test_recovery_event_is_shown(self):
        out = self._render({
            "last_full_success": core.now_iso(),
            "last_recovery": {"at": "2026-07-20T09:00:00+08:00",
                              "damaged": ["stage_entered.json"],
                              "files": ["stage_entered.json.corrupt.20260720-090000"]},
        })
        self.assertIn("曾从状态损坏中恢复过", out)
        self.assertIn("stage_entered.json", out)
        self.assertIn("没有删除", out)


class CorruptHealthTest(unittest.TestCase):
    """
    🔴 health.json 自己坏掉。

    旧实现里损坏检查跑在它第一次被读之前，于是这类损坏**永远漏报**，
    还会被静默重建成一份干净记录 —— 连「上次成功是什么时候」都一起丢掉，
    而那正是唯一能发现「根本没跑」的线索。
    """

    BROKEN = {"health.json": "{ 这不是 JSON "}

    def test_real_run_keeps_alerts_and_records_recovery(self):
        with temp_home(state=dict(self.BROKEN)) as home:
            r = run_main([f"--today={TODAY}", "--force-push"], overdue_sheet())
            names = state_files(home)

            kept = [n for n in names if n.startswith("health.json.corrupt.")]
            self.assertEqual(len(kept), 1, f"坏文件要改名保留，实际：{names}")
            self.assertTrue(r.alerted, "健康记录损坏必须告警，不能只打一行 stderr")

            h = read_state(home, "health.json") or {}
            rec = h.get("last_recovery")
            self.assertIsInstance(rec, dict, "新记录里要留恢复事件")
            self.assertIn("health.json", rec.get("damaged") or [])

    def test_it_does_not_pretend_the_run_was_clean(self):
        """
        损坏后照常跑完是对的（降级运行），但**不许把这次记成完整成功** ——
        那等于把「我刚丢了一段历史」这件事一笔勾销。
        """
        with temp_home(state=dict(self.BROKEN)) as home:
            run_main([f"--today={TODAY}", "--force-push"], overdue_sheet())
            h = read_state(home, "health.json") or {}
            self.assertNotIn("last_full_success", h,
                             "状态损坏的这一次不算完整成功")

    def test_damage_is_reported_in_the_output(self):
        with temp_home(state=dict(self.BROKEN)):
            r = run_main([f"--today={TODAY}", "--force-push"], overdue_sheet())
            self.assertIn("损坏", r.out + r.err)

    def test_diagnostic_modes_only_report(self):
        """诊断模式：报告可以，改名和重建都不行。"""
        for argv in ([f"--today={TODAY}", "--dry-run"], [f"--today={TODAY}"]):
            with self.subTest(argv=argv):
                with temp_home(state=dict(self.BROKEN)) as home:
                    before = state_files(home)
                    r = run_main(argv, overdue_sheet())
                    self.assertEqual(state_files(home), before,
                                     f"{argv} 动了现场")
                    self.assertEqual(r.alerts, [], f"{argv} 不该告警")
                    self.assertIn("损坏", r.out + r.err, f"{argv} 仍要报告")

    def test_healthy_run_writes_full_success(self):
        """对照组：没损坏时该记的还是要记，别把降级判断写得太宽。"""
        with temp_home() as home:
            run_main([f"--today={TODAY}", "--force-push"], overdue_sheet())
            h = read_state(home, "health.json") or {}
            self.assertIn("last_full_success", h)


if __name__ == "__main__":
    unittest.main()
