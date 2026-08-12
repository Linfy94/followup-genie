#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
需求文档变更探测。

═══════════════════════════════════════════════════════════════════════
业务每天推送前会对着《跟进精灵需求文档》人工核一遍「逻辑有没有变」。
这一段把「有没有变」自动化 —— **只回答变没变，不回答变了什么**。
价值在于：文档没变的那些天，那次人工核对完全可以不做。

rc8 那次是真实代价：哨兵④发货的节律早已从「每周三」改成「每周一和每周四」，
配置没跟上，业务周一来问「怎么不提醒」。读懂改了什么才是花时间的部分，
但**代价全在没发现** —— 已经晚了几周。

四条行为，每条都能单独把这个功能变成废物，所以逐条钉住：

  ① 首次没基线只记录、不提示 —— 一上来就报一条谁也没法处理的警报，
     只会教人忽略它
  ② 变更提示**持续到人工确认为止**，不是只响一天 —— 只响一天等于
     「那天没看到就永久错过」，正好回到这个项目一路在消灭的形状
  ③ 提示**绝不能进 run_warnings** —— 那会让未确认的每一天都少一次
     last_full_success，两天后看门狗误报「任务根本没跑」
  ④ 取指纹失败要说出来、但不改退出码 —— 否则「文档没变」和「没查成」
     在输出上长得一模一样
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import unittest
from datetime import date

from harness import (make_sheet, row, temp_home, run_main, read_state,
                     state_files, rules_cfg, ledgers_cfg, output_cfg)

import core

TODAY = date(2026, 7, 20)
DOC = "DR1ZTESTDOCID"

FP_OLD = {"title": "跟进精灵需求文档", "last_modify_name": "张三",
          "last_modify_time": "1786500000000"}
FP_NEW = {"title": "跟进精灵需求文档", "last_modify_name": "李四",
          "last_modify_time": "1786600000000"}


def rules_watching(*, name="跟进精灵需求文档", file_id=DOC):
    r = rules_cfg()
    r["spec_watch"] = [{"file_id": file_id, "name": name}]
    return r


def sheet():
    return make_sheet([row(1, "甲公司", tech="待收资",
                           reported=date(2026, 6, 1),
                           progress=date(2026, 6, 1))])


def go(home_fp, argv=None, **kw):
    return run_main(argv or [f"--today={TODAY}", "--force-push"], sheet(),
                    fingerprints=home_fp, **kw)


def baseline(home) -> dict:
    return read_state(home, core.SPEC_WATCH_FILE) or {}


class FirstRunTest(unittest.TestCase):
    """① 首次没有基线：静静记下来，不提示。"""

    def test_no_notice_on_first_sight(self):
        with temp_home(rules=rules_watching()):
            r = go({DOC: FP_OLD})
            self.assertNotIn("需求文档", r.out,
                             "第一次见到就报警等于教人忽略这条提示")
            self.assertEqual(r.code, 0)

    def test_baseline_is_recorded(self):
        with temp_home(rules=rules_watching()) as home:
            go({DOC: FP_OLD})
            self.assertEqual(baseline(home).get(DOC, {}).get("last_modify_time"),
                             FP_OLD["last_modify_time"])

    def test_unchanged_stays_quiet(self):
        with temp_home(rules=rules_watching()):
            go({DOC: FP_OLD})
            r = go({DOC: FP_OLD})
            self.assertNotIn("需求文档", r.out)


class ChangeDetectedTest(unittest.TestCase):
    """② 变了就说，而且一直说到人工确认。"""

    def test_change_is_reported(self):
        with temp_home(rules=rules_watching()):
            go({DOC: FP_OLD})
            r = go({DOC: FP_NEW})
            self.assertIn("需求文档", r.out)
            self.assertIn("李四", r.out, "要说清是谁改的")
            self.assertIn("--ack-spec", r.out, "要给出消除提示的办法")

    def test_notice_repeats_every_day_until_acknowledged(self):
        """
        🔴 本文件最要紧的一条。只响一天的提示 == 那天没看到就永久错过。
        """
        with temp_home(rules=rules_watching()):
            go({DOC: FP_OLD})
            for day in range(3):
                r = go({DOC: FP_NEW})
                self.assertIn("需求文档", r.out, f"第 {day + 1} 天就不提示了")

    def test_baseline_is_not_advanced_by_merely_noticing(self):
        """看见了不等于处理了 —— 基线只有 --ack-spec 才动。"""
        with temp_home(rules=rules_watching()) as home:
            go({DOC: FP_OLD})
            go({DOC: FP_NEW})
            self.assertEqual(baseline(home)[DOC]["last_modify_time"],
                             FP_OLD["last_modify_time"])

    def test_rename_alone_counts_as_a_change(self):
        renamed = dict(FP_OLD, title="跟进精灵需求文档（作废）")
        with temp_home(rules=rules_watching()):
            go({DOC: FP_OLD})
            self.assertIn("需求文档", go({DOC: renamed}).out)


class AckTest(unittest.TestCase):
    """③ --ack-spec：核对完了，把当前状态记成新基线。"""

    def test_ack_updates_the_baseline_and_silences_the_notice(self):
        with temp_home(rules=rules_watching()) as home:
            go({DOC: FP_OLD})
            self.assertIn("需求文档", go({DOC: FP_NEW}).out)

            r = run_main(["--ack-spec"], sheet(), fingerprints={DOC: FP_NEW})
            self.assertEqual(r.code, 0, r.err)
            self.assertEqual(baseline(home)[DOC]["last_modify_time"],
                             FP_NEW["last_modify_time"])

            self.assertNotIn("需求文档", go({DOC: FP_NEW}).out)

    def test_ack_notices_the_next_change_again(self):
        newer = dict(FP_NEW, last_modify_time="1786700000000")
        with temp_home(rules=rules_watching()):
            go({DOC: FP_OLD})
            run_main(["--ack-spec"], sheet(), fingerprints={DOC: FP_NEW})
            self.assertIn("需求文档", go({DOC: newer}).out)

    def test_ack_refuses_to_run_in_diagnostic_mode(self):
        """--ack-spec 要写状态，与只读开关同时给是自相矛盾，必须明确拒绝。"""
        for flags in (["--ack-spec", "--dry-run"],
                      ["--ack-spec", "--json"],
                      ["--ack-spec", f"--today={TODAY}"]):
            with self.subTest(flags=flags):
                with temp_home(rules=rules_watching()) as home:
                    r = run_main(flags, sheet(), fingerprints={DOC: FP_NEW})
                    self.assertEqual(r.code, 2, r.out + r.err)
                    self.assertNotIn(core.SPEC_WATCH_FILE, state_files(home))

    def test_ack_does_not_advance_a_doc_it_could_not_read(self):
        """
        🔴 取不到指纹的那份**不更新基线**。记一个取不到的值等于把提示
           永久消音，而它本该继续提醒。

           必须用「一份读得到 + 一份读不到」：只有一份读不到时根本不会落盘，
           断言会因为「压根没写」而通过，测不到「写的时候有没有带上它」。
        """
        other = "DR1ZOTHERDOC"
        r = rules_cfg()
        r["spec_watch"] = [{"file_id": DOC, "name": "需求文档"},
                           {"file_id": other, "name": "补充说明"}]
        with temp_home(rules=r,
                       state={core.SPEC_WATCH_FILE: {DOC: FP_OLD,
                                                     other: FP_OLD}}) as home:
            run = run_main(["--ack-spec"], sheet(),
                           fingerprints={DOC: core.LedgerError("凭证失效"),
                                         other: FP_NEW})
            self.assertNotEqual(run.code, 0, "有一份没确认成，要说出来")
            self.assertEqual(baseline(home)[other]["last_modify_time"],
                             FP_NEW["last_modify_time"], "读得到的那份要推进")
            self.assertEqual(baseline(home)[DOC]["last_modify_time"],
                             FP_OLD["last_modify_time"],
                             "读不到的那份不许推进，否则提示被永久消音")


class ReadFailureTest(unittest.TestCase):
    """④ 读不到要说，但不许因此判定当天的催办失败。"""

    def test_failure_is_reported_but_exit_code_stays_zero(self):
        with temp_home(rules=rules_watching()):
            r = go({DOC: core.LedgerError("网络不通")})
            self.assertIn("没核对成", r.out)
            self.assertIn("不是", r.out, "要说清这不等于「文档没变」")
            self.assertEqual(r.code, 0,
                             "需求文档跟催办判定无关，读不到它不该让当天算失败")

    def test_failure_does_not_wipe_an_existing_baseline(self):
        with temp_home(rules=rules_watching()) as home:
            go({DOC: FP_OLD})
            go({DOC: core.LedgerError("网络不通")})
            self.assertEqual(baseline(home)[DOC]["last_modify_time"],
                             FP_OLD["last_modify_time"])


class DoesNotBreakHealthTest(unittest.TestCase):
    """
    🔴 ③ 提示绝不能进 run_warnings。

    `exit_code == 0 and not run_warnings` 才写 last_full_success，
    而看门狗靠它判断「任务有没有跑」。塞进去会让文档变更未确认的每一天
    都少一次成功记录，两天后误报「任务根本没跑」——
    修好一个静默，换来一个假警报。节假日闸门当初踩的就是这个坑。
    """

    def _last_full_success(self, home):
        return (read_state(home, "health.json") or {}).get("last_full_success")

    def test_pending_change_still_records_a_full_success(self):
        """
        🔴 基线**预置**在 state 里，让「有提示」的那次成为唯一一次运行。

           先跑一次干净的、再跑一次有提示的，是测不出东西的：
           `last_full_success` 上一次就写好了，第二次不写也照样是真值，
           断言永远绿 —— 一个漏报的检查比没有检查更糟。
        """
        with temp_home(rules=rules_watching(),
                       state={core.SPEC_WATCH_FILE: {DOC: FP_OLD}}) as home:
            r = go({DOC: FP_NEW})
            self.assertIn("需求文档", r.out, "前提：这一次确实带着提示")
            self.assertTrue(self._last_full_success(home),
                            "文档变更是待办、不是故障，"
                            "不能让看门狗两天后误报「任务根本没跑」")

    def test_read_failure_still_records_a_full_success(self):
        with temp_home(rules=rules_watching()) as home:
            r = go({DOC: core.LedgerError("网络不通")})
            self.assertIn("没核对成", r.out)
            self.assertTrue(self._last_full_success(home))


class ReadOnlyTest(unittest.TestCase):
    """诊断模式严格只读 —— 连这个新状态文件也不许写。"""

    def test_dry_run_writes_nothing(self):
        with temp_home(rules=rules_watching()) as home:
            r = run_main(["--dry-run"], sheet(), fingerprints={DOC: FP_OLD})
            self.assertEqual(r.code, 0)
            self.assertNotIn(core.SPEC_WATCH_FILE, state_files(home))

    def test_json_mode_writes_nothing(self):
        with temp_home(rules=rules_watching()) as home:
            run_main(["--json"], sheet(), fingerprints={DOC: FP_OLD})
            self.assertNotIn(core.SPEC_WATCH_FILE, state_files(home))


class NotConfiguredTest(unittest.TestCase):
    """没配 spec_watch 时，整段不该有任何存在感 —— 包括不去联网。"""

    def test_absent_config_makes_no_call_and_no_state(self):
        called = []

        def fp(fid):
            called.append(fid)
            return FP_OLD

        with temp_home() as home:                 # 默认 rules 里没有 spec_watch
            r = run_main([f"--today={TODAY}", "--force-push"], sheet(),
                         fingerprints=fp)
            self.assertEqual(r.code, 0)
            self.assertEqual(called, [], "没配就不该去查")
            self.assertNotIn(core.SPEC_WATCH_FILE, state_files(home))


class ConfigValidationTest(unittest.TestCase):
    """spec_watch 段写坏时离线就拦下来，别等到明早才发现。"""

    def _errs(self, spec):
        r = rules_cfg()
        r["spec_watch"] = spec
        return core.validate_configs(ledgers_cfg(), r, output_cfg())

    def test_valid_entry_passes(self):
        self.assertEqual(self._errs([{"file_id": DOC, "name": "需求文档"}]), [])

    def test_absent_section_passes(self):
        self.assertEqual(core.validate_configs(ledgers_cfg(), rules_cfg(),
                                               output_cfg()), [])

    def test_not_a_list(self):
        self.assertTrue(self._errs({"file_id": DOC}))

    def test_entry_not_an_object(self):
        self.assertTrue(self._errs([DOC]))

    def test_missing_file_id(self):
        errs = self._errs([{"name": "需求文档"}])
        self.assertTrue(errs)
        self.assertIn("file_id", "\n".join(errs))

    def test_missing_name(self):
        """提示里要说清是哪份文档 —— 只报一串 file_id 业务看不懂。"""
        errs = self._errs([{"file_id": DOC}])
        self.assertTrue(errs)
        self.assertIn("name", "\n".join(errs))


class MultipleDocsTest(unittest.TestCase):
    """可以盯多份。一份变了不该把另一份的状态也带着动。"""

    OTHER = "DR1ZOTHERDOC"

    def rules(self):
        r = rules_cfg()
        r["spec_watch"] = [{"file_id": DOC, "name": "需求文档"},
                           {"file_id": self.OTHER, "name": "补充说明"}]
        return r

    def test_only_the_changed_one_is_reported(self):
        with temp_home(rules=self.rules()) as home:
            go({DOC: FP_OLD, self.OTHER: FP_OLD})
            r = go({DOC: FP_NEW, self.OTHER: FP_OLD})
            self.assertIn("需求文档", r.out)
            self.assertNotIn("补充说明", r.out)
            self.assertEqual(baseline(home)[self.OTHER]["last_modify_time"],
                             FP_OLD["last_modify_time"])

    def test_one_unreadable_does_not_hide_the_other(self):
        with temp_home(rules=self.rules()):
            go({DOC: FP_OLD, self.OTHER: FP_OLD})
            r = go({DOC: core.LedgerError("读不到"), self.OTHER: FP_NEW})
            self.assertIn("没核对成", r.out)
            self.assertIn("补充说明", r.out)


if __name__ == "__main__":
    unittest.main()
