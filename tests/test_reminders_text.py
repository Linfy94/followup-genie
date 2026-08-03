#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
macOS 提醒事项的文案。

═══════════════════════════════════════════════════════════════════════
业务 2026-08-03 给的模板，并明确要求「不要太长」：

    ⭕️AI外贸拓客「客户填表」深圳市永新能科技有限公司
    超期（ ）天
    该做什么：提醒客户提交需求

（括号是填空占位，实际写「超期 3 天」，与企微清单同一把尺子。）

🔴 这个模板顺带修掉一个真实缺陷：`_upsert()` 靠**标题精确匹配**认回旧提醒
   （AppleScript 里 `if name of r is "<标题>"`），而改版前的标题**含超期天数**。
   天数每天涨 → 标题每天变 → 匹配不上 → **每天给同一个项目再建一条提醒**。
   28 条 × 每天，两周后这个列表就没法用了。

   新模板把天数移到第二行（备注），标题因此成为稳定的键。

   这个缺陷一直没暴露，因为提醒事项文案此前**零测试覆盖**，
   且开发机上 `reminders.write` 一直是 false，从未真实执行过。
   本文件就是补上那个缺口。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import re
import unittest
from datetime import date

from harness import core  # noqa: F401 —— 挂 sys.path

import reminders_sync  # noqa: E402


def item(*, name="深业集团有限公司", stage="节能测试", line_label="AI节能盒子",
         stalled=179, allowance=21, action="催节能测试出报告"):
    return core.Item(
        ledger_id="box", line="盒子", key="3", name=name,
        node_id="efficiency_test", node_name="④节能测试",
        stalled_days=stalled, clock_from=date(2026, 2, 9),
        clock_source="stage_entered", action=action,
        line_label=line_label, stage=stage, allowance=allowance,
    )


class TitleFormatTest(unittest.TestCase):
    """标题严格照业务给的模板。"""

    def test_matches_the_template(self):
        self.assertEqual(
            reminders_sync._title(item()),
            "⭕️AI节能盒子「节能测试」深业集团有限公司")

    def test_another_line_and_stage(self):
        """换业务线、换阶段，形状不变 —— 业务举的例子就是外贸拓客那条线。"""
        self.assertEqual(
            reminders_sync._title(item(
                line_label="AI外贸拓客", stage="客户填表",
                name="深圳市永新能科技有限公司")),
            "⭕️AI外贸拓客「客户填表」深圳市永新能科技有限公司")

    def test_stage_with_a_slash_survives(self):
        """「预调试/安装」带斜杠，别被当成路径或分隔符处理掉。"""
        self.assertEqual(
            reminders_sync._title(item(stage="预调试/安装")),
            "⭕️AI节能盒子「预调试/安装」深业集团有限公司")

    def test_starts_with_the_marker(self):
        self.assertTrue(reminders_sync._title(item()).startswith(reminders_sync.MARKER))


class TitleMustBeStableTest(unittest.TestCase):
    """
    🔴 本文件最重要的一组：标题是 `_upsert()` 认回旧提醒的唯一匹配键。

    标题一旦随时间变化，每天都会新建一条提醒而不是更新原来那条。
    这不会报错、不会告警，只会让业务的提醒事项列表在两周内塞满重复项。
    """

    def test_title_contains_no_day_count(self):
        title = reminders_sync._title(item(stalled=179))
        self.assertNotIn("179", title)
        self.assertNotIn("158", title, "超期天数也不许出现")
        self.assertIsNone(
            re.search(r"\d+\s*天", title),
            f"🔴 标题里出现了天数：{title!r} —— 它每天都会变，"
            f"于是每天新建一条提醒而不是更新原来那条")

    def test_same_project_same_title_on_different_days(self):
        """
        直接验幂等前提：同一个 (项目, 节点)，在超期 3 天和超期 158 天时，
        标题必须**完全相同**。
        """
        day3 = reminders_sync._title(item(stalled=24))    # 允许 21 → 超期 3
        day158 = reminders_sync._title(item(stalled=179))  # 允许 21 → 超期 158
        self.assertEqual(day3, day158,
                         "🔴 标题随天数变了 —— _upsert 会认不出旧提醒，天天重建")

    def test_body_is_what_carries_the_changing_number(self):
        """天数必须落在备注里 —— 它是每天被刷新的那部分。"""
        self.assertIn("超期 3 天", reminders_sync._body(item(stalled=24)))
        self.assertIn("超期 158 天", reminders_sync._body(item(stalled=179)))


class BodyFormatTest(unittest.TestCase):
    """备注严格两行 —— 业务明确要求「不要太长」。"""

    def test_exactly_two_lines(self):
        body = reminders_sync._body(item())
        self.assertEqual(body, "超期 158 天\n该做什么：催节能测试出报告")

    def test_no_action_means_one_line(self):
        """没配 action 的节点不该留一个空的「该做什么：」。"""
        body = reminders_sync._body(item(action=""))
        self.assertEqual(body, "超期 158 天")
        self.assertNotIn("该做什么", body)

    def test_diagnostics_are_gone(self):
        """
        业务定的是两行。起点、来源、允许天数、台账序号、生成日期全部不再进备注。
        （排查时这些仍可从 `--json` 拿到，那才是它们该在的地方。）
        """
        body = reminders_sync._body(item())
        for gone in ("起点", "来源", "允许", "台账序号", "生成于", "在本节点"):
            self.assertNotIn(gone, body, f"备注里不该还有「{gone}」")

    def test_day_count_uses_plain_spacing(self):
        """业务模板写的「超期（ ）天」里括号是填空占位，不是字面量。"""
        body = reminders_sync._body(item(stalled=24))
        self.assertIn("超期 3 天", body)
        self.assertNotIn("（3）", body)
        self.assertNotIn("()", body)


class DryRunUsesNewTitleTest(unittest.TestCase):
    """演练模式打印的必须是真会写进去的那个标题，否则验收看的是假的。"""

    def test_dry_run_prints_the_new_title(self):
        import io
        buf = io.StringIO()
        reminders_sync.sync([item()],
                            {"reminders": {"write": False, "list_name": "测试列表"}},
                            date(2026, 8, 7), stream=buf)
        out = buf.getvalue()
        self.assertIn("⭕️AI节能盒子「节能测试」深业集团有限公司", out)
        self.assertIn("演练模式", out)
        self.assertIn("未调用任何 osascript", out)


if __name__ == "__main__":
    unittest.main()
