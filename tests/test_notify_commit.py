#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P0-A：推送失败不得记为已通知。

这是本次加固最重要的一组测试。它守的是这个失败链条：

    企微推送失败 → 系统仍写 last_notified → 项目进入 7 天静默期
    → 业务没收到、也不会再收到 → 而 Hermes 显示「成功」

链条上任何一环破了，业务就会漏催，且**没有任何迹象**。
"""

from __future__ import annotations

import unittest
from datetime import date

from harness import (make_sheet, row, days_ago, temp_home, run_main,
                     read_state, state_files, output_cfg)

TODAY = date(2026, 7, 20)


def overdue_sheet():
    """两个必然超期的项目：一个卡收资、一个卡专家评估。"""
    return make_sheet([
        row(1, "甲公司", tech="待收资", reported=days_ago(TODAY, 40),
            progress=days_ago(TODAY, 40)),
        row(2, "乙公司", tech="", reported=days_ago(TODAY, 30),
            progress=days_ago(TODAY, 30)),
    ])


class NotifyCommitTest(unittest.TestCase):

    def _run(self, **kw):
        return run_main([f"--today={TODAY}", "--force-push"], overdue_sheet(), **kw)

    # ── 全部成功 ───────────────────────────────────────────────────
    def test_all_chunks_ok_commits_and_exits_zero(self):
        with temp_home() as home:
            r = self._run(post_results=True)
            self.assertEqual(r.code, 0, r.err)
            fs = read_state(home, "followup_state.json")
            notified = [k for k, v in fs.items() if v.get("last_notified")]
            self.assertEqual(len(notified), 2,
                             f"两条都该记为已通知，实际 {fs}")
            for v in fs.values():
                self.assertEqual(v["last_notified"], TODAY.isoformat())
            self.assertFalse(r.alerted, "全部成功不该告警")

    # ── 全部失败 ───────────────────────────────────────────────────
    def test_all_chunks_fail_no_commit_nonzero_and_alerts(self):
        with temp_home() as home:
            r = self._run(post_results=False)
            self.assertEqual(r.code, 1, "推送全失败必须非零退出，否则 Hermes 显示成功")
            fs = read_state(home, "followup_state.json")
            notified = [k for k, v in fs.items() if v.get("last_notified")]
            self.assertEqual(notified, [],
                             "🔴 一条都不许记为已通知——业务根本没收到")
            self.assertTrue(r.alerted, "推送失败必须告警")

    # ── 分片部分成功：本次加固的核心场景 ─────────────────────────
    def test_partial_success_does_not_commit_whole_batch(self):
        """
        拆成多片、只成了一部分。

        整批不提交 → 下次会重发整批 → 业务可能看到一小段重复。
        这是刻意的取舍：重复消息她一眼能识别，静默漏催她永远发现不了。
        """
        many = make_sheet([
            row(i, f"公司{i}" + "长" * 60, tech="待收资",
                reported=days_ago(TODAY, 40), progress=days_ago(TODAY, 40))
            for i in range(1, 60)
        ])
        cfg = output_cfg(wecom_webhook={"split_bytes": 900})
        with temp_home(output=cfg) as home:
            r = run_main([f"--today={TODAY}", "--force-push"], many,
                         post_results=[True, False])
            self.assertGreater(len(r.posts), 1, "这个用例必须真的拆成多片")
            self.assertEqual(r.code, 1)
            fs = read_state(home, "followup_state.json")
            notified = [k for k, v in fs.items() if v.get("last_notified")]
            self.assertEqual(notified, [],
                             "🔴 部分成功不许把整批记成已通知")
            self.assertTrue(r.alerted)

    # ── 推送抛异常 ─────────────────────────────────────────────────
    def test_push_exception_no_commit_nonzero(self):
        with temp_home() as home:
            r = self._run(push_raises=RuntimeError("模拟拆分算法炸了"))
            self.assertEqual(r.code, 1)
            fs = read_state(home, "followup_state.json")
            self.assertEqual([k for k, v in fs.items() if v.get("last_notified")], [])
            self.assertTrue(r.alerted)

    # ── 事实观测仍要落盘 ───────────────────────────────────────────
    def test_failed_push_still_persists_observations(self):
        """
        投递凭证不提交，但「节点进入时间」这类事实观测照写。

        两者混在一起写正是旧实现的病根；混在一起**不写**同样是错的——
        那样每天都会用「最新进展日期」重新初始化，停滞天数永远长不上去。
        """
        with temp_home() as home:
            r = self._run(post_results=False)
            self.assertEqual(r.code, 1)
            se = read_state(home, "stage_entered.json")
            self.assertTrue(se, "stage_entered 必须照常落盘")
            self.assertIn("snapshot_last_box.json", state_files(home))

    # ── 三个诊断开关：不发不写 ─────────────────────────────────────
    def test_dry_run_sends_nothing_writes_nothing(self):
        with temp_home() as home:
            before = state_files(home)
            r = run_main(["--dry-run"], overdue_sheet())
            self.assertEqual(r.code, 0)
            self.assertEqual(r.posts, [], "--dry-run 绝不能真发")
            self.assertEqual(state_files(home), before, "--dry-run 绝不能写状态")

    def test_today_without_force_sends_nothing_writes_nothing(self):
        """--today 是回归测试的常用开关，绝不能污染真实状态。"""
        with temp_home() as home:
            before = state_files(home)
            r = run_main(["--today=2026-08-30"], overdue_sheet())
            self.assertEqual(r.code, 0)
            self.assertEqual(r.posts, [])
            self.assertEqual(state_files(home), before,
                             "--today 写状态会污染节点计时，还会留下未来日期的快照")

    def test_json_sends_nothing_writes_nothing_and_is_parseable(self):
        import json as _json
        with temp_home() as home:
            before = state_files(home)
            r = run_main(["--json", f"--today={TODAY}"], overdue_sheet())
            self.assertEqual(r.code, 0)
            self.assertEqual(r.posts, [])
            self.assertEqual(state_files(home), before)
            _json.loads(r.out)  # 混进任何诊断输出都会让这行炸

    def test_future_today_with_force_push_is_refused(self):
        """未来日期 + 强推会写出未来日期的快照，把计时基准彻底污染。"""
        future = (date.today().replace(year=date.today().year + 1)).isoformat()
        with temp_home() as home:
            before = state_files(home)
            r = run_main([f"--today={future}", "--force-push"], overdue_sheet())
            self.assertEqual(r.code, 2)
            self.assertEqual(r.posts, [])
            self.assertEqual(state_files(home), before)

    # ── 无待催时不该有任何投递 ─────────────────────────────────────
    def test_verbose_still_prints_when_nothing_is_due(self):
        """--verbose 是诊断开关，静默毫无用处——排查时最想看的正是「今天为什么没有」。"""
        fresh = make_sheet([
            row(1, "甲公司", tech="待收资", reported=TODAY, progress=TODAY),
        ])
        with temp_home():
            r = run_main([f"--today={TODAY}", "--dry-run", "--verbose"], fresh)
            self.assertEqual(r.code, 0)
            self.assertIn("运行摘要", r.out)
            self.assertEqual(r.posts, [], "打印摘要不等于要推送")

    def test_nothing_due_sends_nothing_but_still_leaves_a_receipt(self):
        """
        🔴 **刻意的行为变更（2026-08-01）**：以前这里断言「完全静默」。

        企微不发是对的（无事不发是降噪的一部分），但**本地运行结果不该跟着消失**：
        stdout 为空时 Hermes 把这次运行记成 `[SILENT]`，业务手动点一下，
        看到的就是「毫无反应」—— 而那和「今天压根没跑起来」长得一模一样。

        所以现在：企微仍然一条不发，本地必留一行回执。
        """
        fresh = make_sheet([
            row(1, "甲公司", tech="待收资", reported=TODAY, progress=TODAY),
        ])
        with temp_home() as home:
            r = run_main([f"--today={TODAY}", "--force-push"], fresh)
            self.assertEqual(r.code, 0)
            self.assertEqual(r.posts, [], "无事不发 —— 企微一条都不许发")
            self.assertIn("检查完成", r.out, "真实运行必须留一行本地回执")
            self.assertIn("待催 0 项", r.out)

            fs = read_state(home, "followup_state.json") or {}
            self.assertEqual([k for k, v in fs.items() if v.get("last_notified")],
                             [], "没发就不许记已通知")

            # Hermes --no-agent 模式下 stdout 可能被吞，摘要必须同时落进 health
            h = read_state(home, "health.json")
            self.assertIn("last_run_summary", h, "摘要要落盘，不能只在 stdout")
            self.assertEqual(h["last_run_summary"]["due"], 0)
            self.assertEqual(h["last_run_summary"]["read"], 1)

    def test_diagnostic_modes_stay_completely_silent(self):
        """对照组：回执只给真实运行。诊断模式无事发生时仍然一个字都不打。"""
        fresh = make_sheet([
            row(1, "甲公司", tech="待收资", reported=TODAY, progress=TODAY),
        ])
        # 必须带 --today：不带的话用的是真实今天，这条数据早就超期了，
        # 测的就不是「无事发生」而是「有事但不发」了。
        for argv in ([f"--today={TODAY}", "--dry-run"], [f"--today={TODAY}"]):
            with self.subTest(argv=argv):
                with temp_home():
                    r = run_main(argv, fresh)
                    self.assertEqual(r.out.strip(), "", f"{argv} 应完全静默")


class PrimaryChannelTest(unittest.TestCase):
    """主通道声明错配会让状态永远提交不了 —— 必须在启动阶段就失败。"""

    def test_primary_points_at_disabled_channel(self):
        cfg = output_cfg(notify={"primary": "wecom_webhook"},
                         wecom_webhook={"enabled": False})
        with temp_home(output=cfg):
            r = run_main([f"--today={TODAY}", "--force-push"], overdue_sheet())
            self.assertEqual(r.code, 2, "这是配置故障，不能默默跑成功")
            self.assertIn("primary", r.err)

    def test_unknown_primary_is_refused(self):
        cfg = output_cfg(notify={"primary": "telegram"})
        with temp_home():
            r = run_main([f"--today={TODAY}", "--force-push"], overdue_sheet())
            self.assertEqual(r.code, 0)  # 先确认基线是通的
        with temp_home(output=cfg):
            r = run_main([f"--today={TODAY}", "--force-push"], overdue_sheet())
            self.assertEqual(r.code, 2)


if __name__ == "__main__":
    unittest.main()
