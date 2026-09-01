#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
2026-09-01：数据质量问题（_problems，比如 known_values 出现未知取值）
不再进企微推送，改走 telegram（复用 alert() 通道）。

═══════════════════════════════════════════════════════════════════════
业务反馈：GEO 那条冗长的"已知取值"枚举，一旦企微当天有别的台账要催，
就会跟着整段推给业务，演示/日常使用观感很差——而这些内容业务本来就
用不上，是给排查用的。

跟"停用节点"那条（见 test_render.py 的 DisabledNodeChannelTest）是
同一个模式：安全属性只是换通道，不是取消。企微收窄成纯业务清单；
终端 / --verbose / --json / doctor 四处原样保留，一个字都不少。

🔴 2026-09-01 外部审查指出的 P0：一刀切换通道会让"整列失效"这类真正
   会漏催的关键问题，在没配 telegram（典型是 WorkBuddy，没有 hermes）
   的场景下彻底没人知道——比换通道之前（好歹进企微）更差。补法：
   _problems() 的输出按 🔴 前缀分"关键"和"温和"两级（见 _is_loud()），
   telegram 发不出去时，关键问题会退到企微发一条简短摘要兜底；
   两个通道都没送达才把 exit_code 顶成 1。见下面 WeComFallbackForLoud
   ProblemsTest 这一组。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import io
import unittest
from datetime import date
from unittest import mock

from harness import check_followup, core  # noqa: F401 —— 挂 sys.path

TODAY = date(2026, 9, 1)


def _report_with_warning(warning="启动优化时间 出现未知取值 ['等立项']（已知：[...]）"):
    rep = core.Report({"id": "geo_wecom", "name": "GEO主台账（企微文档）",
                       "line": "GEO", "display_name": "GEO"})
    rep.warnings = [warning]
    return rep


class RenderWecomDropsDataQualityTest(unittest.TestCase):
    """镜像 test_render.py 的 DisabledNodeChannelTest：换通道，不是删除。"""

    def setUp(self):
        self.rep = _report_with_warning()
        self.md = check_followup.render_wecom([self.rep], TODAY, {})
        self.txt = check_followup.render([self.rep], TODAY, False, {})

    def test_the_fixture_really_has_a_warning(self):
        self.assertTrue(self.rep.warnings,
                        "fixture 没造出数据质量问题，后两条断言证明不了任何东西")

    def test_wecom_push_does_not_mention_it(self):
        self.assertNotIn("出现未知取值", self.md,
                         f"数据质量问题不该再推给业务：\n{self.md}")
        self.assertNotIn("需要注意", self.md,
                         f"整块「需要注意」标题也不该再出现：\n{self.md}")

    def test_terminal_output_still_announces_it(self):
        self.assertIn("出现未知取值", self.txt,
                      "终端仍要明说 —— 安全属性只是换通道，不是取消")


class NotifyDataQualityTest(unittest.TestCase):

    def test_sends_via_alert_when_real_run_and_has_problems(self):
        rep = _report_with_warning()
        with mock.patch.object(check_followup, "alert",
                               return_value=(True, "ok")) as m:
            check_followup._notify_data_quality([rep], {}, real_run=True)
        m.assert_called_once()
        text = m.call_args[0][0]
        self.assertIn("出现未知取值", text)
        self.assertIn("GEO主台账（企微文档）", text, "要能看出是哪张台账")

    def test_dry_run_does_not_call_alert(self):
        """🔒 试跑模式不真发——跟企微推送的拦截约定一致。"""
        rep = _report_with_warning()
        buf = io.StringIO()
        with mock.patch.object(check_followup, "alert") as m, \
             mock.patch("sys.stderr", buf):
            check_followup._notify_data_quality([rep], {}, real_run=False)
        m.assert_not_called()
        self.assertIn("已拦截", buf.getvalue())

    def test_no_problems_does_not_call_alert(self):
        """没有数据质量问题时，不该白发一条空消息。"""
        rep = core.Report({"id": "x", "name": "X台账", "line": "线"})
        with mock.patch.object(check_followup, "alert") as m:
            check_followup._notify_data_quality([rep], {}, real_run=True)
        m.assert_not_called()

    def test_alert_failure_is_reported_but_does_not_raise(self):
        rep = _report_with_warning()
        buf = io.StringIO()
        with mock.patch.object(check_followup, "alert",
                               return_value=(False, "hermes 找不到")), \
             mock.patch("sys.stderr", buf):
            check_followup._notify_data_quality([rep], {}, real_run=True)
        self.assertIn("hermes 找不到", buf.getvalue())

    def test_quiet_only_alert_failure_does_not_fall_back_to_wecom(self):
        """
        只有温和提示（没有 🔴 前缀）时，telegram 发不出去不设兜底——
        --verbose / doctor 里还看得到，不算数据丢失，犯不着为它去动
        企微那边的推送。
        """
        rep = _report_with_warning()  # 默认这条没有 🔴 前缀
        with mock.patch.object(check_followup, "alert",
                               return_value=(False, "hermes 找不到")):
            import wecom_push
            with mock.patch.object(wecom_push, "push") as wp:
                undelivered = check_followup._notify_data_quality(
                    [rep], {}, real_run=True)
        wp.assert_not_called()
        self.assertFalse(undelivered, "只是温和提示，不该顶 exit_code")


class WeComFallbackForLoudProblemsTest(unittest.TestCase):
    """🔴 前缀的关键问题：telegram 发不出去时必须退到企微兜底。"""

    def _loud_rep(self):
        return _report_with_warning(
            "🔴 需求提出时间 整列失效：需要这一列的 12 行没有一行能算出日期。"
        )

    def test_telegram_ok_means_no_wecom_fallback_needed(self):
        rep = self._loud_rep()
        with mock.patch.object(check_followup, "alert",
                               return_value=(True, "ok")):
            import wecom_push
            with mock.patch.object(wecom_push, "push") as wp:
                undelivered = check_followup._notify_data_quality(
                    [rep], {}, real_run=True)
        wp.assert_not_called()
        self.assertFalse(undelivered)

    def test_telegram_fails_falls_back_to_a_short_wecom_summary(self):
        rep = self._loud_rep()
        import wecom_push
        fake_result = wecom_push.PushResult(attempted=True, ok=True, sent=1, total=1)
        with mock.patch.object(check_followup, "alert",
                               return_value=(False, "hermes 找不到")), \
             mock.patch.object(wecom_push, "push",
                               return_value=fake_result) as wp:
            undelivered = check_followup._notify_data_quality(
                [rep], {}, real_run=True)
        wp.assert_called_once()
        summary_text = wp.call_args[0][0]
        self.assertIn("整列失效", summary_text, "关键问题的具体内容要能看到")
        self.assertNotIn("已知：", summary_text,
                         "兜底摘要不该带冗长枚举——那正是最初要挪走的东西")
        self.assertFalse(undelivered, "企微兜底送达了，不算彻底没通知到")

    def test_both_channels_fail_marks_undelivered_and_records_health(self):
        rep = self._loud_rep()
        import wecom_push
        fake_result = wecom_push.PushResult(attempted=True, ok=False, sent=0, total=1,
                                            errors=["企微那边也挂了"])
        with mock.patch.object(check_followup, "alert",
                               return_value=(False, "hermes 找不到")), \
             mock.patch.object(wecom_push, "push", return_value=fake_result), \
             mock.patch.object(core, "update_health") as uh:
            undelivered = check_followup._notify_data_quality(
                [rep], {}, real_run=True)
        self.assertTrue(undelivered,
                        "telegram 和企微兜底都没送达，必须报告给调用方")
        uh.assert_called_once()
        self.assertIn("last_failure", uh.call_args.kwargs)

    def test_dry_run_mentions_loud_count(self):
        rep = self._loud_rep()
        buf = io.StringIO()
        with mock.patch.object(check_followup, "alert") as m, \
             mock.patch("sys.stderr", buf):
            check_followup._notify_data_quality([rep], {}, real_run=False)
        m.assert_not_called()
        self.assertIn("1 条是关键问题", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
