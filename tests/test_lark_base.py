#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞书取数层：分页、关联日期、异常返回、缺列。

═══════════════════════════════════════════════════════════════════════
这一层此前只有「找得到 lark-cli」有测试（test_lark_cli_lookup.py），
真正的取数逻辑一条都没有。而它恰好集中了几种最会静默出错的东西：

  分页    —— 少读一页 = 少一批项目，总数看起来仍然合理
  关联日期 —— 换不成日期就成了 recXXXX 字符串，日期解析失败 → 那条被跳过
  异常返回 —— lark-cli 返回非 JSON / ok=false 时若被当成空表，
             整份台账变成「今天没有要催的」

全部走打桩，零网络、不碰真实台账。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import unittest
from datetime import date
from unittest import mock

from harness import core  # noqa: F401 —— 挂 sys.path

import lark_base  # noqa: E402
from qqdoc import LedgerError  # noqa: E402


def page(fields, rows, rids, has_more=False):
    return {"data": {"fields": fields, "data": rows,
                     "record_id_list": rids, "has_more": has_more}}


class PagingTest(unittest.TestCase):
    """一页 200 条。少读一页不会报错，只会少一批项目。"""

    def test_single_page(self):
        with mock.patch.object(lark_base, "_run_cli", return_value=page(
                ["企业名称"], [["甲"], ["乙"]], ["r1", "r2"])):
            sh = lark_base.read_sheet("bas", "tbl")
        self.assertEqual(len(sh.data_rows), 2)
        self.assertEqual(sh.text(1, "企业名称"), "甲")

    def test_follows_has_more(self):
        pages = [page(["企业名称"], [["甲"], ["乙"]], ["r1", "r2"], has_more=True),
                 page(["企业名称"], [["丙"]], ["r3"], has_more=False)]
        with mock.patch.object(lark_base, "_run_cli",
                               side_effect=pages) as run:
            sh = lark_base.read_sheet("bas", "tbl")
        self.assertEqual(len(sh.data_rows), 3, "第二页必须被读进来")
        self.assertEqual(sh.text(3, "企业名称"), "丙")
        self.assertEqual(run.call_count, 2)

    def test_offset_advances_by_rows_read(self):
        pages = [page(["A"], [["1"], ["2"]], ["r1", "r2"], has_more=True),
                 page(["A"], [["3"]], ["r3"])]
        with mock.patch.object(lark_base, "_run_cli",
                               side_effect=pages) as run:
            lark_base.read_sheet("bas", "tbl")
        offsets = [c.args[1][c.args[1].index("--offset") + 1]
                   for c in run.call_args_list]
        self.assertEqual(offsets, ["0", "2"],
                         "offset 不按已读条数推进会重复读同一页或跳过一段")

    def test_has_more_but_empty_page_stops(self):
        """
        服务端说还有、却给了空页 —— 不加这一条会无限循环，
        任务卡死到超时，表现是「今天什么都没发生」。
        """
        pages = [page(["A"], [["1"]], ["r1"], has_more=True),
                 page(["A"], [], [], has_more=True)]
        with mock.patch.object(lark_base, "_run_cli", side_effect=pages):
            sh = lark_base.read_sheet("bas", "tbl")
        self.assertEqual(len(sh.data_rows), 1)

    def test_record_id_is_exposed_as_a_column(self):
        with mock.patch.object(lark_base, "_run_cli", return_value=page(
                ["企业名称"], [["甲"]], ["recABC"])):
            sh = lark_base.read_sheet("bas", "tbl")
        self.assertTrue(sh.has_column("_record_id"))
        self.assertEqual(sh.text(1, "_record_id"), "recABC")


class LinkDateFieldsTest(unittest.TestCase):
    """
    关联字段读出来只是记录 id。换不成日期的话，那一列所有行都解析失败，
    依赖它计时的节点会被整批跳过 —— 不报错，只是「没有要催的」。
    """

    MAIN = ["企业名称", "发货表"]
    CHILD = ["发货时间"]

    def fetch(self, main_rows, child_rows, child_rids):
        calls = []

        def fake(sub, args):
            tid = args[args.index("--table-id") + 1]
            calls.append(tid)
            if tid == "tblMain":
                return page(self.MAIN, main_rows,
                            [f"r{i}" for i in range(1, len(main_rows) + 1)])
            return page(self.CHILD, child_rows, child_rids)

        return fake, calls

    def read(self, main_rows, child_rows, child_rids):
        fake, _ = self.fetch(main_rows, child_rows, child_rids)
        with mock.patch.object(lark_base, "_run_cli", side_effect=fake):
            return lark_base.read_sheet(
                "bas", "tblMain",
                link_date_fields=[{"link_field": "发货表",
                                   "child_table_id": "tblChild",
                                   "child_date_field": "发货时间"}])

    def test_link_becomes_a_real_date(self):
        sh = self.read([["甲", [{"id": "c1"}]]],
                       [["2026-05-20 10:00:00"]], ["c1"])
        self.assertEqual(sh.date(1, "发货表"), date(2026, 5, 20))

    def test_without_the_mapping_it_stays_an_unparsable_id(self):
        """不配 link_date_fields 时读到的就是记录 id —— 对照组，证明这层确实在起作用。"""
        with mock.patch.object(lark_base, "_run_cli", return_value=page(
                self.MAIN, [["甲", [{"id": "c1"}]]], ["r1"])):
            sh = lark_base.read_sheet("bas", "tblMain")
        self.assertIsNone(sh.date(1, "发货表"))
        self.assertEqual(sh.text(1, "发货表"), "c1")

    def test_multiple_links_take_the_earliest(self):
        sh = self.read([["甲", [{"id": "c2"}, {"id": "c1"}]]],
                       [["2026-05-20 10:00:00"], ["2026-03-01 09:00:00"]],
                       ["c1", "c2"])
        self.assertEqual(sh.date(1, "发货表"), date(2026, 3, 1),
                         "多条关联取最早一次（比如最早的发货时间）")

    def test_empty_link_becomes_empty_not_garbage(self):
        sh = self.read([["甲", []]], [["2026-05-20 10:00:00"]], ["c1"])
        self.assertEqual(sh.text(1, "发货表"), "")
        self.assertIsNone(sh.date(1, "发货表"))

    def test_dangling_link_id_becomes_empty(self):
        """关联记录被删了。空是对的——不该留下一个解析不了的 id 冒充有值。"""
        sh = self.read([["甲", [{"id": "已经不存在"}]]],
                       [["2026-05-20 10:00:00"]], ["c1"])
        self.assertEqual(sh.text(1, "发货表"), "")


class BadResponseTest(unittest.TestCase):
    """
    🔴 取数失败必须抛错。当成空表继续跑的话，整份台账变成
    「今天没有要催的」—— 故障伪装成正常，是这个项目从头到尾在防的那件事。
    """

    def run_cli(self, *, returncode=0, stdout="", stderr=""):
        proc = mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)
        with mock.patch.object(lark_base, "lark_cli_bin", return_value="/x/lark-cli"), \
             mock.patch.object(lark_base.subprocess, "run", return_value=proc):
            return lark_base._run_cli("+record-list", [])

    def test_non_json_output(self):
        with self.assertRaises(LedgerError) as cm:
            self.run_cli(stdout="Segmentation fault")
        self.assertIn("不是合法 JSON", str(cm.exception))

    def test_ok_false_surfaces_the_server_message(self):
        with self.assertRaises(LedgerError) as cm:
            self.run_cli(stdout='{"ok": false, "error": {"message": "no authority"}}')
        self.assertIn("no authority", str(cm.exception),
                      "要把服务端原话带出来，否则无从排查")

    def structured_error(self, error, *, identity="user") -> str:
        payload = {"ok": False, "identity": identity, "error": error}
        with self.assertRaises(LedgerError) as cm:
            self.run_cli(stdout=json.dumps(payload, ensure_ascii=False))
        return str(cm.exception)

    def test_missing_scope_does_not_tell_author_to_add_collaborator(self):
        out = self.structured_error({
            "type": "authorization", "subtype": "missing_scope",
            "message": "missing scope", "missing_scopes": ["bitable:app:readonly"],
            "hint": "login with the missing scope",
        })
        self.assertIn("API 权限范围问题", out)
        self.assertIn("bitable:app:readonly", out)
        self.assertIn("不要让文档作者重复添加协作者", out)

    def test_login_error_names_the_same_profile(self):
        out = self.structured_error({
            "type": "authentication", "subtype": "not_logged_in",
            "message": "login required",
        })
        self.assertIn("用户登录态问题", out)
        self.assertIn("auth status --profile sentinel", out)
        self.assertIn(
            'auth login --profile sentinel --scope "bitable:app:readonly offline_access"',
            out)

    def test_resource_permission_names_user_collaborator_not_bot(self):
        out = self.structured_error({
            "type": "api", "subtype": "forbidden", "code": 91403,
            "message": "forbidden",
        })
        self.assertIn("没有这份 Base 的资源权限", out)
        self.assertIn("实际登录账号", out)
        self.assertIn("不要把机器人当成协作者", out)

    def test_keychain_error_is_not_mislabeled_as_login(self):
        out = self.structured_error({
            "type": "api", "subtype": "unknown",
            "message": "keychain Get failed: keychain not initialized",
        })
        self.assertIn("本机凭证存储不可用", out)
        self.assertIn("config show --profile sentinel", out)
        self.assertIn("不是重新扫码", out)

    def test_unknown_error_preserves_classification_and_stops_guessing(self):
        out = self.structured_error({
            "type": "api", "subtype": "unknown", "code": 999,
            "message": "unexpected upstream failure",
        })
        self.assertIn("unexpected upstream failure", out)
        self.assertIn("type=api", out)
        self.assertIn("subtype=unknown", out)
        self.assertIn("不要在“重新登录”和“添加协作者”之间盲目来回尝试", out)

    def test_timeout(self):
        import subprocess as sp
        with mock.patch.object(lark_base, "lark_cli_bin", return_value="/x/lark-cli"), \
             mock.patch.object(lark_base.subprocess, "run",
                               side_effect=sp.TimeoutExpired("lark-cli", 45)):
            with self.assertRaises(LedgerError) as cm:
                lark_base._run_cli("+record-list", [])
        self.assertIn("超时", str(cm.exception))

    def test_present_but_not_executable(self):
        with mock.patch.object(lark_base, "lark_cli_bin", return_value="/x/lark-cli"), \
             mock.patch.object(lark_base.subprocess, "run",
                               side_effect=FileNotFoundError):
            with self.assertRaises(LedgerError) as cm:
                lark_base._run_cli("+record-list", [])
        self.assertIn("无法执行", str(cm.exception))

    def test_empty_table_is_not_an_error(self):
        """真的一行都没有，与「读失败」是两回事，不能混。"""
        with mock.patch.object(lark_base, "_run_cli",
                               return_value=page(["企业名称"], [], [])):
            sh = lark_base.read_sheet("bas", "tbl")
        self.assertEqual(sh.data_rows, [])
        self.assertTrue(sh.has_column("企业名称"))


class MissingColumnTest(unittest.TestCase):

    def sheet(self):
        with mock.patch.object(lark_base, "_run_cli", return_value=page(
                ["企业名称", "项目状态"], [["甲", "已实施"]], ["r1"])):
            return lark_base.read_sheet("bas", "tbl")

    def test_has_column_is_honest(self):
        sh = self.sheet()
        self.assertTrue(sh.has_column("项目状态"))
        self.assertFalse(sh.has_column("并不存在的列"))

    def test_missing_column_reads_as_empty(self):
        """
        取值层返回空是有意的（与腾讯文档那边一致）。
        「缺列」这件事由 assert_sheet 的入口断言统一报，
        不在每个取值点各报一次 —— 但正因如此，任何**不走断言**的取值路径
        （比如 cross_ledger）必须自己检查列在不在。
        """
        sh = self.sheet()
        self.assertEqual(sh.text(1, "并不存在的列"), "")
        self.assertIsNone(sh.date(1, "并不存在的列"))


class ReadOnlyWhitelistTest(unittest.TestCase):
    """整层的安全边界。改这里等于改只读铁律。"""

    def test_only_three_readonly_subcommands(self):
        self.assertEqual(lark_base.ALLOWED_SUBCOMMANDS,
                         frozenset({"+table-list", "+field-list", "+record-list"}))

    def test_write_subcommand_refused_before_anything_else(self):
        with mock.patch.object(lark_base, "lark_cli_bin") as look:
            with self.assertRaises(LedgerError):
                lark_base._run_cli("+record-create", [])
        look.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class IsoDateCellTest(unittest.TestCase):
    """
    飞书日期列的真实形状是 ISO-8601，认不出来会让整条业务线静默漏催。

    ═══════════════════════════════════════════════════════════════════
    🔴 2026-08-14 实测：哨兵飞书主台账「立项时间」返回
       '2025-10-24T16:06:34.000+08:00'，而适配层只认三种 strptime 格式，
       **731 行全部解析失败**。后果不是报错，是 ④发货 拿不到计时起点 →
       3 个项目落进「没有任何节点管」：既不催、也不终止。
       运行报告里只有一行数据质量警告，配置注释里还写着「72/72 都能解析」。

       解析失败 → None → 节点静默跳过，这是本项目最怕的失败形态，
       所以这里把真实字符串一字不差地钉住。
    ═══════════════════════════════════════════════════════════════════
    """

    def test_the_exact_string_from_production(self):
        self.assertEqual(lark_base._cell_date("2025-10-24T16:06:34.000+08:00"),
                         date(2025, 10, 24))

    def test_iso_variants(self):
        cases = {
            "2025-10-24T16:06:34.000+08:00": date(2025, 10, 24),
            "2025-10-24T16:06:34+08:00": date(2025, 10, 24),
            "2025-10-24T16:06:34": date(2025, 10, 24),
            "2025-10-24T00:00:00Z": date(2025, 10, 24),   # 3.9 的 fromisoformat 不认 Z
        }
        for s, want in cases.items():
            with self.subTest(s=s):
                self.assertEqual(lark_base._cell_date(s), want)

    def test_old_formats_still_work(self):
        """原有三种格式一个都不能丢 —— 别的台账还在用。"""
        for s in ("2025-10-24 16:06:34", "2025-10-24", "2025/10/24"):
            with self.subTest(s=s):
                self.assertEqual(lark_base._cell_date(s), date(2025, 10, 24))

    def test_junk_still_returns_none(self):
        """
        认不出来必须返回 None，不许瞎猜一个日期 ——
        猜错的计时起点会算出一个看起来正常的超期天数，比不催更难发现。
        """
        for s in ("", "待定", "recv0wSKfAQh3v", "2025-13-45", "第三季度"):
            with self.subTest(s=s):
                self.assertIsNone(lark_base._cell_date(s))
