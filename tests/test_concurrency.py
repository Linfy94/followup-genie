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
import time
import unittest
from datetime import date, timedelta
from unittest import mock

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


def write_marker(home, token, *, seconds_ago: int):
    """造一个抢锁标记，并把 mtime 拨老 —— 模拟进程死在抢锁半路。"""
    p = home / "followup" / "state" / f"{core.LOCK_FILE}.steal.{token}"
    p.write_text("{}", encoding="utf-8")
    t = time.time() - seconds_ago
    os.utime(p, (t, t))
    return p


class StealMarkerRecoveryTest(unittest.TestCase):
    """
    🔴 0.3.0-rc2 修的头号问题：**残留的抢锁标记会把运行永久堵死。**

    进程在建完标记之后、unlink 之前被 SIGKILL（关机、强杀、OOM），
    标记就留在盘上。锁没被替换 → holder token 不变 → 下次算出**同一个**
    标记名 → 永远建不上 → 每天 LockBusy。

    而当时的清理函数只在「抢到标记之后」的 finally 里跑，
    正好够不着自己造成的死锁。修复前实测：连试三次，三次都被堵死。
    """

    def test_stale_marker_is_recovered(self):
        with temp_home() as home:
            write_lock(home, pid=999_999, minutes_ago=60, token="T")
            marker = write_marker(home, "T", seconds_ago=300)

            path, token, steal = core.acquire_lock()
            self.assertIsNotNone(steal, "夺陈旧锁必须告警")
            self.assertFalse(marker.exists(), "残留标记应被清掉")
            core.release_lock(path, token)

    def test_stale_marker_does_not_block_forever(self):
        """修复前这个循环三次全是 LockBusy。"""
        with temp_home() as home:
            write_lock(home, pid=999_999, minutes_ago=60, token="T")
            write_marker(home, "T", seconds_ago=300)

            for attempt in range(3):
                try:
                    path, token, _ = core.acquire_lock()
                except core.LockBusy as e:
                    self.fail(f"第 {attempt + 1} 次仍被残留标记堵死：{e}")
                core.release_lock(path, token)
                # 下一轮重新布置同样的现场
                write_lock(home, pid=999_999, minutes_ago=60, token="T")
                write_marker(home, "T", seconds_ago=300)

    def test_fresh_marker_still_yields(self):
        """
        自愈不能矫枉过正：标记还新鲜，说明**真有**另一个进程正在抢，
        这时必须让路。踢掉它就等于两个进程同时夺锁 —— 正是标记要防的事。
        """
        with temp_home() as home:
            write_lock(home, pid=999_999, minutes_ago=60, token="T")
            marker = write_marker(home, "T", seconds_ago=1)

            with self.assertRaises(core.LockBusy):
                core.acquire_lock()
            self.assertTrue(marker.exists(), "新鲜标记不该被清掉")

    def test_sweep_runs_at_entry_not_only_on_success(self):
        """清理必须在入口跑 —— 放在成功后的 finally 里就够不着死锁。"""
        with temp_home() as home:
            old = write_marker(home, "早就没人要的token", seconds_ago=600)
            path, token, _ = core.acquire_lock()   # 空锁，一次就成功
            self.assertFalse(old.exists(), "入口那次 sweep 应该清掉它")
            core.release_lock(path, token)

    def test_marker_for_a_different_lock_is_left_alone_when_fresh(self):
        with temp_home() as home:
            other = write_marker(home, "别人的token", seconds_ago=5)
            path, token, _ = core.acquire_lock()
            self.assertTrue(other.exists(), "新鲜的别家标记不该被误清")
            core.release_lock(path, token)


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
        """
        起 PROCS 个进程同时抢锁，返回结果列表。

        🔴 `hold=True` 是必须的，不是可选项。

        worker 若拿到锁就立刻释放，那么「一轮里有几个 ok」根本不是互斥的
        度量 —— A 拿到、A 释放、D 再拿到，两个 ok 但**全程只有一个持有者**，
        完全合法。实测确实会偶发（两条 busy 指向同一个 pid、且没有夺锁告警，
        就是这种情况）。

        让赢家**攥着不放**，「恰好一个 ok」才真正等价于「同一时刻只有一个持有者」。
        """
        import multiprocessing as mp
        import lockworker

        ctx = mp.get_context("spawn")
        results = []
        for _ in range(self.ROUNDS):
            lock = home / "followup" / "state" / core.LOCK_FILE
            if lock.exists():
                lock.unlink()
            for m in (home / "followup" / "state").glob(f"{core.LOCK_FILE}.steal.*"):
                m.unlink()
            if prep:
                prep(home)

            barrier = ctx.Barrier(self.PROCS)
            queue = ctx.Queue()
            procs = [ctx.Process(target=lockworker.grab,
                                 args=(barrier, queue, True))
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

    def test_exactly_one_wins_when_a_stale_marker_is_left_behind(self):
        """
        🔴 0.3.0-rc2 的核心并发回归：**自愈不能把互斥一起愈掉。**

        现场是最糟的那种 —— 陈旧锁 + 残留标记，四个进程同时进来。
        它们会同时发现标记残留、同时想 unlink 后重建。
        只要有一轮出现两个 ok，就说明清理动作本身把互斥破坏了。

        单进程顺序调用永远测不出这一条。
        """
        def prep(home):
            write_lock(home, pid=999_999, minutes_ago=60, token="T")
            write_marker(home, "T", seconds_ago=300)

        with temp_home() as home:
            for i, got in enumerate(self._race(home, prep=prep)):
                kinds = [k for k, _ in got]
                self.assertNotIn("error", kinds, f"第 {i+1} 轮出异常：{got}")
                self.assertNotIn("barrier-failed", kinds, f"第 {i+1} 轮同步失败")
                self.assertEqual(
                    kinds.count("ok"), 1,
                    f"🔴 第 {i+1} 轮有 {kinds.count('ok')} 个进程同时夺到锁：{got}")

    def test_lock_released_mid_acquire_does_not_let_two_in(self):
        """
        🔴 真实竞态窗口，靠 15 轮四进程实测抓出来的（第 11 轮 2 个 ok）：

          A 持锁 → B 的 O_EXCL 失败 → **A 释放** → B 读锁读到空
            → holder 是 {}、token 是 None
              → 走进夺锁路径，而「token 有没有变」拿 None 和 None 比
                → 必然相等 → 校验通过 → os.replace
                  → C 同时也这么干 → **两个进程同时持锁**

        这里用 mock 把那个窗口固定下来：O_EXCL 第一次失败、读锁读到空。
        正确行为是重新走一次 O_EXCL，而不是去夺一把根本不存在的锁。
        """
        with temp_home() as home:
            p = home / "followup" / "state" / core.LOCK_FILE
            calls = {"n": 0}
            real_create = core._create_lock_exclusive

            def flaky(path, payload):
                # 第一次假装被别人抢先，之后恢复真实行为
                if path == p and calls["n"] == 0:
                    calls["n"] += 1
                    return False
                return real_create(path, payload)

            with mock.patch.object(core, "_create_lock_exclusive", flaky):
                path, token, steal = core.acquire_lock()

            self.assertIsNone(steal, "锁不存在时不该报「夺回陈旧锁」")
            self.assertEqual(core._read_lock(p).get("token"), token)
            core.release_lock(path, token)

    def test_half_created_lock_is_never_stolen(self):
        """
        🔴 这是 15 轮四进程反复抓到「2 个赢家」的**真正**根因。

        `_create_lock_exclusive` 是 `os.open(O_EXCL)` 之后才 `os.write`——
        这两个系统调用之间，锁文件是 **0 字节**的。而空内容 parse 出来是 `{}`，
        与「锁文件损坏」完全无法区分：

          A: open(O_EXCL) 成功，还没 write
            B: open 失败 → 读到 0 字节 → 判成「损坏的陈旧锁」→ 夺走
              → A 的 fd 指向已被 os.replace 掉的旧 inode，
                A 仍以为自己持锁 → **两个进程同时在跑**

        合法的废弃锁一定有内容，所以空文件只可能是这个中间态。
        """
        with temp_home() as home:
            p = home / "followup" / "state" / core.LOCK_FILE
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            try:
                self.assertEqual(p.stat().st_size, 0, "先造出那个中间态")
                with self.assertRaises(core.LockBusy) as ctx:
                    core.acquire_lock()
                self.assertIn("正在创建", str(ctx.exception))
            finally:
                os.close(fd)

    def test_whitespace_only_lock_is_also_treated_as_half_created(self):
        with temp_home() as home:
            p = home / "followup" / "state" / core.LOCK_FILE
            p.write_text("   \n", encoding="utf-8")
            with self.assertRaises(core.LockBusy):
                core.acquire_lock()

    def test_garbage_lock_is_still_reclaimable(self):
        """
        自愈不能矫枉过正：**有内容**但解析不了的锁，仍然该夺回。
        那才是真的损坏，不夺的话催办会永久停摆。
        """
        with temp_home() as home:
            p = home / "followup" / "state" / core.LOCK_FILE
            p.write_text("这不是 JSON", encoding="utf-8")
            path, token, steal = core.acquire_lock()
            self.assertIsNotNone(steal, "损坏的锁应该被夺回并告警")
            core.release_lock(path, token)

    def test_empty_lock_race_is_stable_under_repetition(self):
        """把空锁竞态多跑几轮 —— 那个窗口只在负载高时才够宽。"""
        with temp_home() as home:
            for i, got in enumerate(self._race(home)):
                kinds = [k for k, _ in got]
                self.assertEqual(
                    kinds.count("ok"), 1,
                    f"🔴 第 {i+1} 轮有 {kinds.count('ok')} 个进程同时拿到锁：{got}")

    def test_tokens_are_unique_across_processes(self):
        with temp_home() as home:
            tokens = [tok for got in self._race(home)
                      for kind, tok in got if kind == "ok"]
            self.assertEqual(len(tokens), len(set(tokens)), "token 不许重复")


if __name__ == "__main__":
    unittest.main()
