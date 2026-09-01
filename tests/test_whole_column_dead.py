#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
「整列日期全废」必须和「几行没填对」长得不一样。

═══════════════════════════════════════════════════════════════════════
🔴 2026-08-14：飞书「立项时间」整列 731 行解析失败（列类型悄悄变成了带时区
   的 ISO 字符串），后果是 ④发货 拿不到计时起点，3 个项目掉进
   「既不催、也不终止」—— 而它报出来的样子和「3 行业务还没填」**一模一样**。

   这条警告当时会进企微推送（render_wecom 的「⚠️ 需要注意」那节），
   业务每天都收到，照样没人动。**所以问题从来不在通道，在措辞分不出轻重。**
   最后是业务随口问了句「那行警告是什么」才挖出来。

   这组用例钉的就是「分得出轻重」，不是「有没有报」。

   🔴 2026-09-01：数据质量问题已经不进企微了（业务反馈演示观感差，
      改走 telegram，见 check_followup._notify_data_quality）。
      但「分得出轻重」这条属性不能因为换了通道就丢——不管发到哪，
      「整列失效」都必须比「解析不出日期」更扎眼，下面 test_the_loud_
      wording_reaches_the_push 改成钉住新通道，不再钉旧的 render_wecom。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import unittest
from unittest import mock

from datetime import date

from harness import check_followup, ledgers_cfg, make_sheet, row, rules_cfg

import core


def _warnings(reported_values: list) -> list[str]:
    """
    造一批走 ②专家评估 的行（技术确认为空即命中），
    它们的计时列是「需求上报日期」—— 正是被检查的那一列。
    """
    rows = [row(i + 1, f"项目{i}", reported=v)
            for i, v in enumerate(reported_values)]
    sheet = make_sheet(rows)
    led = ledgers_cfg()["ledgers"][0]
    rs = rules_cfg()["rulesets"]["box"]
    return core.assert_sheet(sheet, led, rs).warnings


def _joined(vals):
    return "\n".join(_warnings(vals))


class WholeColumnTest(unittest.TestCase):

    BAD = "recvXXXX"          # 关联字段读出来的记录 ID，正是当天的形态

    def test_whole_column_dead_says_so_unmistakably(self):
        """🔴 整列全废 → 必须说「整列失效」和「一条都不会催」。"""
        said = _joined([self.BAD, self.BAD, self.BAD])
        self.assertIn("整列失效", said)
        self.assertIn("一条都不会催", said)

    def test_partial_failure_keeps_the_ordinary_wording(self):
        """
        只坏几行是数据质量问题，不是节点停摆。
        用整列那套措辞会制造狼来了 —— 而这个项目最怕的就是没人再看告警。
        """
        said = _joined([date(2026, 8, 1), date(2026, 8, 2), self.BAD])
        self.assertNotIn("整列失效", said)
        self.assertIn("解析不出日期", said)

    def test_healthy_column_says_nothing(self):
        said = _joined([date(2026, 8, 1), date(2026, 8, 2)])
        self.assertNotIn("整列失效", said)
        self.assertNotIn("解析不出日期", said)

    def test_all_blank_is_not_whole_column_dead(self):
        """
        🔴 全空 ≠ 整列失效。全空多半是业务还没填（完全正常的状态），
           而整列失效是列类型变了。混为一谈会天天误报一条最刺眼的警告。
        """
        said = _joined(["", "", ""])
        self.assertNotIn("整列失效", said)

    def test_the_loud_wording_reaches_the_push(self):
        """
        它走的是 rep.warnings → _problems() → _notify_data_quality()。
        这条钉住那条链路不被改断——「整列失效」这种最扎眼的措辞，
        必须能一路传到发往 telegram 的文本里，不能半路被压成安静的
        --verbose-only 提示。
        """
        rep = core.Report({"id": "box", "name": "AI节能盒子项目登记册",
                           "line": "盒子"})
        rep.warnings = _warnings([self.BAD, self.BAD, self.BAD])
        self.assertTrue(rep.warnings, "fixture 没造出警告，下面的断言证明不了什么")

        with mock.patch.object(check_followup, "alert",
                               return_value=(True, "ok")) as m:
            check_followup._notify_data_quality([rep], {}, real_run=True)
        m.assert_called_once()
        sent_text = m.call_args[0][0]
        self.assertIn("整列失效", sent_text)


if __name__ == "__main__":
    unittest.main()
