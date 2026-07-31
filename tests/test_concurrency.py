#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
并发保护。

两次运行同时跑会互相覆盖状态、重复推送。但锁本身也有个陷阱：
**一次卡死如果让之后每天都静默跳过，那和「每天没有超时单」长得一模一样。**
所以陈旧锁必须能夺回，而且夺回要告警。
"""

from __future__ import annotations

import json
import os
import unittest
from datetime import date, timedelta

from harness import (core, make_sheet, row, temp_home, run_main, read_state)

TODAY = date(2026, 7, 20)


def sheet():
    return make_sheet([row(1, "甲公司", tech="待收资",
                           reported=date(2026, 6, 1), progress=date(2026, 6, 1))])


def write_lock(home, *, pid, minutes_ago):
    p = home / "followup" / "state" / core.LOCK_FILE
    started = (core.now_iso() if minutes_ago == 0 else
               (core.parse_dt(core.now_iso()) -
                timedelta(minutes=minutes_ago)).isoformat(timespec="seconds"))
    p.write_text(json.dumps({"pid": pid, "started_at": started}),
                 encoding="utf-8")
    return p


class LockTest(unittest.TestCase):

    def test_lock_is_created_and_released(self):
        with temp_home() as home:
            r = run_main([f"--today={TODAY}", "--force-push"], sheet())
            self.assertEqual(r.code, 0, r.err)
            self.assertFalse((home / "followup" / "state" / core.LOCK_FILE).exists(),
                             "跑完必须释放锁")

    def test_live_lock_makes_second_run_skip_quietly(self):
        """
        另一次运行正在进行 → 安静跳过，退出码 0。
        这不是故障，不该让 Hermes 报错。
        """
        with temp_home() as home:
            write_lock(home, pid=os.getpid(), minutes_ago=0)  # 本进程必然存活
            r = run_main([f"--today={TODAY}", "--force-push"], sheet())
            self.assertEqual(r.code, 0)
            self.assertIn("本次跳过", r.err)
            self.assertEqual(r.posts, [], "跳过就不该推送")
            self.assertFalse(r.alerted, "并发跳过是正常现象，不该告警")

    def test_dead_process_lock_is_reclaimed_and_alerted(self):
        """持锁进程已经不在了 —— 上次可能被强杀。夺回并告警。"""
        with temp_home() as home:
            write_lock(home, pid=999_999, minutes_ago=1)
            r = run_main([f"--today={TODAY}", "--force-push"], sheet())
            self.assertEqual(r.code, 0)
            self.assertIn("夺回陈旧运行锁", r.out)
            self.assertTrue(r.alerted)
            self.assertEqual(len(r.posts), 1, "夺回后要照常干活")

    def test_stale_but_alive_lock_is_reclaimed(self):
        """
        🔴 这条最重要：进程还活着但已经卡了 31 分钟。
           不夺回的话，之后每天都静默跳过，而那看起来完全正常。
        """
        with temp_home() as home:
            write_lock(home, pid=os.getpid(), minutes_ago=31)
            r = run_main([f"--today={TODAY}", "--force-push"], sheet())
            self.assertEqual(r.code, 0)
            self.assertIn("夺回陈旧运行锁", r.out)
            self.assertTrue(r.alerted)

    def test_unparseable_lock_is_reclaimed(self):
        with temp_home() as home:
            (home / "followup" / "state" / core.LOCK_FILE).write_text(
                "垃圾内容", encoding="utf-8")
            r = run_main([f"--today={TODAY}", "--force-push"], sheet())
            self.assertEqual(r.code, 0)
            self.assertIn("夺回陈旧运行锁", r.out)

    def test_lock_released_even_when_run_fails(self):
        """失败路径也必须释放锁，否则一次失败会锁死后续所有运行。"""
        with temp_home() as home:
            r = run_main([f"--today={TODAY}", "--force-push"], sheet(),
                         post_results=False)
            self.assertEqual(r.code, 1)
            self.assertFalse((home / "followup" / "state" / core.LOCK_FILE).exists())

    def test_diagnostic_runs_do_not_take_the_lock(self):
        """--dry-run / --json 只读，不该被真实运行挡住，也不该占锁。"""
        with temp_home() as home:
            write_lock(home, pid=os.getpid(), minutes_ago=0)
            r = run_main(["--dry-run"], sheet())
            self.assertEqual(r.code, 0)
            self.assertIn("甲公司", r.out, "试跑不该被锁挡住")


class LockPrimitiveTest(unittest.TestCase):

    def test_acquire_twice_raises(self):
        with temp_home():
            p, warn = core.acquire_lock()
            self.assertIsNone(warn)
            with self.assertRaises(core.LockBusy):
                core.acquire_lock()
            core.release_lock(p)
            p2, warn2 = core.acquire_lock()   # 释放后拿得到
            self.assertIsNone(warn2)
            core.release_lock(p2)

    def test_release_is_idempotent(self):
        with temp_home():
            p, _ = core.acquire_lock()
            core.release_lock(p)
            core.release_lock(p)   # 不该抛

    def test_pid_alive(self):
        self.assertTrue(core._pid_alive(os.getpid()))
        self.assertFalse(core._pid_alive(999_999))
        self.assertFalse(core._pid_alive(None))


if __name__ == "__main__":
    unittest.main()
