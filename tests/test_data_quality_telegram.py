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


if __name__ == "__main__":
    unittest.main()
