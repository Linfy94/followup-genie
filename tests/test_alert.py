#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P0-B：故障告警不得静默失败。

企微是唯一的内容通道。它挂了 → 业务完全收不到 → 而「完全静默」和
「今天没有超时单」长得一模一样。告警是这条链上最后一道让人知情的机制，
**它自己挂了必须留下痕迹**，绝不能一声不响地返回。
"""

from __future__ import annotations

import unittest
from datetime import date

# harness 必须先导入 —— 是它把 scripts/ 放进 sys.path 的
from harness import (make_sheet, row, days_ago, temp_home, run_main,  # noqa: I001
                     read_state, output_cfg)
import check_followup  # noqa: E402

TODAY = date(2026, 7, 20)


class _proc:
    """最小的 CompletedProcess 替身，只带 _cli_output 关心的两个字段。"""

    def __init__(self, stderr="", stdout=""):
        self.stderr = stderr
        self.stdout = stdout


def overdue_sheet():
    return make_sheet([
        row(1, "甲公司", tech="待收资", reported=days_ago(TODAY, 40),
            progress=days_ago(TODAY, 40)),
    ])


class AlertTest(unittest.TestCase):

    def test_alert_success_is_recorded(self):
        with temp_home() as home:
            r = run_main([f"--today={TODAY}", "--force-push"], overdue_sheet(),
                         post_results=False, alert_ok=True)
            self.assertEqual(r.code, 1)
            self.assertTrue(r.alerted)
            h = read_state(home, "health.json")
            self.assertIs(h["alert_ok"], True)

    def test_hermes_send_nonzero_exit_is_not_swallowed(self):
        """
        🔴 旧实现的病根：subprocess.run(...) 之后不看 returncode，
           hermes send 失败也当成功返回。
        """
        with temp_home() as home:
            r = run_main([f"--today={TODAY}", "--force-push"], overdue_sheet(),
                         post_results=False, alert_ok=False)
            self.assertEqual(r.code, 1, "主任务仍按主任务的成败判定")
            self.assertIn("故障告警发送失败", r.err)
            h = read_state(home, "health.json")
            self.assertIs(h["alert_ok"], False)
            self.assertIn("退出码 3", h["alert_detail"])

    def test_failure_detail_keeps_stderr(self):
        r = check_followup._cli_output(_proc(stderr="boom from stderr", stdout=""))
        self.assertIn("stderr=boom from stderr", r)

    def test_failure_detail_keeps_stdout_too(self):
        """
        🔴 2026-08-06 09:00：hermes send 退出码 1，**stderr 为空**，
           旧实现只看 stderr → health.json 里只留「退出码 1」，无从查起。
           错误信息很可能一直在 stdout 里，只是被丢掉了。
        """
        r = check_followup._cli_output(_proc(stderr="", stdout="boom from stdout"))
        self.assertIn("stdout=boom from stdout", r)

    def test_failure_detail_says_so_when_both_empty(self):
        """两个都空也要明说，不能留一句光秃秃的退出码让人以为细节没记全。"""
        r = check_followup._cli_output(_proc(stderr="", stdout=""))
        self.assertIn("无输出", r)

    def test_hermes_not_on_path_is_loud(self):
        """
        cron 由 launchd 派生的 gateway 启动，PATH 与登录终端不同。
        本机恰好能找到是偶然，业务那台不保证。
        """
        with temp_home() as home:
            r = run_main([f"--today={TODAY}", "--force-push"], overdue_sheet(),
                         post_results=False, hermes_found=False)
            self.assertEqual(r.code, 1)
            self.assertIn("找不到 hermes", r.err)
            self.assertIn("原本要告警的内容", r.err,
                          "发不出去时至少要把内容打进日志，不能凭空消失")
            h = read_state(home, "health.json")
            self.assertIs(h["alert_ok"], False)

    def test_no_alert_target_configured(self):
        with temp_home(env_lines=[
            "TENCENT_DOCS_TOKEN=fake",
            "FOLLOWUP_WECOM_WEBHOOK=https://qyapi.weixin.qq.com/x?key=fake",
        ]) as home:
            r = run_main([f"--today={TODAY}", "--force-push"], overdue_sheet(),
                         post_results=False)
            self.assertEqual(r.code, 1)
            self.assertIn("FOLLOWUP_ALERT_TARGET", r.err)
            self.assertIs(read_state(home, "health.json")["alert_ok"], False)

    def test_alert_disabled_by_config(self):
        cfg = output_cfg(alert={"enabled": False})
        with temp_home(output=cfg):
            r = run_main([f"--today={TODAY}", "--force-push"], overdue_sheet(),
                         post_results=False)
            self.assertEqual(r.code, 1, "关掉告警不改变主任务的成败")
            self.assertEqual(r.alerts, [], "关了就不该发")

    def test_alert_failure_does_not_mask_main_success(self):
        """
        「告警发出去了」≠「主任务成功」，反过来也一样：
        主任务成功时不该有告警，退出码必须是 0。
        """
        with temp_home():
            r = run_main([f"--today={TODAY}", "--force-push"], overdue_sheet(),
                         post_results=True, alert_ok=False)
            self.assertEqual(r.code, 0)
            self.assertEqual(r.alerts, [])

    def test_alert_target_never_printed_in_full(self):
        """告警目标是个人标识，日志里只该出现平台名。"""
        with temp_home():
            r = run_main([f"--today={TODAY}", "--force-push"], overdue_sheet(),
                         post_results=False)
            self.assertNotIn("telegram:000000", r.out + r.err)


class HealthRecordTest(unittest.TestCase):
    """健康记录抓的是别的机制都抓不到的那类失败：**根本没跑**。"""

    def test_success_records_full_success_and_resets_counter(self):
        with temp_home(state={"health.json": {"consecutive_failures": 5}}) as home:
            run_main([f"--today={TODAY}", "--force-push"], overdue_sheet(),
                     post_results=True)
            h = read_state(home, "health.json")
            self.assertTrue(h["last_full_success"])
            self.assertTrue(h["last_fetch_ok"])
            self.assertTrue(h["last_wecom_ok"])
            self.assertEqual(h["consecutive_failures"], 0)

    def test_failures_accumulate(self):
        with temp_home() as home:
            for _ in range(3):
                run_main([f"--today={TODAY}", "--force-push"], overdue_sheet(),
                         post_results=False)
            h = read_state(home, "health.json")
            self.assertEqual(h["consecutive_failures"], 3)
            self.assertEqual(h["last_failure"]["stage"], "企微推送")
            self.assertIsNone(h.get("last_full_success"))

    def test_fetch_failure_recorded_separately(self):
        with temp_home() as home:
            r = run_main([f"--today={TODAY}", "--force-push"], overdue_sheet(),
                         read_sheet_error="凭证失效或无权限（HTTP 401）")
            self.assertEqual(r.code, 1)
            self.assertTrue(r.alerted)
            h = read_state(home, "health.json")
            self.assertEqual(h["last_failure"]["stage"], "取数/判定")
            self.assertIn("401", h["last_failure"]["reason"])
            self.assertIsNone(h.get("last_fetch_ok"))

    def test_dry_run_does_not_touch_health(self):
        with temp_home() as home:
            run_main(["--dry-run"], overdue_sheet())
            self.assertIsNone(read_state(home, "health.json"))


class AlertTimeoutConfigTest(unittest.TestCase):
    """
    🔴 告警函数**只在已经出事时**被调用。让它因为一个配置笔误裸崩，
       等于把「有故障」升级成「主脚本崩掉、连故障是什么都说不出来」。
       离线校验是第一道，运行时兜底是第二道，两道都要有。
    """

    def _send(self, timeout_value):
        from unittest import mock
        with temp_home():
            with mock.patch.object(check_followup, "_hermes_bin",
                                   return_value="/usr/local/bin/hermes"), \
                 mock.patch.object(check_followup.subprocess, "run",
                                   return_value=mock.Mock(returncode=0, stdout="", stderr="")):
                return check_followup.alert(
                    "测试", output_cfg(alert={"enabled": True,
                                              "timeout_seconds": timeout_value}))

    def test_non_numeric_timeout_does_not_crash(self):
        ok, _ = self._send("三十")
        self.assertTrue(ok, "配置写错也要把告警发出去")

    def test_zero_and_negative_fall_back_to_default(self):
        for bad in (0, -5):
            with self.subTest(bad=bad):
                ok, _ = self._send(bad)
                self.assertTrue(ok)

    def test_valid_value_is_used(self):
        self.assertEqual(
            check_followup._alert_timeout({"timeout_seconds": "45"}, None), 45)

    def test_missing_value_uses_default(self):
        self.assertEqual(
            check_followup._alert_timeout({}, None),
            check_followup.ALERT_TIMEOUT_DEFAULT)

    def test_offline_check_catches_it_too(self):
        """第一道：doctor --validate-config 必须先拦下来，不能报「通过」。"""
        import core
        errs = core.validate_configs(
            {}, {}, {"alert": {"timeout_seconds": "三十"}})
        self.assertTrue(any("timeout_seconds" in e for e in errs), errs)


class AlertRetryTest(unittest.TestCase):
    """
    告警撞上几十秒的网络抖动就整条丢掉——2026-08-06 09:00 真实发生。

    同一天 09:07 每日新闻的推送遇到同样的 `httpx.ConnectError`，
    日志写着 `retrying in 1s (attempt 1/3)`，重试后送达。
    同一个网络，有重试的成功、没重试的失败。
    """

    def _alert(self, results):
        """results: 每次调用的 returncode 序列。返回 (成功?, 说明, 实际调用次数)。"""
        from unittest import mock
        calls = []

        def fake_run(cmd, **kwargs):
            rc = results[len(calls)] if len(calls) < len(results) else 0
            calls.append(cmd)
            return mock.Mock(returncode=rc, stdout="", stderr="" if rc == 0 else "boom")

        with temp_home():
            with mock.patch.object(check_followup, "_hermes_bin",
                                   return_value="/usr/local/bin/hermes"), \
                 mock.patch.object(check_followup.subprocess, "run", side_effect=fake_run), \
                 mock.patch("time.sleep", lambda _s: None):
                ok, detail = check_followup.alert("测试内容", output_cfg())
        return ok, detail, len(calls)

    def test_first_attempt_success_does_not_retry(self):
        ok, detail, n = self._alert([0])
        self.assertTrue(ok)
        self.assertEqual(n, 1, "成功了就不该多打扰一次")
        self.assertEqual(detail, "ok")

    def test_transient_failure_is_retried_and_recovers(self):
        """今早那个场景：第一次撞上抖动，第二次就好了。"""
        ok, detail, n = self._alert([1, 0])
        self.assertTrue(ok)
        self.assertEqual(n, 2)
        self.assertIn("第2次成功", detail)

    def test_gives_up_after_three_and_reports_every_attempt(self):
        ok, detail, n = self._alert([1, 1, 1])
        self.assertFalse(ok)
        self.assertEqual(n, 3, "三次就收手，不能在 cron 里无限重试")
        for i in ("第1次", "第2次", "第3次"):
            self.assertIn(i, detail, "每次的原因都要留下，否则还是查不了")

    def test_retry_never_turns_failure_into_success(self):
        """0.3.0-rc1 的老护栏：发不出去就是发不出去，不许记成已通知。"""
        ok, _, _ = self._alert([1, 1, 1])
        self.assertFalse(ok)


class HermesBinTest(unittest.TestCase):
    def test_finds_hermes_via_which(self):
        import shutil
        from unittest import mock
        with mock.patch.object(shutil, "which", lambda n: "/usr/local/bin/hermes"):
            self.assertEqual(check_followup._hermes_bin(), "/usr/local/bin/hermes")

    def test_returns_none_when_absent(self):
        import shutil
        from unittest import mock
        with temp_home():  # HERMES_HOME 指向临时目录，回退路径也不会命中
            with mock.patch.object(shutil, "which", lambda n: None):
                self.assertIsNone(check_followup._hermes_bin())


if __name__ == "__main__":
    unittest.main()
