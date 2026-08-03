#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
外部存活监控。

═══════════════════════════════════════════════════════════════════════
这一组守的是**最隐蔽的那种失败：任务根本没跑。**

关机、休眠、gateway 没起来、cron 被误删 —— 这几种失败连一行 stderr
都不会产生：没有进程、没有日志、没有异常。业务只会觉得「最近很安静」，
而那和「最近确实没有要催的」长得一模一样。

判定用「数错过了几次本该执行的 9:00」而不是小时阈值，
因为这台机器工作日不关机、周末可能关机 ——
小时阈值要么让周四的故障潜伏到周末，要么在周一早上误报。
下面的 WeekendTest 就是拿真实作息在验这件事。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import subprocess
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from harness import temp_home

import watchdog  # noqa: E402 —— 必须在 harness 之后


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def health(last_ok: str | None = None, **extra) -> dict:
    h = dict(extra)
    if last_ok:
        h["last_full_success"] = last_ok
    return h


class MissedRunsTest(unittest.TestCase):
    """日历算术。周末跳过是这套判定的核心。"""

    def _n(self, last_ok: str, now: str, weekends=False) -> int:
        return watchdog.missed_runs(dt(last_ok), dt(now), 9, weekends)

    def test_same_day_after_success(self):
        self.assertEqual(
            self._n("2026-07-29T09:00:12+08:00", "2026-07-29T15:00:00+08:00"), 0)

    def test_next_day_before_nine(self):
        self.assertEqual(
            self._n("2026-07-29T09:00:12+08:00", "2026-07-30T08:30:00+08:00"), 0,
            "还没到 9:00，不算错过")

    def test_next_day_after_nine(self):
        self.assertEqual(
            self._n("2026-07-29T09:00:12+08:00", "2026-07-30T10:30:00+08:00"), 1)

    def test_weekend_is_skipped(self):
        # 2026-07-31 周五 → 2026-08-03 周一
        self.assertEqual(
            self._n("2026-07-31T09:00:12+08:00", "2026-08-03T10:30:00+08:00"), 1,
            "周六日不该计入，只有周一那一班算错过")

    def test_weekend_counted_when_configured(self):
        self.assertEqual(
            self._n("2026-07-31T09:00:12+08:00", "2026-08-03T10:30:00+08:00",
                    weekends=True), 3)

    def test_future_last_ok(self):
        self.assertEqual(
            self._n("2026-08-10T09:00:00+08:00", "2026-08-01T10:00:00+08:00"), 0,
            "时钟乱了也不该算出负数或爆炸")


class WeekendTest(unittest.TestCase):
    """
    🔴 用真实作息走一遍：工作日不关机、周末可能关机。

    这是当初选「数班次」而不是「数小时」的全部理由，必须测住。
    """

    CFG = dict(watchdog.DEFAULTS)

    def _alert(self, last_ok: str, now: str) -> bool:
        need, _, _ = watchdog.judge(health(last_ok), {}, self.CFG, dt(now))
        return need

    def test_friday_ok_weekend_off_monday_caught_up(self):
        """周五跑完 → 周末关机 → 周一 9:00 补跑成功 → 周一 10:30 检查。"""
        self.assertFalse(self._alert("2026-08-03T09:00:12+08:00",
                                     "2026-08-03T10:30:00+08:00"),
                         "🔴 这正是要避免的周一误报")

    def test_friday_ok_monday_failed_not_yet_alerted(self):
        """周一挂了但只错过 1 次 —— 容一次抖动，先不吵。"""
        self.assertFalse(self._alert("2026-07-31T09:00:12+08:00",
                                     "2026-08-03T10:30:00+08:00"))

    def test_friday_ok_still_broken_on_tuesday_alerts(self):
        self.assertTrue(self._alert("2026-07-31T09:00:12+08:00",
                                    "2026-08-04T10:30:00+08:00"),
                        "错过周一周二两班，该报了")

    def test_midweek_failure_alerts_on_the_second_day(self):
        # 周三成功 → 周四挂 → 周五 10:30
        self.assertTrue(self._alert("2026-07-29T09:00:12+08:00",
                                    "2026-07-31T10:30:00+08:00"))

    def test_absolute_cap_catches_a_miscounted_calendar(self):
        """
        绝对上限兜的不是「假期太长」——正常一周里必然有 5 个工作日，
        班次规则先就触发了。它兜的是**班次逻辑本身被配错或算错**：
        比如有人把 missed_runs_before_alert 设成了一个大得离谱的值。

        没有这条兜底，配错一个数字就等于把整个监控静音，而且没人会发现。
        """
        cfg = dict(self.CFG, missed_runs_before_alert=999)
        need, key, why = watchdog.judge(
            health("2026-07-20T09:00:00+08:00"), {}, cfg,
            dt("2026-07-29T10:00:00+08:00"))
        self.assertTrue(need, "班次规则被配哑了，绝对上限必须接住")
        self.assertTrue(key.startswith("absolute:"))
        self.assertIn("兜底", why)

    def test_no_alert_before_the_absolute_cap(self):
        cfg = dict(self.CFG, missed_runs_before_alert=999)
        need, _, _ = watchdog.judge(
            health("2026-07-28T09:00:00+08:00"), {}, cfg,
            dt("2026-07-30T10:00:00+08:00"))
        self.assertFalse(need, "才两天，兜底不该乱响")


class MissingHealthTest(unittest.TestCase):

    def test_grace_period_on_first_sight(self):
        need, key, why = watchdog.judge({}, {}, watchdog.DEFAULTS,
                                        dt("2026-08-01T10:00:00+08:00"))
        self.assertFalse(need, "刚装完还没跑过，不该马上吵")
        self.assertEqual(key, "missing")

    def test_alerts_after_grace(self):
        st = {"first_seen_missing": "2026-07-30T10:00:00+08:00"}
        need, _, why = watchdog.judge({}, st, watchdog.DEFAULTS,
                                      dt("2026-08-01T10:00:00+08:00"))
        self.assertTrue(need, "36 小时还没有健康记录，说明压根没跑起来")
        self.assertIn("从来没有过一次成功运行", why)

    def test_health_exists_but_never_succeeded(self):
        h = {"last_failure": {"stage": "取数", "reason": "凭证失效"}}
        need, key, why = watchdog.judge(h, {}, watchdog.DEFAULTS,
                                        dt("2026-08-01T10:00:00+08:00"))
        self.assertTrue(need)
        self.assertIn("凭证失效", why)

    def test_malformed_last_failure_does_not_crash_the_monitor(self):
        need, key, why = watchdog.judge(
            {"last_failure": "损坏的字段"}, {}, watchdog.DEFAULTS,
            dt("2026-08-01T10:00:00+08:00"))
        self.assertTrue(need)
        self.assertEqual(key, "never-succeeded")
        self.assertIn("无法读取", why)

    def test_future_success_timestamp_alerts_instead_of_hiding_failures(self):
        need, key, why = watchdog.judge(
            health("2026-08-10T09:00:00+08:00"), {}, watchdog.DEFAULTS,
            dt("2026-08-01T10:00:00+08:00"))
        self.assertTrue(need)
        self.assertEqual(key, "future-success:2026-08-10T09:00:00+08:00")
        self.assertIn("未来", why)

    def test_corrupt_health_is_treated_as_missing(self):
        """health.json 坏掉时 read_json 返回 {} —— 等同于没成功过，要报。"""
        with temp_home(state={"health.json": "{ 坏 "}):
            self.assertEqual(watchdog.read_json(
                watchdog.state_dir() / "health.json"), {})


class DedupeTest(unittest.TestCase):
    """一天查两次却报两次，人两天就把它静音了。"""

    def test_same_key_within_window_is_suppressed(self):
        with temp_home(state={
            "health.json": health("2026-07-27T09:00:00+08:00"),
            "watchdog_state.json": {
                "last_alert_at": "2026-08-01T09:00:00+08:00",
                "last_alert_key": "stale:2026-07-27T09:00:00+08:00"},
        }):
            with mock.patch.object(watchdog, "send_alert") as send:
                with mock.patch.object(watchdog, "datetime") as m:
                    m.now.return_value = dt("2026-08-01T15:00:00+08:00")
                    m.combine = datetime.combine
                    m.fromisoformat = datetime.fromisoformat
                    watchdog.main([])
                send.assert_not_called()

    def test_different_key_alerts_again(self):
        with temp_home(state={
            "health.json": health("2026-07-27T09:00:00+08:00"),
            "watchdog_state.json": {
                "last_alert_at": "2026-08-01T09:00:00+08:00",
                "last_alert_key": "别的故障"},
        }):
            with mock.patch.object(watchdog, "send_alert",
                                   return_value=(True, "stub")) as send:
                watchdog.main([])
                send.assert_called_once()

    def test_future_dedupe_timestamp_does_not_suppress_alerts(self):
        """时钟回拨或状态损坏不能让同一故障一直被当成刚告警过。"""
        with temp_home(state={
            "health.json": health("2026-07-27T09:00:00+08:00"),
            "watchdog_state.json": {
                "last_alert_at": "2026-09-01T09:00:00+08:00",
                "last_alert_key": "stale:2026-07-27T09:00:00+08:00"},
        }):
            with mock.patch.object(watchdog, "send_alert",
                                   return_value=(True, "stub")) as send, \
                 mock.patch.object(watchdog, "datetime") as m:
                m.now.return_value = dt("2026-08-03T15:00:00+08:00")
                m.combine = datetime.combine
                m.fromisoformat = datetime.fromisoformat
                watchdog.main([])
                send.assert_called_once()


class WatchdogStateCorruptionTest(unittest.TestCase):

    def test_non_numeric_counters_are_reset_instead_of_crashing(self):
        with temp_home(state={
            "health.json": health("2026-07-01T09:00:00+08:00"),
            "watchdog_state.json": {"checks": "很多", "alert_failures": "很多"},
        }) as home:
            with mock.patch.object(watchdog, "send_alert",
                                   return_value=(False, "stub")):
                code = watchdog.main([])

            self.assertEqual(code, 1)
            state = json.loads((home / "followup" / "state" / "watchdog_state.json")
                               .read_text(encoding="utf-8"))
            self.assertEqual(state["checks"], 1)
            self.assertEqual(state["alert_failures"], 1)


class AlertChainTest(unittest.TestCase):
    """三级降级：hermes send → 本机通知 → 日志 + 非零退出。"""

    def test_falls_back_to_local_notification(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv[0])
            class R:
                returncode = 3 if argv[0] != "osascript" else 0
                stdout = ""
                stderr = "boom"
            return R()

        with temp_home():
            with mock.patch.object(watchdog, "_hermes_bin", lambda: "/fake/hermes"), \
                 mock.patch.object(watchdog.subprocess, "run", fake_run), \
                 mock.patch.object(watchdog.sys, "platform", "darwin"):
                ok, how = send_ok = watchdog.send_alert(
                    "测试", log=watchdog.state_dir() / "watchdog.log")
            self.assertTrue(ok)
            self.assertIn("本机通知", how)
            self.assertIn("osascript", calls)

    def test_writes_log_and_fails_when_everything_fails(self):
        with temp_home() as home:
            log = watchdog.state_dir() / "watchdog.log"
            with mock.patch.object(watchdog, "_hermes_bin", lambda: None), \
                 mock.patch.object(watchdog.sys, "platform", "linux"):
                ok, how = watchdog.send_alert("出事了", log=log)
            self.assertFalse(ok)
            self.assertTrue(log.exists(), "一条都发不出去时至少要留下痕迹")
            self.assertIn("出事了", log.read_text(encoding="utf-8"))

    def test_never_prints_the_alert_target(self):
        """告警目标是配置不是秘密，但也没有理由打到屏幕上。"""
        with temp_home():
            with mock.patch.object(watchdog, "_hermes_bin", lambda: None), \
                 mock.patch.object(watchdog.sys, "platform", "linux"):
                ok, how = watchdog.send_alert(
                    "x", log=watchdog.state_dir() / "watchdog.log")
            self.assertNotIn("telegram:000000", how)


class DryRunTest(unittest.TestCase):

    def test_dry_run_sends_nothing_writes_nothing(self):
        with temp_home(state={"health.json": health("2026-07-01T09:00:00+08:00")}) as home:
            sd = home / "followup" / "state"
            before = {p.name for p in sd.iterdir()}
            with mock.patch.object(watchdog, "send_alert") as send:
                code = watchdog.main(["--dry-run"])
            self.assertEqual(code, 0)
            send.assert_not_called()
            self.assertEqual({p.name for p in sd.iterdir()}, before,
                             "--dry-run 不许写 watchdog_state.json")

    def test_disabled_by_config(self):
        from harness import output_cfg
        cfg = output_cfg(watchdog={"enabled": False})
        with temp_home(output=cfg):
            with mock.patch.object(watchdog, "send_alert") as send:
                self.assertEqual(watchdog.main([]), 0)
                send.assert_not_called()


class AlertFailureMustRetryTest(unittest.TestCase):
    """
    🔴 0.3.0-rc2 修的：**告警失败不得进入去重。**

    旧实现无条件写 last_alert_at / last_alert_key，于是：
      告警发失败 → 照样记进 24 小时去重 → 下次检查「已告警过，不重复」
      → 再下次还是跳过 …… 故障一直在，告警**永远发不出去**。

    监控器最不能有的就是这种失败：它自己哑了，还以为自己在响。
    """

    STATE = {"health.json": health("2026-07-01T09:00:00+08:00")}

    def test_failed_alert_does_not_write_dedup_key(self):
        with temp_home(state=dict(self.STATE)) as home:
            with mock.patch.object(watchdog, "send_alert",
                                   return_value=(False, "全都失败了")):
                code = watchdog.main([])
            self.assertEqual(code, 1, "告警发不出去必须非零退出")

            st = json.loads((home / "followup" / "state" / "watchdog_state.json")
                            .read_text(encoding="utf-8"))
            self.assertNotIn("last_alert_at", st, "🔴 失败的告警写了去重时间")
            self.assertNotIn("last_alert_key", st, "🔴 失败的告警写了去重键")
            self.assertFalse(st.get("last_alert_ok"))
            self.assertIn("last_alert_failed_at", st, "失败要留诊断痕迹")
            self.assertEqual(st.get("alert_failures"), 1)

    def test_next_check_actually_retries(self):
        """真正要证明的是这条：下一次检查**还会再发一遍**。"""
        with temp_home(state=dict(self.STATE)):
            with mock.patch.object(watchdog, "send_alert",
                                   return_value=(False, "失败")) as send:
                watchdog.main([])
                self.assertEqual(send.call_count, 1)
                watchdog.main([])
                self.assertEqual(send.call_count, 2, "🔴 第二次被去重吃掉了")
                watchdog.main([])
                self.assertEqual(send.call_count, 3, "🔴 第三次被去重吃掉了")

    def test_failures_accumulate(self):
        with temp_home(state=dict(self.STATE)) as home:
            with mock.patch.object(watchdog, "send_alert",
                                   return_value=(False, "失败")):
                watchdog.main([])
                watchdog.main([])
            st = json.loads((home / "followup" / "state" / "watchdog_state.json")
                            .read_text(encoding="utf-8"))
            self.assertEqual(st.get("alert_failures"), 2)

    def test_success_still_dedups(self):
        """自愈不能矫枉过正：发成功了就该正常去重，别一天响两次。"""
        with temp_home(state=dict(self.STATE)) as home:
            with mock.patch.object(watchdog, "send_alert",
                                   return_value=(True, "hermes send")) as send:
                watchdog.main([])
                watchdog.main([])
                self.assertEqual(send.call_count, 1, "成功后 24 小时内不该重发")

            st = json.loads((home / "followup" / "state" / "watchdog_state.json")
                            .read_text(encoding="utf-8"))
            self.assertIn("last_alert_at", st)
            self.assertTrue(st.get("last_alert_ok"))

    def test_recovery_clears_failure_counters(self):
        with temp_home(state=dict(self.STATE)) as home:
            with mock.patch.object(watchdog, "send_alert",
                                   return_value=(False, "失败")):
                watchdog.main([])
            with mock.patch.object(watchdog, "send_alert",
                                   return_value=(True, "hermes send")):
                watchdog.main([])
            st = json.loads((home / "followup" / "state" / "watchdog_state.json")
                            .read_text(encoding="utf-8"))
            self.assertNotIn("alert_failures", st, "发成功了就该把失败计数清掉")
            self.assertNotIn("last_alert_failed_at", st)


class LocalNotificationFailureTest(unittest.TestCase):
    """
    🔴 0.3.0-rc2 修的：**osascript 非零必须判定为失败。**

    旧实现 `check=False` 之后无条件 return True，退出码根本没看。
    而 osascript 失败是常事 —— launchd 里没有 GUI 会话、通知权限被拒、
    被 MDM 限制，全都返回非零。结果三级降级里的第三级永远走不到：
    watchdog 报「已告警」并退 0，而那条告警根本没发出去。
    """

    def _run(self, osa_returncode, home):
        """让一级必失败，二级由参数决定，看 send_alert 怎么判。"""
        with mock.patch.object(watchdog, "_hermes_bin", return_value=None), \
             mock.patch.object(watchdog.sys, "platform", "darwin"), \
             mock.patch.object(watchdog.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=["osascript"], returncode=osa_returncode,
                stdout="", stderr="执行 AppleScript 失败" if osa_returncode else "")
            return watchdog.send_alert("测试告警\n第二行",
                                       log=home / "watchdog.log")

    def test_nonzero_osascript_is_a_failure(self):
        with temp_home() as home:
            ok, how = self._run(1, home)
            self.assertFalse(ok, "🔴 osascript 退出码 1 仍被当成告警成功")
            self.assertIn("osascript 退出码 1", how)

    def test_nonzero_osascript_falls_through_to_the_log(self):
        """第三级必须真的走到 —— 至少在日志里留下痕迹。"""
        with temp_home() as home:
            log = home / "watchdog.log"
            with mock.patch.object(watchdog, "_hermes_bin", return_value=None), \
                 mock.patch.object(watchdog.sys, "platform", "darwin"), \
                 mock.patch.object(watchdog.subprocess, "run") as run:
                run.return_value = subprocess.CompletedProcess(
                    args=["osascript"], returncode=1, stdout="", stderr="没有 GUI 会话")
                ok, _ = watchdog.send_alert("取数失败", log=log)
            self.assertFalse(ok)
            self.assertTrue(log.exists(), "🔴 三级降级没写日志")
            self.assertIn("取数失败", log.read_text(encoding="utf-8"))

    def test_zero_osascript_is_still_a_success(self):
        with temp_home() as home:
            ok, how = self._run(0, home)
            self.assertTrue(ok)
            self.assertIn("本机通知", how)

    def test_main_exits_nonzero_when_everything_fails(self):
        with temp_home(state={"health.json":
                              health("2026-07-01T09:00:00+08:00")}) as home:
            with mock.patch.object(watchdog, "_hermes_bin", return_value=None), \
                 mock.patch.object(watchdog.sys, "platform", "darwin"), \
                 mock.patch.object(watchdog.subprocess, "run") as run:
                run.return_value = subprocess.CompletedProcess(
                    args=["osascript"], returncode=1, stdout="", stderr="拒绝")
                self.assertEqual(watchdog.main([]), 1)


class ConfigValidationTest(unittest.TestCase):
    """
    🔴 0.3.0-rc2 修的：**配置写错不得让监控器崩掉。**

    旧实现在使用点写 `int(cfg.get("schedule_hour") or 9)`：
      "九" → ValueError 当场裸崩
      25   → 更隐蔽，要等到 time(25, 0) 那一行才炸

    监控器崩掉 = 完全没有监控，而崩的原因只是一个手滑打错的数字。
    口径：**坏值退默认 + 人话警告，绝不罢工。**
    """

    def test_good_config_is_kept(self):
        cfg, warns = watchdog.validate_config(
            {"schedule_hour": 8, "count_weekends": True,
             "missed_runs_before_alert": 3})
        self.assertEqual(warns, [])
        self.assertEqual(cfg["schedule_hour"], 8)
        self.assertTrue(cfg["count_weekends"])
        self.assertEqual(cfg["missed_runs_before_alert"], 3)

    def test_underscore_comments_are_not_warned_about(self):
        _, warns = watchdog.validate_config({"_说明": "这是注释", "schedule_hour": 9})
        self.assertEqual(warns, [])

    def test_bad_values_fall_back_with_a_readable_warning(self):
        cases = [
            ("schedule_hour", "九", "应该是数字"),
            ("schedule_hour", 25, "应该在 0–23 之间"),
            ("schedule_hour", -1, "应该在 0–23 之间"),
            ("schedule_hour", 9.5, "应该是整数"),
            ("schedule_hour", None, "应该是数字"),
            ("missed_runs_before_alert", 0, "应该在 1–1000 之间"),
            ("missed_runs_before_alert", -5, "应该在 1–1000 之间"),
            ("absolute_max_hours", 0, "应该在"),
            ("repeat_alert_hours", "一天", "应该是数字"),
            ("enabled", "false", "应该是 true 或 false"),
            ("enabled", 1, "应该是 true 或 false"),
            ("count_weekends", "yes", "应该是 true 或 false"),
        ]
        for key, bad, expect in cases:
            with self.subTest(key=key, bad=bad):
                cfg, warns = watchdog.validate_config({key: bad})
                self.assertEqual(cfg[key], watchdog.DEFAULTS[key],
                                 f"{key}={bad!r} 应该退回默认值")
                self.assertEqual(len(warns), 1, f"{key}={bad!r} 应该正好一条警告")
                self.assertIn(expect, warns[0])
                self.assertIn(key, warns[0], "警告里要说清是哪个字段")

    def test_enabled_string_false_does_not_silently_disable(self):
        """
        "false" 这个字符串用 bool() 转会变成 True。
        配置写错时，「关掉监控」和「打开监控」是反的 —— 必须报出来。
        """
        cfg, warns = watchdog.validate_config({"enabled": "false"})
        self.assertTrue(cfg["enabled"], "坏值应退回默认（开启），不能被静默关掉")
        self.assertTrue(warns)

    def test_unknown_key_is_reported_not_crashed(self):
        cfg, warns = watchdog.validate_config({"schedul_hour": 8})
        self.assertEqual(cfg, watchdog.DEFAULTS)
        self.assertIn("不是可识别的配置项", warns[0])

    def test_whole_segment_wrong_type(self):
        for bad in ([], "开", 42, True):
            with self.subTest(bad=bad):
                cfg, warns = watchdog.validate_config(bad)
                self.assertEqual(cfg, watchdog.DEFAULTS)
                self.assertEqual(len(warns), 1)
                self.assertIn("应该是一个对象", warns[0])

    def test_missing_segment_is_not_a_warning(self):
        cfg, warns = watchdog.validate_config(None)
        self.assertEqual(cfg, watchdog.DEFAULTS)
        self.assertEqual(warns, [])

    def test_broken_output_json_does_not_crash(self):
        for content in ("{ 这不是 JSON", "[]", "null", '"字符串"'):
            with self.subTest(content=content):
                with temp_home() as home:
                    (home / "followup" / "config" / "output.json").write_text(
                        content, encoding="utf-8")
                    cfg, warns = watchdog.load_config()
                    self.assertEqual(cfg, watchdog.DEFAULTS)
                    self.assertTrue(warns, "坏配置要有警告，不能悄悄用默认值")

    def test_main_survives_a_broken_config(self):
        """端到端：配置全是坏值，watchdog 照样跑完并给出判定。"""
        from harness import output_cfg
        cfg = output_cfg(watchdog={"schedule_hour": "九",
                                   "missed_runs_before_alert": -1,
                                   "enabled": "yes"})
        with temp_home(output=cfg,
                       state={"health.json": health("2026-07-01T09:00:00+08:00")}):
            with mock.patch.object(watchdog, "send_alert",
                                   return_value=(True, "hermes send")) as send:
                code = watchdog.main([])       # 旧实现在这里 ValueError 裸崩
            self.assertIn(code, (0, 1))
            send.assert_called_once()

    def test_judge_never_coerces_at_use_site(self):
        """判定时不该再做类型转换 —— 那正是旧实现裸崩的位置。"""
        cfg, _ = watchdog.validate_config({"schedule_hour": "九"})
        need, key, why = watchdog.judge(
            health("2026-07-01T09:00:00+08:00"), {}, cfg,
            dt("2026-07-30T10:30:00+08:00"))
        self.assertTrue(need)


class RuntimeHomeTest(unittest.TestCase):
    """
    watchdog 刻意不 import core，所以路径解析是**第二份实现**。
    两份不一致的话，监控器看的就不是主任务写的那个 health.json ——
    它会永远报「从来没跑过」，或者永远报「一切正常」。
    """

    def test_matches_core(self):
        import core
        import os as _os
        for fh, hh in (("/tmp/a", "/tmp/b"), (None, "/tmp/b"),
                       ("/tmp/a", None), (None, None)):
            with self.subTest(FOLLOWUP_HOME=fh, HERMES_HOME=hh):
                old = {k: _os.environ.get(k)
                       for k in ("FOLLOWUP_HOME", "HERMES_HOME")}
                try:
                    for k, v in (("FOLLOWUP_HOME", fh), ("HERMES_HOME", hh)):
                        _os.environ.pop(k, None)
                        if v:
                            _os.environ[k] = v
                    self.assertEqual(watchdog.runtime_home(), core.hermes_home())
                finally:
                    for k, v in old.items():
                        _os.environ.pop(k, None)
                        if v is not None:
                            _os.environ[k] = v

    def test_does_not_import_core(self):
        """
        这条不是洁癖：core 坏了正是主任务会失败的场景，
        监控器跟着 import 失败就等于没有监控器。

        只看真正的 import 语句 —— 文件头的注释里就写着「刻意不 import core」，
        全文搜字符串会搜到那句说明。
        """
        # 用 read_text 而不是 open().read()：后者不关文件，
        # 在 -W error::ResourceWarning 下会直接把测试跑红。
        text = Path(watchdog.__file__).read_text(encoding="utf-8")
        bad = [ln for ln in text.splitlines()
               if ln.strip().startswith(("import core", "from core import",
                                         "import qqdoc", "from qqdoc import"))]
        self.assertEqual(bad, [],
                         f"watchdog 必须自给自足，不能依赖包内其他模块：{bad}")


if __name__ == "__main__":
    unittest.main()
