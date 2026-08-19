#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
`doctor.py --values <台账id>`：只读枚举判据用到的各列真实取值。

═══════════════════════════════════════════════════════════════════════
改触发条件之前必须先看这个。照抄业务口头说的写法**会一条都匹配不上**，
而且不报错，只表现为那个节点从此不催 —— 实测踩过两次：

  · 同一个文件里三个子表，分行写「杭州」/「杭州分行」两种
  · GEO 的「启动优化时间」全表 43 种写法，业务说的「未开始」在表里
    根本不存在（实际是「未开始，等客户确认平台」等 7 种）

两次都是靠一次性临时脚本发现的，每次要用都得重写。这里把它变成常驻命令。

🔴 只列**判据引用到的**列。主键列与项目名称列故意不列：它们是身份不是判据，
   没人会对企业名写触发条件，打出来只会把上百个客户名刷满屏幕，
   把真正要核的那两三列淹掉。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from unittest import mock

from harness import (temp_home, ledgers_cfg, rules_cfg, state_files,
                     run_doctor)

import core
import doctor


class FakeSheet:
    """够用就好：header + 每行一个 dict。"""

    def __init__(self, header, rows):
        self.header = header
        self._rows = rows

    @property
    def data_rows(self):
        return list(range(len(self._rows)))

    def has_column(self, f):
        return f in self.header

    def text(self, r, f):
        return self._rows[r].get(f, "")


HEADER = ["序号", "项目名称", "地点", "技术确认", "安装调试", "节能测试",
          "需求上报日期", "最新进展日期", "备注"]

ROWS = [
    {"序号": "1", "项目名称": "甲公司", "地点": "杭州", "技术确认": "待收资"},
    {"序号": "2", "项目名称": "乙公司", "地点": "杭州分行", "技术确认": "待收资"},
    {"序号": "3", "项目名称": "丙公司", "地点": "深圳", "技术确认": "可行"},
    {"序号": "4", "项目名称": "丁公司", "地点": "", "技术确认": ""},
]


def run_values(ledger_id="box", ledgers=None, rules=None, sheet=None):
    out, err = io.StringIO(), io.StringIO()
    with temp_home(ledgers=ledgers or ledgers_cfg(),
                   rules=rules or rules_cfg()) as home:
        with mock.patch.object(core, "read_ledger_sheet",
                               lambda l: sheet or FakeSheet(HEADER, ROWS)), \
             mock.patch.object(sys, "argv",
                               ["doctor.py", "--values", ledger_id]), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = doctor.main()
        left = state_files(home)
    return code, out.getvalue(), err.getvalue(), left


class ListsJudgementColumnsTest(unittest.TestCase):

    def test_scope_filter_column_is_listed_with_counts(self):
        code, out, err, _ = run_values()
        self.assertEqual(code, 0, err)
        self.assertIn("【地点】", out)
        self.assertIn("'杭州'", out)
        self.assertIn("'杭州分行'", out)

    def test_the_two_spellings_problem_is_visible(self):
        """
        🔴 这条就是这个命令存在的理由：「杭州」和「杭州分行」并存时，
           scope_filters 只枚举了其中一种 —— 另一种会被静默过滤光。
        """
        _, out, _, _ = run_values()
        block = out.split("【地点】")[1].split("【")[0]
        self.assertIn("杭州", block)
        self.assertIn("杭州分行", block)

    def test_scope_filter_column_is_listed_even_when_nothing_else_references_it(self):
        """
        单独钉住 scope_filters 这一路。默认 fixture 里「地点」同时出现在
        known_values 中，所以上一条测试即使砍掉 scope_filters 这一路也照样绿
        —— 冗余覆盖是测不出东西的。这里换一列，只由 scope_filters 引用。
        """
        led = ledgers_cfg()
        led["ledgers"][0]["scope_filters"] = [
            {"field": "安装调试", "op": "in", "values": ["已完成"]}]
        led["ledgers"][0]["known_values"] = {}
        _, out, _, _ = run_values(ledgers=led)
        self.assertIn("【安装调试】", out)
        self.assertIn("责任范围过滤", out)

    def test_when_condition_column_is_listed(self):
        _, out, _, _ = run_values()
        self.assertIn("【技术确认】", out)
        self.assertIn("待收资", out)

    def test_blank_rows_are_counted_separately(self):
        """空值单独说：它既不是一种「取值」，又常常是判据的一半（op: empty）。"""
        _, out, _, _ = run_values()
        self.assertIn("行为空", out)

    def test_why_each_column_is_listed(self):
        """只给一串列名没用 —— 要说清它是被谁引用的，才知道改了会影响什么。"""
        _, out, _, _ = run_values()
        self.assertIn("责任范围过滤", out)
        self.assertIn("节点", out)


class DoesNotDumpIdentityColumnsTest(unittest.TestCase):
    """🔴 主键与项目名称不列 —— 它们是身份不是判据，列出来只是刷客户名。"""

    def test_name_column_is_not_listed(self):
        _, out, _, _ = run_values()
        self.assertNotIn("【项目名称】", out)
        self.assertNotIn("甲公司", out)

    def test_key_column_is_not_listed(self):
        _, out, _, _ = run_values()
        self.assertNotIn("【序号】", out)


class DisabledNodesTest(unittest.TestCase):
    """停用节点的字段也列 —— 改配置时常常正要把某个停用节点打开。"""

    def test_disabled_node_fields_are_listed_and_marked(self):
        rules = rules_cfg()
        for n in rules["rulesets"]["box"]["nodes"]:
            if n["id"] == "collect":
                n["enabled"] = False
        _, out, _, _ = run_values(rules=rules)
        self.assertIn("【技术确认】", out)
        self.assertIn("未启用", out, "要标出来，否则会以为它正在生效")


class MissingColumnTest(unittest.TestCase):

    def test_referenced_column_absent_from_the_sheet_is_flagged(self):
        """
        配置引用了一个台账里没有的列 —— 判据会静默失效。
        这里要红字点出来，而不是安静地跳过这一列。
        """
        sheet = FakeSheet([h for h in HEADER if h != "技术确认"], ROWS)
        _, out, _, _ = run_values(sheet=sheet)
        self.assertIn("技术确认", out)
        self.assertIn("静默失效", out)


class ArgumentTest(unittest.TestCase):

    def test_unknown_ledger_id_lists_the_real_ones(self):
        code, out, err, _ = run_values(ledger_id="打错的id")
        self.assertEqual(code, 2)
        self.assertIn("打错的id", err)
        self.assertIn("box", err, "要顺手给出现有的 id，不然还得去翻配置")


class ReadOnlyTest(unittest.TestCase):
    """自检是纯诊断工具：不写状态、不发消息。这条命令也不例外。"""

    def test_writes_no_state(self):
        _, _, _, left = run_values()
        self.assertEqual(left, set(), f"不该留下任何状态文件，实际有 {left}")

    def test_writes_are_blocked_at_the_core_gate(self):
        """
        闸门是真开着的，不只是「这条路径恰好没有写入代码」。

        探针必须在 temp_home **里面**打：出了这个块 HERMES_HOME 就还原成
        真实目录了，那时再试写等于拿生产状态目录做实验。
        """
        with temp_home() as home:
            with mock.patch.object(core, "read_ledger_sheet",
                                   lambda l: FakeSheet(HEADER, ROWS)), \
                 mock.patch.object(sys, "argv", ["doctor.py", "--values", "box"]), \
                 contextlib.redirect_stdout(io.StringIO()):
                doctor.main()
            core.BLOCKED_WRITES.clear()
            core.write_state("试探.json", {"x": 1})
            self.assertTrue(core.BLOCKED_WRITES,
                            "--values 跑完后只读闸门应该仍然开着")
            self.assertNotIn("试探.json", state_files(home))


class DisabledReasonShownInDoctorTest(unittest.TestCase):
    """
    停用节点的理由必须在 `doctor --validate-config` 里说得出来。

    🔴 2026-08-18 起「⏸ 未启用」不再进企微推送，**doctor 成了这条信息的主通道**。
       主通道说不出理由就失去了意义 —— 而配置里两种键名都在用
       （`_禁用原因` / `_停用说明`），doctor 原本只认前者，
       于是盒子线①收资那份写得最详细的停用依据反而打不出来，
       只剩通用兜底「配置里 enabled=false」。

    ⚠️ 第一版这条测试写错了：拿 `doctor --values`（枚举列取值）去测，
       那是另一条代码路径，根本不经过这里 —— 新旧代码上都红。
       坑 #11 的近亲：**测试必须真的打在被改的那段上**。
    """

    def _doctor_out(self, key: str | None) -> str:
        rules = rules_cfg()
        for n in rules["rulesets"]["box"]["nodes"]:
            if n["id"] == "collect":
                n["enabled"] = False
                if key:
                    n[key] = f"标记理由-{key}"
        with temp_home(rules=rules):
            _, out = run_doctor(["--validate-config"])
        return out

    def test_reason_key_禁用原因_is_shown(self):
        self.assertIn("标记理由-_禁用原因", self._doctor_out("_禁用原因"))

    def test_reason_key_停用说明_is_shown(self):
        """旧代码上是真红：`_停用说明` 被忽略，只打通用兜底。"""
        self.assertIn("标记理由-_停用说明", self._doctor_out("_停用说明"))

    def test_without_any_reason_it_still_says_something(self):
        """两个键都没有时仍要有兜底，不能打出空白。"""
        out = self._doctor_out(None)
        self.assertIn("未启用", out)
        self.assertIn("enabled=false", out)


if __name__ == "__main__":
    unittest.main()

