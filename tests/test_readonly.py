#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
诊断模式必须是**真的**不发不写。

═══════════════════════════════════════════════════════════════════════
这一组比别的组严：不看「有没有发消息」，而是**逐字节比对整个 state 目录**
——文件名集合、文件个数、每个文件的完整内容。

为什么要这么狠：上一轮我以为这三个开关已经干净了，实测才发现三处漏网，
每一处都不产生任何可见输出：

  · --dry-run 碰上坏掉的 stage_entered.json → 悄悄把它改名成 .corrupt.*
  · --json 碰上坏配置                        → 真发 telegram + 创建 health.json
  · --today（无 --force-push）碰上坏配置      → 同上

三处都是「只在故障路径上才发生」，而故障路径恰恰是人最常用 --dry-run
去看的时候。一个用来「看看情况」的开关，动了现场。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

from harness import (make_sheet, row, days_ago, temp_home, run_main,
                     output_cfg, ledgers_cfg)

import core   # noqa: E402 —— 必须在 harness 之后：是它把 scripts/ 挂上 sys.path

TODAY = date(2026, 7, 20)

# 三个诊断开关。每一条都必须严格只读。
DIAGNOSTIC_ARGVS = [
    ["--dry-run"],
    ["--dry-run", "--verbose"],
    ["--json"],
    [f"--today={TODAY}"],
    [f"--today={TODAY}", "--verbose"],
]


def snapshot_dir(home: Path) -> dict[str, bytes]:
    """整个 state 目录的完整快照：文件名 → 字节内容。"""
    d = home / "followup" / "state"
    if not d.exists():
        return {}
    return {p.name: p.read_bytes() for p in sorted(d.iterdir()) if p.is_file()}


def overdue_sheet():
    return make_sheet([
        row(1, "甲公司", tech="待收资", reported=days_ago(TODAY, 40),
            progress=days_ago(TODAY, 40)),
        row(2, "乙公司", tech="", reported=days_ago(TODAY, 30),
            progress=days_ago(TODAY, 30)),
    ])


class ReadOnlyBase(unittest.TestCase):

    def assert_untouched(self, argv, *, expect_code=None, **home_kw):
        """跑一次，断言 state 目录逐字节没变、没发任何消息。"""
        with temp_home(**home_kw) as home:
            before = snapshot_dir(home)
            r = run_main(argv, overdue_sheet())
            after = snapshot_dir(home)

            if expect_code is not None:
                self.assertEqual(r.code, expect_code, r.err)
            self.assertEqual(set(after), set(before),
                             f"{argv}：state 目录的文件集合变了\n{r.err}")
            self.assertEqual(len(after), len(before), f"{argv}：文件数量变了")
            for name in before:
                self.assertEqual(after[name], before[name],
                                 f"{argv}：{name} 的内容被改了")
            self.assertEqual(r.posts, [], f"{argv}：不许发企微")
            self.assertEqual(r.alerts, [], f"{argv}：不许发 telegram")
            self.assertNotIn("health.json", after, f"{argv}：不许创建健康记录")
            self.assertFalse([n for n in after if ".corrupt." in n],
                             f"{argv}：不许改名坏文件")
            self.assertFalse([n for n in after if n == core.LOCK_FILE],
                             f"{argv}：只读运行不该加锁")
            return r


class HappyPathTest(ReadOnlyBase):
    """一切正常时的三个开关。"""

    def test_all_diagnostic_switches_are_clean(self):
        for argv in DIAGNOSTIC_ARGVS:
            with self.subTest(argv=argv):
                self.assert_untouched(argv, expect_code=0)

    def test_starting_from_a_populated_state_dir(self):
        """空目录跑干净不算数 —— 已有状态时才可能被改写。"""
        state = {
            "stage_entered.json": {"box|1|collect": "2026-06-01"},
            "followup_state.json": {"box|1|collect": {"last_notified": "2026-07-01"}},
            "stage_history.json": {},
            "snapshot_last_box.json": {"1": {"node": "collect",
                                             "date": "2026-06-01"}},
            "health.json": {"last_full_success": "2026-07-19T09:00:00+08:00"},
        }
        for argv in DIAGNOSTIC_ARGVS:
            with self.subTest(argv=argv):
                with temp_home(state=state) as home:
                    before = snapshot_dir(home)
                    r = run_main(argv, overdue_sheet())
                    self.assertEqual(snapshot_dir(home), before,
                                     f"{argv}：已有状态被改动了\n{r.err}")
                    self.assertEqual(r.posts, [])
                    self.assertEqual(r.alerts, [])


class CorruptStateTest(ReadOnlyBase):
    """
    🔴 本轮的头号病灶：--dry-run 会把坏掉的状态文件改名。

    改名是「修复」，属于写入。而人用 --dry-run 恰恰是想看看现场什么样，
    看一眼就把现场动了，下次再跑文件已经不在原处。
    """

    BROKEN = {"stage_entered.json": "{ 这不是 JSON ",
              "followup_state.json": "[]",
              "stage_history.json": "null"}

    def test_diagnostic_modes_do_not_rename_corrupt_files(self):
        for argv in DIAGNOSTIC_ARGVS:
            with self.subTest(argv=argv):
                r = self.assert_untouched(argv, state=dict(self.BROKEN))
                # 只读不代表装作没看见 —— 该报的还得报
                self.assertIn("损坏", r.err + r.out,
                              f"{argv}：不改名不等于不报告")

    def test_corrupt_health_is_also_left_alone(self):
        """health.json 坏了同样不许在诊断模式下重建。"""
        for argv in DIAGNOSTIC_ARGVS:
            with self.subTest(argv=argv):
                with temp_home(state={"health.json": "{ 坏 "}) as home:
                    before = snapshot_dir(home)
                    run_main(argv, overdue_sheet())
                    self.assertEqual(snapshot_dir(home), before,
                                     f"{argv}：坏掉的 health.json 被动了")

    def test_real_run_does_quarantine(self):
        """对照组：真实运行该改名的还是要改名，否则这个修复就修过头了。"""
        with temp_home(state=dict(self.BROKEN)) as home:
            r = run_main([f"--today={TODAY}", "--force-push"], overdue_sheet())
            names = set(snapshot_dir(home))
            kept = [n for n in names if ".corrupt." in n]
            self.assertEqual(len(kept), 3, f"三个坏文件都该被保留下来：{names}")
            self.assertTrue(r.alerted, "状态损坏是重大降级，必须告警")
            h = r  # noqa: F841
            self.assertIn("last_recovery", str(snapshot_dir(home)
                                               .get("health.json", b""), "utf-8"),
                          "健康记录里要留下恢复事件")


class BadConfigTest(ReadOnlyBase):
    """
    配置坏掉的那一刻，人往往正在用 --dry-run 排查。那时更不该有副作用。

    旧实现的病灶：main() 里有**两个** real_run —— 早岔路用的是
    `not args.dry_run`，真正的那个在十几行之后才算出来。
    于是 --json / --today 从早岔路溜出去，真发 telegram、真写 health。
    """

    def test_broken_ledgers_json(self):
        for argv in DIAGNOSTIC_ARGVS:
            with self.subTest(argv=argv):
                with temp_home() as home:
                    (home / "followup" / "config" / "ledgers.json").write_text(
                        "[]", encoding="utf-8")
                    before = snapshot_dir(home)
                    r = run_main(argv, overdue_sheet())
                    self.assertEqual(r.code, 2)
                    self.assertEqual(snapshot_dir(home), before)
                    self.assertEqual(r.alerts, [],
                                     f"{argv}：🔴 撞上坏配置不许发 telegram")

    def test_broken_syntax(self):
        for argv in DIAGNOSTIC_ARGVS:
            with self.subTest(argv=argv):
                with temp_home() as home:
                    (home / "followup" / "config" / "rules.json").write_text(
                        "{ 坏 ", encoding="utf-8")
                    before = snapshot_dir(home)
                    r = run_main(argv, overdue_sheet())
                    self.assertEqual(r.code, 2)
                    self.assertEqual(snapshot_dir(home), before)
                    self.assertEqual(r.alerts, [])

    def test_broken_holidays_table(self):
        for argv in DIAGNOSTIC_ARGVS:
            with self.subTest(argv=argv):
                with temp_home(holidays="{ 坏掉的节假日表") as home:
                    before = snapshot_dir(home)
                    r = run_main(argv, overdue_sheet())
                    self.assertEqual(r.code, 2)
                    self.assertEqual(snapshot_dir(home), before)
                    self.assertEqual(r.alerts, [])

    def test_primary_channel_misconfigured(self):
        cfg = output_cfg(wecom_webhook={"enabled": False})
        for argv in DIAGNOSTIC_ARGVS:
            with self.subTest(argv=argv):
                self.assert_untouched(argv, expect_code=2, output=cfg)

    def test_no_enabled_ledger(self):
        for argv in DIAGNOSTIC_ARGVS:
            with self.subTest(argv=argv):
                self.assert_untouched(argv, expect_code=2,
                                      ledgers=ledgers_cfg(enabled=False))

    def test_bad_today_argument_does_not_alert(self):
        """参数写错是操作者当场看得到的，不该为一次手误往 telegram 发告警。"""
        with temp_home() as home:
            before = snapshot_dir(home)
            r = run_main(["--today=不是日期"], overdue_sheet())
            self.assertEqual(r.code, 2)
            self.assertEqual(snapshot_dir(home), before)
            self.assertEqual(r.alerts, [])
            self.assertIn("YYYY-MM-DD", r.err)


class FetchFailureTest(ReadOnlyBase):
    """取数失败是最需要拿 --dry-run 反复试的场景。"""

    def test_fetch_error_in_diagnostic_modes(self):
        for argv in DIAGNOSTIC_ARGVS:
            with self.subTest(argv=argv):
                with temp_home() as home:
                    before = snapshot_dir(home)
                    r = run_main(argv, overdue_sheet(),
                                 read_sheet_error="凭证失效（HTTP 401）")
                    self.assertEqual(r.code, 1, "故障仍然要非零退出")
                    self.assertEqual(snapshot_dir(home), before)
                    self.assertEqual(r.alerts, [],
                                     f"{argv}：诊断模式不该惊动 telegram")


class GateIsActuallyEngagedTest(unittest.TestCase):
    """
    两道防线各自在做什么，得分得清。

      第一道 = check_followup 里每个调用点显式的 `if real_run:`
      第二道 = core 层的硬闸门（本轮新增）

    健康状态下第二道**应该一次都不触发** —— 第一道已经全拦住了。
    所以 BLOCKED_WRITES 为空不是「闸门没用」，恰恰是「第一道完好」的证据；
    而它一旦非空，就等于点名说出：**哪个调用点忘了判断 real_run**。
    """

    def test_first_line_catches_everything_so_the_gate_never_fires(self):
        for argv in DIAGNOSTIC_ARGVS:
            with self.subTest(argv=argv):
                with temp_home():
                    run_main(argv, overdue_sheet())
                    self.assertEqual(
                        core.BLOCKED_WRITES, [],
                        f"{argv}：core 闸门拦下了写入，说明有调用点漏判 real_run —— "
                        f"拦住了是好事，但那个口子要补上")

    def test_real_run_blocks_nothing(self):
        with temp_home():
            run_main([f"--today={TODAY}", "--force-push"], overdue_sheet())
            self.assertEqual(core.BLOCKED_WRITES, [],
                             "真实运行不该有任何写入被拦")

    def test_core_gate_really_blocks_when_reached(self):
        """
        第二道防线本身是有效的 —— 绕过 check_followup 直接调 core 来证明。

        这条测的正是「将来有人加了新写入点、忘了判断」时会发生什么。
        """
        with temp_home() as home:
            core.set_read_only(True)
            core.write_state("stage_entered.json", {"x": 1})
            core.update_health(last_run_at="2026-01-01T00:00:00+08:00")
            state = home / "followup" / "state"
            self.assertFalse((state / "stage_entered.json").exists())
            self.assertFalse((state / "health.json").exists())
            self.assertEqual(len(core.BLOCKED_WRITES), 2)

    def test_core_gate_refuses_to_lock(self):
        with temp_home():
            core.set_read_only(True)
            with self.assertRaises(Exception):
                core.acquire_lock()


if __name__ == "__main__":
    unittest.main()
