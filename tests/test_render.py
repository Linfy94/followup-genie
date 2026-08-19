#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
输出格式（业务 2026-07-31 定稿）。

  · 组内按**超期天数**升序 —— 短的在前
    （超期天数 = 在本节点的天数 − 允许天数；待收资允许 6 天、预调试/安装允许 21 天）
  · 组内编号 1、2、3
  · 只在【待收资】插「超2个月 可考虑终止」分割线，插在第一个**超期** ≥60 天之前
  · 不带台账序号、不带阶段描述文案
  · 企微侧只做标题分级；条目一律不加样式

两套渲染共用 group_items()，所以这里重点测**两边必须一致** ——
分开各写一遍的话，迟早只改了企微那边、终端那边还是旧格式。
"""

from __future__ import annotations

import re
import unittest
from datetime import date, timedelta

from harness import (check_followup, core, make_sheet, row, temp_home,
                     run_main, output_cfg, ledgers_cfg, rules_cfg, wecom_push)

TODAY = date(2026, 7, 20)


def collect_rows(days: list[int], tech="待收资", **kw):
    """造一批卡在同一节点、停留天数各异的项目。days 是**在节点的天数**，不是超期天数。"""
    return make_sheet([
        row(i + 1, f"公司{d}天", tech=tech,
            reported=TODAY - timedelta(days=d),
            progress=TODAY - timedelta(days=d), **kw)
        for i, d in enumerate(days)
    ])


def render_both(sheet, cfg=None):
    """跑一次判定，拿到两套渲染的文本。不发不写。"""
    with temp_home(output=cfg or output_cfg()):
        r = run_main([f"--today={TODAY}", "--dry-run"], sheet)
        assert r.code == 0, r.err
        term = r.out
    # 企微那份要用同一批 report 重渲染，走 push 的拦截日志拿不到，
    # 所以直接调渲染函数（下面 WecomRenderTest 用 _reports 辅助）
    return term


def _reports(sheet, cfg):
    """跑判定但不走 main，拿到 Report 对象，供直接调用两套渲染。"""
    import qqdoc
    from unittest import mock
    led = ledgers_cfg()["ledgers"][0]
    rules = rules_cfg()
    wd = core.WorkdayCalc(rules["workday"], None)
    with mock.patch.object(qqdoc, "read_sheet", lambda *a: sheet):
        rep, _ = core.evaluate_ledger(led, rules["rulesets"]["box"], wd,
                                      TODAY, {}, {}, {}, {})
    return [rep]


def _reports_with(rules, sheet, cfg):
    """同 _reports，但允许换一份 rules（用来造停用节点）。"""
    import qqdoc
    from unittest import mock
    led = ledgers_cfg()["ledgers"][0]
    wd = core.WorkdayCalc(rules["workday"], None)
    with mock.patch.object(qqdoc, "read_sheet", lambda *a: sheet):
        rep, _ = core.evaluate_ledger(led, rules["rulesets"]["box"], wd,
                                      TODAY, {}, {}, {}, {})
    return [rep]


class DisabledNodeChannelTest(unittest.TestCase):
    """
    停用节点的「⏸ 未启用」提示：**不进企微，但必须留在终端**（2026-08-18 业务决定）。

    两半都要断言，缺一半这条测试就守不住东西：
      · 只断言「企微里没有」→ 把三处渲染全删光也能绿，
        那就把「一个悄悄不跑的规则比一个跑错的规则更难发现」这条安全属性丢了；
      · 只断言「终端里有」→ 本轮的改动等于没测。
    """

    def setUp(self):
        self.cfg = output_cfg()
        rules = rules_cfg(collect={"enabled": False})
        # 这批行本来正好卡在①收资，节点一停用就都落不到任何节点上 ——
        # 正是生产里那 6 个「待收资」项目的处境。
        self.reps = _reports_with(rules, collect_rows([26, 78, 81]), self.cfg)
        self.md = check_followup.render_wecom(self.reps, TODAY, self.cfg)
        self.txt = check_followup.render(self.reps, TODAY, False, self.cfg)

    def test_the_fixture_really_has_a_disabled_node(self):
        """先证明造出来的确实是停用场景，否则下面两条都是空跑。"""
        self.assertTrue(self.reps[0].disabled_nodes,
                        "fixture 没造出停用节点，后两条断言证明不了任何东西")

    def test_wecom_push_does_not_mention_it(self):
        self.assertNotIn("未启用", self.md,
                         f"停用节点不该再推给业务：\n{self.md}")

    def test_terminal_output_still_announces_it(self):
        self.assertIn("未启用", self.txt,
                      "终端仍要明说 —— 安全属性只是换通道，不是取消")


class OrderingTest(unittest.TestCase):

    def test_items_are_ascending_by_overdue_days(self):
        # 待收资 boundary=on days=7 → 首提第 7 天 → 允许 6 天
        out = render_both(collect_rows([81, 26, 78]))
        got = [int(m) for m in re.findall(r"超期 (\d+) 天", out)]
        self.assertEqual(got, sorted(got), f"必须升序，实际 {got}")
        self.assertEqual(got, [20, 72, 75], "显示的是超期天数，不是在节点的天数")

    def test_items_are_numbered_from_one(self):
        out = render_both(collect_rows([30, 40, 50]))
        nums = re.findall(r"^(\d+)、", out, re.M)
        self.assertEqual(nums, ["1", "2", "3"])

    def test_numbering_restarts_per_stage(self):
        sheet = make_sheet([
            row(1, "甲", tech="待收资", reported=TODAY - timedelta(days=30),
                progress=TODAY - timedelta(days=30)),
            row(2, "乙", tech="可行", install="",
                reported=TODAY - timedelta(days=40),
                progress=TODAY - timedelta(days=40)),
            row(3, "丙", tech="可行", install="",
                reported=TODAY - timedelta(days=50),
                progress=TODAY - timedelta(days=50)),
        ])
        out = render_both(sheet)
        self.assertEqual(re.findall(r"^(\d+)、", out, re.M), ["1", "1", "2"])

    def test_no_ledger_key_in_output(self):
        """业务 2026-07-31 改口：新样例不带台账序号。"""
        out = render_both(collect_rows([30]))
        self.assertNotIn("台账 #", out)
        self.assertNotIn("#1", out)

    def test_no_stage_action_text(self):
        """「催客户/客户经理交资料」这类描述已删，为了减少信息长度。"""
        rules = rules_cfg(collect={"action": "催客户/客户经理交资料",
                                   "backlog_note": "资料一直没收上来"})
        with temp_home(rules=rules):
            r = run_main([f"--today={TODAY}", "--dry-run"], collect_rows([30]))
            self.assertNotIn("催客户", r.out)
            self.assertNotIn("资料一直没收上来", r.out)

    def test_headline_shows_totals(self):
        out = render_both(collect_rows([30, 40]))
        self.assertIn("总任务量：2 个项目里，2 个要催办", out)

    def test_backlog_line_needs_more_than_one_stage(self):
        """只有一个阶段时「积压最重…占 100%」是废话，不显示。"""
        one = render_both(collect_rows([30, 40]))
        self.assertNotIn("积压最重", one)

        two = make_sheet([
            row(1, "甲", tech="待收资", reported=TODAY - timedelta(days=30),
                progress=TODAY - timedelta(days=30)),
            row(2, "乙", tech="可行", install="",
                reported=TODAY - timedelta(days=40),
                progress=TODAY - timedelta(days=40)),
        ])
        self.assertIn("积压最重：", render_both(two))

    def test_merged_denominator_deducts_advanced_only_for_display(self):
        """
        两张哨兵台账合成一个区块；业务分母去掉已接力的重复项目，
        但每张台账自身的 accounted == total_rows 护栏不变。
        """
        first = core.Report({"id": "sentinel_qq", "name": "前期台账",
                             "line": "sentinel", "display_name": "AI哨兵"})
        first.total_rows = 14
        first.advanced.append(("已接力",))
        first.no_node = 13
        second = core.Report({"id": "sentinel_lark", "name": "飞书台账",
                              "line": "sentinel", "display_name": "AI哨兵"})
        second.total_rows = 83
        second.no_node = 83

        self.assertEqual(first.accounted, first.total_rows)
        self.assertEqual(second.accounted, second.total_rows)
        sections = check_followup.merge_reports([first, second])
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0].in_scope, 96)
        out = check_followup.render([first, second], TODAY, False, output_cfg())
        self.assertEqual(out.count("——AI哨兵——"), 1)
        self.assertIn("总任务量：96 个项目里，0 个要催办", out)


class TerminalHintTest(unittest.TestCase):
    """「超2个月 可考虑终止」分割线。"""

    HINT = "超2个月 可考虑终止"

    def test_divider_before_first_item_at_or_over_60_days(self):
        # 在节点 26/78/81 天 → 超期 20/72/75 天（待收资允许 6 天）
        out = render_both(collect_rows([26, 78, 81]))
        lines = [l for l in out.splitlines() if "超期" in l or self.HINT in l]
        self.assertEqual(
            [("HINT" if self.HINT in l else re.search(r"超期 (\d+)", l).group(1))
             for l in lines],
            ["20", "HINT", "72", "75"])

    def test_boundary_59_60_61(self):
        """
        🔴 业务 2026-07-31 确认：60 天这条线按**超期天数**算，不是在节点的天数。

        所以要造出超期 59/60/61，得让它们在节点待 65/66/67 天。
        判据与显示的数字一致，业务才不会看到「显示超期 55 天却出现了超2个月」。
        """
        out = render_both(collect_rows([65, 66, 67]))
        idx = out.splitlines()
        pos_hint = next(i for i, l in enumerate(idx) if self.HINT in l)
        pos_59 = next(i for i, l in enumerate(idx) if "超期 59 天" in l)
        pos_60 = next(i for i, l in enumerate(idx) if "超期 60 天" in l)
        self.assertLess(pos_59, pos_hint, "超期 59 天在线以上")
        self.assertLess(pos_hint, pos_60, "超期 60 天在线以下（含 60）")

    def test_no_divider_when_nothing_is_old_enough(self):
        out = render_both(collect_rows([10, 20, 30]))   # 超期最多 24 天
        self.assertNotIn(self.HINT, out)

    def test_almost_sixty_still_has_no_divider(self):
        """
        口径改动的直接后果：在节点待满 60 天但只超期 54 天，**不再**出分割线。
        旧口径下这条会出线。业务已确认按新口径。
        """
        out = render_both(collect_rows([60]))
        self.assertIn("超期 54 天", out)
        self.assertNotIn(self.HINT, out)

    def test_divider_goes_first_when_everything_is_old(self):
        out = render_both(collect_rows([70, 80, 90]))   # 超期 64/74/84 天
        lines = out.splitlines()
        pos_hint = next(i for i, l in enumerate(lines) if self.HINT in l)
        pos_first = next(i for i, l in enumerate(lines) if "超期 64 天" in l)
        self.assertLess(pos_hint, pos_first)

    def test_only_configured_stages_get_the_divider(self):
        """业务确认：只在【待收资】加。其他阶段超期再久也不出这行。"""
        sheet = make_sheet([
            row(1, "老甲", tech="可行", install="",
                reported=TODAY - timedelta(days=200),
                progress=TODAY - timedelta(days=200)),
        ])
        out = render_both(sheet)
        # ③预调试/安装 允许 21 天 → 200 − 21 = 179
        self.assertIn("超期 179 天", out)
        self.assertNotIn(self.HINT, out, "③预调试/安装 不该出现建议终止文案")

    def test_can_be_disabled_by_config(self):
        cfg = output_cfg(terminal_hint={"enabled": False})
        out = render_both(collect_rows([26, 81]), cfg)
        self.assertNotIn(self.HINT, out)

    def test_empty_stages_means_all_stages(self):
        cfg = output_cfg(terminal_hint={"stages": []})
        sheet = make_sheet([
            row(1, "老甲", tech="可行", install="",
                reported=TODAY - timedelta(days=200),
                progress=TODAY - timedelta(days=200)),
        ])
        out = render_both(sheet, cfg)
        self.assertIn(self.HINT, out)


class WecomRenderTest(unittest.TestCase):
    """企微那份的样式约束。"""

    def setUp(self):
        self.cfg = output_cfg()
        self.reps = _reports(collect_rows([26, 78, 81]), self.cfg)
        self.md = check_followup.render_wecom(self.reps, TODAY, self.cfg)
        self.txt = check_followup.render(self.reps, TODAY, False, self.cfg)

    def test_heading_levels_for_size(self):
        """「大一号字」靠标题分级实现 —— markdown_v2 没有字号语法。"""
        self.assertIn("# 🧚 项目跟进精灵", self.md)
        self.assertIn("## AI节能盒子", self.md)
        self.assertIn("### 【待收资】", self.md)

    def test_items_carry_no_styling(self):
        """业务否掉了「抬高其他项来反衬」：条目一律不加粗、不引用、不斜体。"""
        for line in self.md.splitlines():
            if "超期" in line and "、" in line:
                self.assertNotIn("**", line, f"条目不该加粗：{line}")
                self.assertFalse(line.startswith(">"), f"条目不该用引用：{line}")
                self.assertFalse(line.startswith("- "), f"条目不该是列表项：{line}")

    def test_numbering_uses_ideographic_comma_not_markdown_list(self):
        """
        🔴 用「1.」会被 markdown 解析成有序列表；被分割线打断后，
           后半段可能被渲染器重新从 1 编号，业务会看到两个「1」。
           用顿号就是纯文本，零渲染风险。
        """
        self.assertRegex(self.md, r"(?m)^1、")
        self.assertNotRegex(self.md, r"(?m)^\d+\. ")

    def test_divider_is_not_a_bare_hr(self):
        """
        紧跟在文字后的一行 `---` 在 markdown 里会把上一行变成二级标题。
        所以分割线写成「------- 文字 -------」整行。
        """
        for line in self.md.splitlines():
            self.assertNotEqual(line.strip(), "---",
                                "不能出现裸的 --- 分割线")
        self.assertIn("------- 超2个月 可考虑终止 -------", self.md)

    def test_both_renderers_list_the_same_items_in_the_same_order(self):
        """防止只改了一边。"""
        def items(t):
            return re.findall(r"^\d+、(.+?) — 超期 (\d+) 天", t, re.M)
        self.assertEqual(items(self.md), items(self.txt))
        self.assertTrue(items(self.md))

    def test_fits_in_wecom_limit(self):
        """新格式加了标题标记和编号，会涨。确认仍在 4096 内。"""
        msgs = wecom_push.render_chunks(self.md, 4000)
        self.assertEqual(len(msgs), 1, "这点量不该被拆")
        self.assertLess(wecom_push._bytes(self.md), 4096)


class MutedSectionTest(unittest.TestCase):
    """
    静默期清单：只进日志，绝不进企微。

    2026-08-10 的由来：业务问「某个项目至今没发货，怎么不提醒」。
    它其实前几天催过、正在等下一个提醒日 —— 但「等下次」和「判定认为不用催」
    在推送和日志里长得一模一样，查了半天才分清。业务同时明确：
    **这批不要推给她**，推送只回答「今天该做什么」。
    """

    def _reports_with_muted(self):
        """造一批已经催过、今天不到复提醒日的项目。"""
        import qqdoc
        from unittest import mock
        sheet = collect_rows([26, 30], tech="待收资")
        led = ledgers_cfg()["ledgers"][0]
        rules = rules_cfg()
        wd = core.WorkdayCalc(rules["workday"], None)
        # 昨天刚催过，而 box①收资 是隔周（days=7）→ 今天必然静默
        state = {f"box|{i}|collect": {"first_overdue": str(TODAY - timedelta(days=10)),
                                      "last_notified": str(TODAY - timedelta(days=1))}
                 for i in (1, 2)}
        with mock.patch.object(qqdoc, "read_sheet", lambda *a: sheet):
            rep, _ = core.evaluate_ledger(led, rules["rulesets"]["box"], wd,
                                          TODAY, {}, state, {}, {})
        return [rep]

    def setUp(self):
        self.cfg = output_cfg()
        self.reps = self._reports_with_muted()
        self.assertTrue(self.reps[0].overdue_muted, "前置：这批必须真的处在静默期")
        self.md = check_followup.render_wecom(self.reps, TODAY, self.cfg)
        self.txt = check_followup.render(self.reps, TODAY, False, self.cfg)

    def test_log_lists_them(self):
        self.assertIn("【静默期】", self.txt)
        for it in self.reps[0].overdue_muted:
            self.assertIn(it.name, self.txt)

    def test_log_says_why_today_is_quiet(self):
        """光列名字不够 —— 要能看出「上次什么时候催的、多久催一次」。"""
        self.assertIn("上次提醒", self.txt)
        self.assertIn("每 7 天提醒", self.txt)

    def test_wecom_never_shows_it(self):
        """🔴 业务明确不要。这条是本组的锚点。"""
        self.assertNotIn("静默期", self.md)
        for it in self.reps[0].overdue_muted:
            self.assertNotIn(it.name, self.md)

    def test_wording_is_not_the_forbidden_truncation_phrase(self):
        """
        🔴 业务口径决策第 3 条：不许出现「…另有 N 条」的截断。
           静默期不是截断，但措辞长得像就会被读成「清单被砍了」。
        """
        self.assertNotIn("另有", self.txt)

    def test_nothing_muted_means_no_section(self):
        reps = _reports(collect_rows([26, 78, 81]), self.cfg)
        self.assertFalse(reps[0].overdue_muted)
        self.assertNotIn("【静默期】",
                         check_followup.render(reps, TODAY, False, self.cfg))


class CadenceOnStageLineTest(unittest.TestCase):
    """业务改了提醒规则，推送里要能看出来生效的是什么。"""

    def setUp(self):
        self.cfg = output_cfg()
        self.reps = _reports(collect_rows([26, 78, 81]), self.cfg)
        self.md = check_followup.render_wecom(self.reps, TODAY, self.cfg)
        self.txt = check_followup.render(self.reps, TODAY, False, self.cfg)

    def test_both_renderers_show_it(self):
        self.assertIn("### 【待收资】3 项 · 每 7 天提醒", self.md)
        self.assertIn("【待收资】3 项 · 每 7 天提醒", self.txt)

    def test_hidden_when_a_group_mixes_cadences(self):
        """合并分节后同名阶段可能来自两份台账。挑一个显示等于对另一半撒谎。"""
        groups = check_followup.group_items(self.reps[0].due, self.cfg)
        self.assertEqual(len(groups), 1)
        groups[0]["items"][0].extra["cadence"] = "周一/周四提醒"
        self.assertEqual(check_followup._one_cadence(groups[0]["items"]), "")


class NeverTruncateTest(unittest.TestCase):
    """业务硬要求：每笔项目都要看到，绝不能出现「…另有 N 条」。"""

    def test_all_items_are_listed(self):
        days = list(range(22, 22 + 40))
        sheet = collect_rows(days, tech="可行", install="")
        out = render_both(sheet)
        self.assertEqual(len(re.findall(r"超期 \d+ 天", out)), 40)
        self.assertNotIn("另有", out)


if __name__ == "__main__":
    unittest.main()
