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


def write_lock(home, *, pid, minutes_ago, token="外来的token"):
    p = home / "followup" / "state" / core.LOCK_FILE
    started = (core.now_iso() if minutes_ago == 0 else
               (core.parse_dt(core.now_iso()) -
                timedelta(minutes=minutes_ago)).isoformat(timespec="seconds"))
    p.write_text(json.dumps({"pid": pid, "started_at": started, "token": token}),
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

    def test_alive_but_slow_lock_is_not_stolen_but_is_alerted(self):
        """
        🔴 **行为变更（2026-08-01）**：进程还活着但跑了 31 分钟。

        旧实现直接夺锁 —— 那正是锁要防的事：那个进程还在写状态，
        夺了就是两个进程同时写。

        现在：**不夺，让路，但告警。** 真卡死的话每天都会跳，
        而「每天静默跳过」和「每天没有超时单」长得一模一样，
        所以必须响一声，让人有机会发现。
        """
        with temp_home() as home:
            write_lock(home, pid=os.getpid(), minutes_ago=31)
            r = run_main([f"--today={TODAY}", "--force-push"], sheet())
            self.assertEqual(r.code, 0, "让路不是故障")
            self.assertIn("本次跳过", r.err)
            self.assertIn("还没结束", r.err)
            self.assertTrue(r.alerted, "跑太久必须告警，不能闷声跳过")
            self.assertEqual(r.posts, [], "没拿到锁就不许干活")
            self.assertTrue((home / "followup" / "state" / core.LOCK_FILE).exists(),
                            "别人的锁还在，不许被删")

    def test_abandoned_lock_is_reclaimed_after_six_hours(self):
        """
        活着但超过 6 小时 —— 几乎肯定是僵死了。这时才夺。

        没有这个上限的话，一个永远卡住的进程会让催办永久停摆。
        """
        with temp_home() as home:
            write_lock(home, pid=os.getpid(), minutes_ago=7 * 60)
            r = run_main([f"--today={TODAY}", "--force-push"], sheet())
            self.assertEqual(r.code, 0)
            self.assertIn("夺回陈旧运行锁", r.out)
            self.assertIn("按僵死处理", r.out)
            self.assertTrue(r.alerted)
            self.assertEqual(len(r.posts), 1, "夺回后要照常干活")

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
            p, tok, warn = core.acquire_lock()
            self.assertIsNone(warn)
            self.assertTrue(tok)
            with self.assertRaises(core.LockBusy):
                core.acquire_lock()
            core.release_lock(p, tok)
            p2, tok2, warn2 = core.acquire_lock()   # 释放后拿得到
            self.assertIsNone(warn2)
            self.assertNotEqual(tok2, tok, "token 不可复用")
            core.release_lock(p2, tok2)

    def test_release_is_idempotent(self):
        with temp_home():
            p, tok, _ = core.acquire_lock()
            core.release_lock(p, tok)
            core.release_lock(p, tok)   # 不该抛

    def test_pid_alive(self):
        self.assertTrue(core._pid_alive(os.getpid()))
        self.assertFalse(core._pid_alive(999_999))
        self.assertFalse(core._pid_alive(None))


class OwnerTokenTest(unittest.TestCase):
    """
    🔴 释放前必须核对 owner token。

    不核对的话：A 的锁被 B 夺走后，A 跑完照样把 B 的锁删掉，
    于是 C 进来又拿到锁 —— **并发保护归零，而且完全无声。**
    """

    def test_wrong_token_does_not_delete_the_lock(self):
        with temp_home() as home:
            p, tok, _ = core.acquire_lock()
            core.release_lock(p, "别人的token")
            self.assertTrue(p.exists(), "不是自己的锁就不许删")
            core.release_lock(p, tok)
            self.assertFalse(p.exists(), "自己的锁才删得掉")

    def test_stolen_lock_survives_the_original_owners_release(self):
        """完整走一遍「A 持锁 → B 夺走 → A 释放」。"""
        with temp_home() as home:
            # A 拿到锁，然后把它改成一个已死进程持有的陈旧锁
            _, tok_a, _ = core.acquire_lock()
            write_lock(home, pid=999_999, minutes_ago=1, token="陈旧")

            # B 夺锁
            p_b, tok_b, steal = core.acquire_lock()
            self.assertIsNotNone(steal)

            # A 跑完了，来释放它以为还属于自己的锁
            core.release_lock(p_b, tok_a)
            self.assertTrue(p_b.exists(), "🔴 A 不许删掉 B 的锁")

            data = json.loads(p_b.read_text(encoding="utf-8"))
            self.assertEqual(data["token"], tok_b, "锁仍属于 B")


class RealRaceTest(unittest.TestCase):
    """
    真·双进程竞态。

    同一进程里顺序调两次 acquire_lock 测不出任何东西 ——
    竞态只在**两个真实进程同时进入那几行代码**时才发生。
    这里用 spawn 起独立解释器，用 Barrier 把它们对齐到同一瞬间起跑。
    """

    PROCS = 4
    ROUNDS = 15

    def _race(self, home, prep=None):
        """起 PROCS 个进程同时抢锁，返回结果列表。"""
        import multiprocessing as mp
        import lockworker

        ctx = mp.get_context("spawn")
        results = []
        for _ in range(self.ROUNDS):
            lock = home / "followup" / "state" / core.LOCK_FILE
            if lock.exists():
                lock.unlink()
            if prep:
                prep(home)

            barrier = ctx.Barrier(self.PROCS)
            queue = ctx.Queue()
            procs = [ctx.Process(target=lockworker.grab, args=(barrier, queue))
                     for _ in range(self.PROCS)]
            for p in procs:
                p.start()
            got = [queue.get(timeout=60) for _ in range(self.PROCS)]
            for p in procs:
                p.join(timeout=30)
            results.append(got)
        return results

    def test_exactly_one_wins_on_an_empty_lock(self):
        with temp_home() as home:
            for i, got in enumerate(self._race(home)):
                kinds = [k for k, _ in got]
                self.assertNotIn("error", kinds, f"第 {i+1} 轮出异常：{got}")
                self.assertNotIn("barrier-failed", kinds, f"第 {i+1} 轮同步失败")
                self.assertEqual(
                    kinds.count("ok"), 1,
                    f"🔴 第 {i+1} 轮有 {kinds.count('ok')} 个进程同时拿到锁：{got}")

    def test_exactly_one_wins_when_stealing_a_stale_lock(self):
        """
        **这一条才是真正测新实现的。** 空锁那条走的是单次 O_EXCL，
        本来就原子；而夺陈旧锁是旧实现用 `write_text()` 的地方 ——
        四个进程会同时判定「已陈旧」，然后双双写入、双双以为自己赢了。
        """
        def prep(home):
            write_lock(home, pid=999_999, minutes_ago=5, token="陈旧的token")

        with temp_home() as home:
            for i, got in enumerate(self._race(home, prep=prep)):
                kinds = [k for k, _ in got]
                self.assertNotIn("error", kinds, f"第 {i+1} 轮出异常：{got}")
                self.assertEqual(
                    kinds.count("ok"), 1,
                    f"🔴 第 {i+1} 轮有 {kinds.count('ok')} 个进程同时夺到锁：{got}")

    def test_tokens_are_unique_across_processes(self):
        with temp_home() as home:
            tokens = [tok for got in self._race(home)
                      for kind, tok in got if kind == "ok"]
            self.assertEqual(len(tokens), len(set(tokens)), "token 不许重复")


if __name__ == "__main__":
    unittest.main()
