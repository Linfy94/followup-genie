#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
企微在线表格只读取数层。

═══════════════════════════════════════════════════════════════════════
这一组守的还是那条铁律：**故障绝不能长得像「今天没有超时单」**。

企微这条线上它格外容易发生，因为**错误藏在第三层信封里**：
拿不到权限时 JSON-RPC 外层是 `"isError": false`，`result.content[0].text`
里那个字符串解出来才有 `errcode: 851008`。只看外层就会读成「0 行」。

而「851008」正是授权失效时的返回。授权会不会 7 天过期至今没有答案 ——
所以这一组里 errcode 那几条是本模块唯一真正的验收点。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import ast
import json
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from harness import core  # noqa: F401 —— 挂 sys.path

import wecom_doc  # noqa: E402
from qqdoc import LedgerError  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EXE = "/opt/fake/bin/wecom-cli"

INFO = {
    "errcode": 0, "errmsg": "ok", "name": "某需求记录",
    "sheets": [
        {"sheet_id": "w4q62o", "title": "甲子表", "row_count": 3, "column_count": 4},
        {"sheet_id": "BB08J2", "title": "乙子表", "row_count": 2, "column_count": 3},
    ],
}

# 🔴 正文里夹一行图片：实测企微返回的 Markdown 就长这样
#    （舆情那份第 1314 行和第 1547 行各一张）。
#
# 🔴 图片必须放在**子表的行与行之间**，那才是有杀伤力的位置。
#    放在两个子表之间是无害的：用「不以 | 开头就是新子表」这条错规则去切，
#    图片只会多出一个没人查的空子表，测试照样绿 —— 第一版夹具就是这么写的，
#    变异测试（把切法改成那条错规则）**照样通过**，等于什么都没测。
#    夹在中间才会让后半段行落进那个假子表，而结果是数据静默变少、不报错。
CONTENT = "\n".join([
    "甲子表",
    "|企业|进度|需求提报日期| |",
    "|---|---|---|---|",
    "|A公司|制作中|2026/6/25||",
    "![](https://wdcdn.qpic.cn/abc?w=100&h=50)",      # ← 夹在两行数据中间
    "|B公司|已交付|2026/7/1||",
    "乙子表",
    "|企业|启动优化时间|",
    "|---|---|",
    "|C公司|已启动;（6.12启动优化）|",
])


def envelope(body: dict) -> str:
    """wecom-cli 的三层信封：进程 stdout → JSON-RPC → content[0].text 字符串。"""
    return json.dumps({
        "id": "x", "jsonrpc": "2.0",
        "result": {"content": [{"text": json.dumps(body, ensure_ascii=False),
                                "type": "text"}],
                   "isError": False},
    })


class FakeProc:
    def __init__(self, stdout: str, *, stderr: str = "", returncode: int = 0):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def run_with(bodies):
    """
    把一串响应体按调用顺序喂给 subprocess.run；返回记录下来的 cmd 列表。

    🔴 响应用完就抛错，**不许悄悄补一个默认响应**。补默认值的话，
       一个「没有上限的轮询循环」会一直拿到默认响应而**永远转下去**——
       测试挂死而不是失败。挂死的测试比红的测试难查得多，
       而这一组恰恰就是在守「不许挂死」。
    """
    calls = []
    seq = list(bodies)

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if not seq:
            raise AssertionError(
                f"第 {len(calls)} 次调用时假响应已用完 —— "
                f"被测代码调用次数超出预期（可能是循环没有上限）")
        return FakeProc(envelope(seq.pop(0)))

    return calls, fake_run


class Base(unittest.TestCase):
    def setUp(self):
        wecom_doc.clear_cache()
        self.addCleanup(wecom_doc.clear_cache)


class WhitelistTest(Base):
    """🔴 wecom-cli doc 底下有一大批能改能删的命令，白名单只有两个只读的。"""

    def test_rejects_every_write_command(self):
        for bad in ("sheet_update_range_data", "sheet_append_data",
                    "sheet_delete_sub", "smartsheet_delete_records",
                    "smartsheet_delete_fields", "smartsheet_delete_sheet",
                    "create_doc", "edit_doc_content",
                    "smartsheet_add_records", "smartsheet_update_records"):
            with self.subTest(bad=bad):
                with self.assertRaises(LedgerError) as cm:
                    wecom_doc._run_cli(bad, {})
                self.assertIn("只允许调用只读子命令", str(cm.exception))

    def test_whitelist_is_exactly_two_read_commands(self):
        self.assertEqual(sorted(wecom_doc.ALLOWED_SUBCOMMANDS),
                         ["get_doc_content", "sheet_get_info"])

    def test_whitelist_is_a_literal_frozenset_not_from_config(self):
        """白名单必须写死在源码里，不许从配置读 —— 配置是可以被改的。"""
        tree = ast.parse((ROOT / "scripts" / "wecom_doc.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign)
                    and any(getattr(t, "id", "") == "ALLOWED_SUBCOMMANDS"
                            for t in node.targets)):
                # 必须是 frozenset({字面量, 字面量})：没有变量、没有 .get()、
                # 没有拼接。用 AST 判而不是搜字符串 —— 命令名里本来就带 "get"。
                value = node.value
                self.assertIsInstance(value, ast.Call)
                self.assertEqual(getattr(value.func, "id", None), "frozenset")
                self.assertEqual(len(value.args), 1)
                self.assertIsInstance(value.args[0], ast.Set)
                for member in value.args[0].elts:
                    self.assertIsInstance(member, ast.Constant, ast.unparse(member))
                    self.assertIsInstance(member.value, str)
                return
        self.fail("源码里找不到 ALLOWED_SUBCOMMANDS 的字面量赋值")


class ErrcodeTest(Base):
    """
    🔴 本模块唯一真正的验收点：非零 errcode 必须抛错，绝不返回空表。

    外层 isError 恒为 false，错误只在第三层。漏看就是把「没权限」
    读成「今天没有要催的」—— 而那正是授权过期时的表现。
    """

    def _read(self, body):
        calls, fake = run_with([body])
        with mock.patch.object(wecom_doc, "wecom_cli_bin", return_value=EXE), \
             mock.patch.object(wecom_doc.subprocess, "run", fake):
            return wecom_doc.doc_info("https://doc.weixin.qq.com/sheet/x")

    def test_851008_no_authorization_raises(self):
        with self.assertRaises(LedgerError) as cm:
            self._read({"errcode": 851008, "errmsg": "partial no authorization"})
        msg = str(cm.exception)
        self.assertIn("851008", msg)
        self.assertIn("这不是「今天没有要催的」", msg)
        self.assertIn("获取成员文档内容", msg, "要说清怎么修")

    def test_any_nonzero_errcode_raises(self):
        for code in (851002, 851003, 40001, -1):
            with self.subTest(code=code):
                with self.assertRaises(LedgerError):
                    self._read({"errcode": code, "errmsg": "boom"})

    def test_851003_explains_robot_object_permission(self):
        with self.assertRaises(LedgerError) as cm:
            self._read({"errcode": 851003, "errmsg": "no authority"})
        msg = str(cm.exception)
        self.assertIn("对象权限", msg)
        self.assertIn("不会继承业务人员", msg)
        self.assertIn("重新扫码", msg)

    def test_851002_explains_link_or_document_type(self):
        with self.assertRaises(LedgerError) as cm:
            self._read({"errcode": 851002, "errmsg": "unsupported"})
        msg = str(cm.exception)
        self.assertIn("链接或文档类型不兼容", msg)
        self.assertIn("完整 URL", msg)
        self.assertNotIn("sheet_id", msg)

    def test_851002_from_content_does_not_blame_sheet_id(self):
        calls, fake = run_with([{"errcode": 851002, "errmsg": "unsupported"}])
        with mock.patch.object(wecom_doc, "wecom_cli_bin", return_value=EXE), \
             mock.patch.object(wecom_doc.subprocess, "run", fake):
            with self.assertRaises(LedgerError) as cm:
                wecom_doc.doc_content("https://doc.weixin.qq.com/sheet/x")
        msg = str(cm.exception)
        self.assertIn("正文读取接口不兼容", msg)
        self.assertIn("不要修改 sheet_id", msg)
        self.assertNotIn("重新核对 sheet_id", msg)

    def test_errcode_zero_passes(self):
        self.assertEqual(self._read(INFO)["name"], "某需求记录")

    def test_outer_iserror_false_does_not_whitewash(self):
        """外层说没错、里层 errcode 非零 —— 必须以里层为准。"""
        calls, fake = run_with([{"errcode": 851008, "errmsg": "nope"}])
        with mock.patch.object(wecom_doc, "wecom_cli_bin", return_value=EXE), \
             mock.patch.object(wecom_doc.subprocess, "run", fake):
            with self.assertRaises(LedgerError):
                wecom_doc.doc_info("https://doc.weixin.qq.com/sheet/x")


class SensitiveErrorTest(Base):
    """任何企微 CLI 错误都可能被写日志、告警和 health.json，必须先脱敏。"""

    SECRET = "SENSITIVE_TEST_TOKEN"

    def assert_redacted(self, callback):
        with self.assertRaises(LedgerError) as cm:
            callback()
        message = str(cm.exception)
        self.assertNotIn(self.SECRET, message)
        self.assertIn("***", message)

    def test_raw_process_error_redacts_query_token(self):
        proc = FakeProc("", returncode=1,
                        stderr="network error: https://example.invalid/?share_code="
                               + self.SECRET)
        with mock.patch.object(wecom_doc, "wecom_cli_bin", return_value=EXE), \
             mock.patch.object(wecom_doc.subprocess, "run", return_value=proc):
            self.assert_redacted(lambda: wecom_doc._run_cli("sheet_get_info", {}))

    def test_outer_json_error_redacts_assignment_token(self):
        proc = FakeProc(json.dumps({"error": "access_token=" + self.SECRET}))
        with mock.patch.object(wecom_doc, "wecom_cli_bin", return_value=EXE), \
             mock.patch.object(wecom_doc.subprocess, "run", return_value=proc):
            self.assert_redacted(lambda: wecom_doc._run_cli("sheet_get_info", {}))

    def test_inner_errmsg_redacts_token(self):
        calls, fake = run_with([{"errcode": 40001,
                                 "errmsg": "Bearer " + self.SECRET}])
        with mock.patch.object(wecom_doc, "wecom_cli_bin", return_value=EXE), \
             mock.patch.object(wecom_doc.subprocess, "run", fake):
            self.assert_redacted(
                lambda: wecom_doc.doc_info("https://doc.weixin.qq.com/sheet/x"))

    def test_polling_timeout_redacts_source_url(self):
        calls, fake = run_with([{"errcode": 0, "task_done": False}])
        url = "https://doc.weixin.qq.com/sheet/x?apikey=" + self.SECRET
        with mock.patch.object(wecom_doc, "wecom_cli_bin", return_value=EXE), \
             mock.patch.object(wecom_doc.subprocess, "run", fake), \
             mock.patch.object(wecom_doc, "POLL_MAX", 1):
            self.assert_redacted(lambda: wecom_doc.doc_content(url))


class PollingTest(Base):
    URL = "https://doc.weixin.qq.com/sheet/poll"

    def test_polls_until_task_done(self):
        bodies = [
            {"errcode": 0, "task_id": "t1", "task_done": False},
            {"errcode": 0, "task_id": "t1", "task_done": False},
            {"errcode": 0, "task_done": True, "content": "OK"},
        ]
        calls, fake = run_with(bodies)
        with mock.patch.object(wecom_doc, "wecom_cli_bin", return_value=EXE), \
             mock.patch.object(wecom_doc.subprocess, "run", fake), \
             mock.patch.object(wecom_doc.time, "sleep"):
            self.assertEqual(wecom_doc.doc_content(self.URL), "OK")
        self.assertEqual(len(calls), 3)
        # 续轮询必须把 task_id 带上，否则每次都是新任务、永远不 done
        self.assertIn("t1", calls[1][-1])

    def test_polling_gives_up_instead_of_hanging(self):
        """🔴 cron 里挂死比读不到更糟：它连「失败」都表现不出来。"""
        never = [{"errcode": 0, "task_id": "t", "task_done": False}] * 40
        calls, fake = run_with(never)
        with mock.patch.object(wecom_doc, "wecom_cli_bin", return_value=EXE), \
             mock.patch.object(wecom_doc.subprocess, "run", fake), \
             mock.patch.object(wecom_doc.time, "sleep"):
            with self.assertRaises(LedgerError) as cm:
                wecom_doc.doc_content(self.URL)
        self.assertIn("这不是「今天没有要催的」", str(cm.exception))
        self.assertLessEqual(len(calls), wecom_doc.POLL_MAX + 1)


class CacheTest(Base):
    """美誉度那份要供 AI体检、GEO 两个台账用；轮询很慢，不缓存等于每天多跑一遍。"""

    def test_same_url_fetched_once(self):
        calls, fake = run_with([INFO, {"errcode": 0, "task_done": True, "content": CONTENT},
                                INFO, {"errcode": 0, "task_done": True, "content": CONTENT}])
        with mock.patch.object(wecom_doc, "wecom_cli_bin", return_value=EXE), \
             mock.patch.object(wecom_doc.subprocess, "run", fake):
            wecom_doc.read_sheet("https://doc.weixin.qq.com/sheet/m", "w4q62o")
            wecom_doc.read_sheet("https://doc.weixin.qq.com/sheet/m", "BB08J2")
        self.assertEqual(len(calls), 2, f"同一份文档只该取一次，实际调了 {len(calls)} 次")


class ParseTest(Base):
    def _sheet(self, sheet_id):
        calls, fake = run_with([INFO, {"errcode": 0, "task_done": True, "content": CONTENT}])
        with mock.patch.object(wecom_doc, "wecom_cli_bin", return_value=EXE), \
             mock.patch.object(wecom_doc.subprocess, "run", fake):
            return wecom_doc.read_sheet("https://doc.weixin.qq.com/sheet/m", sheet_id)

    def test_header_and_rows(self):
        s = self._sheet("w4q62o")
        self.assertEqual(s.header[:3], ["企业", "进度", "需求提报日期"])
        self.assertEqual(s.data_rows, [1, 2])
        self.assertEqual(s.text(1, "企业"), "A公司")
        self.assertEqual(s.text(1, "进度"), "制作中")
        self.assertEqual(s.date(1, "需求提报日期"), date(2026, 6, 25))

    def test_image_line_inside_a_sheet_does_not_truncate_it(self):
        """
        🔴 独立成行的图片 `![](...)` 不能被当成子表标题。

        夹具里图片就夹在甲子表的两行数据中间。用「不以 | 开头就是新子表」
        那条错规则去切，B公司那行会落进一个叫 `![](...)` 的假子表，
        甲子表就只剩 1 行 —— **数据静默变少，不报错**。
        """
        s = self._sheet("w4q62o")
        self.assertEqual(s.data_rows, [1, 2], "图片后面那行不能丢")
        self.assertEqual(s.text(2, "企业"), "B公司")

    def test_sheet_after_an_image_is_still_found(self):
        s = self._sheet("BB08J2")
        self.assertEqual(s.data_rows, [1])
        self.assertEqual(s.text(1, "启动优化时间"), "已启动;（6.12启动优化）")

    def test_separator_row_is_not_data(self):
        self.assertEqual(len(self._sheet("w4q62o").data_rows), 2)

    def test_empty_trailing_columns_are_ignored(self):
        s = self._sheet("w4q62o")
        self.assertFalse(s.has_column(""))
        self.assertTrue(s.has_column("企业"))

    def test_missing_sheet_id_raises_with_the_real_list(self):
        with self.assertRaises(LedgerError) as cm:
            self._sheet("不存在")
        self.assertIn("w4q62o", str(cm.exception), "要告诉人现有的是哪些")

    def test_undated_string_is_none_not_a_guess(self):
        """
        🔴 GEO 写的是 `6.12`，没有年份。猜一个年份出来会在跨年时静默算错，
           而催办天数错了是看不出来的。认不出就返回 None，不编。
        """
        for bad in ("6.12", "7.9有成效", "/", "优化中", ""):
            with self.subTest(bad=bad):
                self.assertIsNone(wecom_doc._cell_date(bad))

    def test_duplicate_columns_are_reported(self):
        s = wecom_doc.Sheet(["企业", "进度", "企业"], {})
        self.assertEqual(s.duplicate_columns, ["企业"])


class ChildEnvTest(Base):
    """wecom-cli 同样是 #!/usr/bin/env node —— rc2/rc4 那个坑会原样重现。"""

    def test_agent_context_vars_are_stripped(self):
        with mock.patch.dict("os.environ", {"HERMES_HOME": "/h", "HERMES_EXEC_ASK": "1"}):
            env = wecom_doc._child_env(EXE)
        self.assertNotIn("HERMES_HOME", env)
        self.assertNotIn("HERMES_EXEC_ASK", env)

    def test_child_path_puts_the_exe_dir_first(self):
        self.assertTrue(wecom_doc._child_env(EXE)["PATH"].startswith("/opt/fake/bin"))

    def test_shares_the_one_list_with_lark(self):
        """两条线共用同一份清单 —— 分开维护迟早只更新一处。"""
        import cli_env
        import lark_base
        self.assertIs(lark_base.AGENT_CONTEXT_VARS, cli_env.AGENT_CONTEXT_VARS)


class MissingCliTest(Base):
    def test_message_tells_a_non_developer_what_to_do(self):
        with mock.patch.object(wecom_doc, "wecom_cli_bin", return_value=None):
            with self.assertRaises(LedgerError) as cm:
                wecom_doc.doc_info("https://doc.weixin.qq.com/sheet/x")
        msg = str(cm.exception)
        self.assertIn("~/.local/bin", msg)
        self.assertIn("定时任务能看到的目录比你终端里少", msg)
        self.assertNotIn("npm install", msg.split("已经找过")[0].replace("wecom-cli", ""))


class DispatchTest(Base):
    def test_core_routes_wecom_doc_source(self):
        with mock.patch.object(wecom_doc, "read_sheet", return_value="SHEET") as rs:
            got = core.read_ledger_sheet(
                {"source": "wecom_doc", "url": "U", "sheet_id": "S"})
        self.assertEqual(got, "SHEET")
        rs.assert_called_once_with("U", "S")

    def test_unknown_source_still_raises(self):
        with self.assertRaises(LedgerError):
            core.read_ledger_sheet({"source": "什么鬼", "name": "X"})


if __name__ == "__main__":
    unittest.main()
