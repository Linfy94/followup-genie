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
import unittest
from datetime import datetime, timedelta
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
        text = open(watchdog.__file__, encoding="utf-8").read()
        bad = [ln for ln in text.splitlines()
               if ln.strip().startswith(("import core", "from core import",
                                         "import qqdoc", "from qqdoc import"))]
        self.assertEqual(bad, [],
                         f"watchdog 必须自给自足，不能依赖包内其他模块：{bad}")


if __name__ == "__main__":
    unittest.main()
