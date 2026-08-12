#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
`docs/改一个催办节点.md` 与代码不许漂。

═══════════════════════════════════════════════════════════════════════
这份文档是「需求变了怎么改规则」的唯一操作卡，业务和接手的人照着它动手。
它说的 op 清单、repeat 写法、命令名一旦和代码对不上，人会照着一份
**看起来权威**的说明写出一个静默失效的节点 —— 比没有文档更糟。

漂移在这个项目里发生过：需求文档写「哨兵⑤按项目状态→已实施」，
配置用的却是「安装情况 not_contains 已完成」，比对时对不上，
花时间才确认两者结果其实相同。

所以这里不检查文风，只检查**会让人写错配置的那几样**。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import unittest
from pathlib import Path

from harness import ledgers_cfg          # 先导它，把 scripts/ 挂上 sys.path

import core

DOC = Path(__file__).resolve().parent.parent / "docs" / "改一个催办节点.md"


class ExistsTest(unittest.TestCase):

    def test_doc_is_shipped(self):
        self.assertTrue(DOC.exists(), "操作卡不在包里等于没有")

    def test_readme_and_skill_point_at_it(self):
        """没有入口的文档 == 不存在的文档。"""
        root = DOC.parent.parent
        for f in ("README.md", "SKILL.md"):
            with self.subTest(f=f):
                self.assertIn("改一个催办节点",
                              (root / f).read_text(encoding="utf-8"))


class MatchesTheCodeTest(unittest.TestCase):

    def setUp(self):
        self.text = DOC.read_text(encoding="utf-8")

    def test_every_supported_op_is_documented(self):
        """
        🔴 少写一个 op，人就不知道它能用；而多写一个不存在的 op，
           照着写出来的配置会在次日 9:00 抛错退出。
        """
        for op in core.CONDITION_OPS:
            with self.subTest(op=op):
                self.assertIn(op, self.text, f"文档没提 op={op}")

    def _op_rows(self) -> list:
        """
        op 对照表的数据行，切成列。

        两次收窄的教训：先是拿正则在全文找「`a` / `b`」，把 `name` / `stage`
        这些字段名也当成 op 报了错；改成按 `##` 切段后，又把同一节里紧跟着的
        「漏写取值字段」那张表一起吃了进来。**只取标记之后的第一张表** ——
        一个每次改文档都会误报的检查，用不了几次就会被人加白名单绕过去。
        """
        rows, started = [], False
        for line in self.text.split("八种 `op`", 1)[-1].splitlines():
            if line.startswith("|"):
                started = True
                cols = [c.strip() for c in line.strip("|").split("|")]
                if cols and not set(cols[0]) <= set("- :"):   # 跳过分隔行
                    rows.append(cols)
            elif started:
                break                                        # 第一张表到此为止
        return rows[1:]                                      # 去掉表头

    def test_op_table_matches_the_code_exactly(self):
        """
        🔴 两个方向都要对：少写一个，人不知道它能用；多写一个不存在的，
           照着写出来的配置会在次日 9:00 抛错退出。
        """
        import re
        listed = set()
        for cols in self._op_rows():
            listed |= set(re.findall(r"`(\w+)`", cols[0]))
        self.assertEqual(listed, set(core.CONDITION_OPS))

    def test_ops_needing_values_are_grouped_as_such(self):
        """`in` / `not_in` 要带 values，其余不要 —— 分组错了照样写出恒假的条件。"""
        import re
        for cols in self._op_rows():
            ops = set(re.findall(r"`(\w+)`", cols[0]))
            need = cols[1]
            with self.subTest(ops=sorted(ops)):
                if ops & core.VALUES_OPS:
                    self.assertIn("values", need)
                elif ops & core.VALUE_OPS:
                    self.assertIn("value", need)
                elif ops & core.NOARG_OPS:
                    self.assertIn("不带", need)

    def test_every_repeat_form_is_documented(self):
        """repeat 四选一，缺一种业务就以为不能那么配。"""
        for form in ("days", "workdays", "weekday", "monthday"):
            with self.subTest(form=form):
                self.assertIn(form, self.text)

    def _command_lines(self) -> list:
        """代码块里那些真的能敲的命令行。散文里提到脚本名不算数。"""
        lines, inside = [], False
        for line in self.text.splitlines():
            if line.startswith("```"):
                inside = line.startswith("```bash")
                continue
            if inside and line.strip():
                lines.append(line.strip())
        return lines

    def test_the_three_commands_are_runnable_and_real(self):
        """
        文档里给的命令必须真的存在，否则照着敲会报错。

        🔴 断言打在**代码块**里，不是全文 —— 只用 `assertIn(名字, 全文)`
           的话，散文里随口提一句就能让它绿，而那不是能敲的东西。
        """
        scripts = DOC.parent.parent / "scripts"
        cmds = "\n".join(self._command_lines())
        for name in ("doctor.py", "check_followup.py", "diff_due.py"):
            with self.subTest(name=name):
                self.assertIn(f"scripts/{name}", cmds,
                              f"{name} 没有出现在任何一条可执行命令里")
                self.assertTrue((scripts / name).exists())

    def test_flags_used_in_the_doc_are_real(self):
        import check_followup
        import doctor
        for flag, parser in (("--dry-run", check_followup),
                             ("--json", check_followup),
                             ("--ack-spec", check_followup),
                             ("--values", doctor),
                             ("--validate-config", doctor)):
            with self.subTest(flag=flag):
                self.assertIn(flag, self.text)
        # 参数真的挂在解析器上，不是只写在文档里
        known = {a.option_strings[0]
                 for a in check_followup.build_parser()._actions
                 if a.option_strings}
        for flag in ("--dry-run", "--json", "--ack-spec"):
            self.assertIn(flag, known)


class SaysTheDangerousPartsTest(unittest.TestCase):
    """
    每一条都对应一个**不报错**的坑。文档少说哪一条，
    哪一条就会被踩到，而且踩到时没有任何提示。
    """

    def setUp(self):
        self.text = DOC.read_text(encoding="utf-8")

    def test_node_order_matters(self):
        """
        判定取「第一个 when 全部满足的启用节点」就停下 —— 插错位置的节点
        永远轮不到，而且不报错。

        🔴 断言收在「新增或删除节点」这一节里。全文找「第一个」是绿不掉的：
           「假期后第一个工作日」也含这三个字，那条断言什么都证明不了。
        """
        section = self.text.split("## 情形三", 1)[-1]
        self.assertIn("第一个", section)
        self.assertIn("顺序", section)
        self.assertIn("轮不到", section, "要说清后果，不只是说「注意顺序」")

    def test_disable_instead_of_delete(self):
        self.assertIn("enabled", self.text)
        self.assertIn("不删", self.text)

    def test_string_false_is_truthy(self):
        self.assertIn('"false"', self.text)

    def test_in_without_values_is_always_false(self):
        self.assertIn("恒假", self.text)

    def test_contains_without_value_is_always_true(self):
        self.assertIn("恒真", self.text)

    def test_enumerate_real_values_before_changing_conditions(self):
        self.assertIn("--values", self.text)
        self.assertIn("一条都匹配不上", self.text)

    def test_says_config_changes_need_no_release(self):
        """业务提这个需求，就是因为以为改规则要等发版。"""
        self.assertIn("不用发版", self.text)

    def test_biweekly_means_seven_days(self):
        """业务口径：「隔周」＝每周一次、7 天。按字面理解会配成 14。"""
        self.assertIn("隔周", self.text)
        self.assertIn("7 天", self.text)

    def test_ledger_stays_read_only(self):
        self.assertIn("只读", self.text)


if __name__ == "__main__":
    unittest.main()
