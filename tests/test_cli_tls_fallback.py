#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
外部 CLI（lark-cli / wecom-cli）撞上坏掉的 TLS 1.3 时，也要能降到 1.2。

═══════════════════════════════════════════════════════════════════════
🔴 2026-08-14 只修了 Python 侧（scripts/nethttp.py）就以为扛住了。
   两个 CLI 都是 Node、**有各自的 TLS 栈**，nethttp 管不到它们 ——
   当天 Node 侧仍然打挂一次生产运行：
   「AI哨兵前期台账：lark-cli 调用失败：remote error: tls: bad record MAC」，
   退出码 1、九份台账少读一份。

   **修完一半比没修更危险**：Python 侧全绿会让人以为整件事结束了。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import unittest
from io import StringIO
from unittest import mock

from harness import core  # noqa: F401 —— 挂 sys.path

import cli_env
import lark_base
import wecom_doc
from qqdoc import LedgerError


class DetectionTest(unittest.TestCase):

    def test_recognises_the_exact_production_error(self):
        self.assertTrue(cli_env.looks_like_tls_failure(
            'lark-cli 调用失败（+record-list）：API call failed: '
            'Get "https://open.feishu.cn/...": remote error: tls: bad record MAC'))

    def test_recognises_other_wordings(self):
        for s in ("SSLError: bad record mac", "TLS handshake failure",
                  "remote error: tls: internal error"):
            with self.subTest(s=s):
                self.assertTrue(cli_env.looks_like_tls_failure(s))

    def test_ordinary_failures_are_not_tls(self):
        """
        🔴 认错了会把「凭证失效」「文档没权限」当成网络抖动重试掉，
           而那两条各自有明确的处理动作。
        """
        for s in ("errcode=851014 authorization expired",
                  "找不到 lark-cli", "errcode=640002 文档不存在",
                  "调用超时（120s）"):
            with self.subTest(s=s):
                self.assertFalse(cli_env.looks_like_tls_failure(s))


class ChildEnvTest(unittest.TestCase):

    def setUp(self):
        cli_env.reset_tls()
        self.addCleanup(cli_env.reset_tls)

    def test_no_node_flag_before_degrading(self):
        env = cli_env.child_env("/usr/bin/true")
        self.assertNotIn("--tls-max-v1.2", env.get("NODE_OPTIONS", ""))

    def test_node_flag_after_degrading(self):
        cli_env.mark_tls_degraded(stream=StringIO())
        env = cli_env.child_env("/usr/bin/true")
        self.assertIn("--tls-max-v1.2", env["NODE_OPTIONS"])

    def test_existing_node_options_are_kept(self):
        cli_env.mark_tls_degraded(stream=StringIO())
        with mock.patch.dict("os.environ", {"NODE_OPTIONS": "--max-old-space-size=512"}):
            env = cli_env.child_env("/usr/bin/true")
        self.assertIn("--max-old-space-size=512", env["NODE_OPTIONS"])
        self.assertIn("--tls-max-v1.2", env["NODE_OPTIONS"])

    def test_marking_twice_only_announces_once(self):
        out = StringIO()
        self.assertTrue(cli_env.mark_tls_degraded(stream=out))
        self.assertFalse(cli_env.mark_tls_degraded(stream=out))
        self.assertEqual(out.getvalue().count("TLS 1.2"), 1)


class RetryTest(unittest.TestCase):
    """两个适配层都要真的接上 —— 只改 cli_env 不接线等于没改。"""

    def setUp(self):
        cli_env.reset_tls()
        self.addCleanup(cli_env.reset_tls)

    TLS_ERR = LedgerError("remote error: tls: bad record MAC")

    def _check(self, mod, once_name, call):
        with mock.patch.object(mod, once_name,
                               side_effect=[self.TLS_ERR, {"ok": 1}]) as once:
            got = call()
        self.assertEqual(got, {"ok": 1})
        self.assertEqual(once.call_count, 2, "TLS 失败后没有重试")
        self.assertTrue(cli_env.tls_degraded())

    def test_lark_retries_once_on_tls_failure(self):
        self._check(lark_base, "_run_cli_once",
                    lambda: lark_base._run_cli("+table-list", []))

    def test_wecom_retries_once_on_tls_failure(self):
        self._check(wecom_doc, "_run_cli_once",
                    lambda: wecom_doc._run_cli("sheet_get_info", {}))

    def test_non_tls_failures_are_not_retried(self):
        """🔴 授权过期重试一次也没用，只会把一条明确的故障拖成两次超时。"""
        err = LedgerError("errcode=851014 authorization expired")
        with mock.patch.object(lark_base, "_run_cli_once",
                               side_effect=err) as once:
            with self.assertRaises(LedgerError):
                lark_base._run_cli("+table-list", [])
        self.assertEqual(once.call_count, 1)
        self.assertFalse(cli_env.tls_degraded())

    def test_already_degraded_does_not_retry_again(self):
        """降级后仍然失败，就是真失败 —— 不许每次调用都翻倍重试。"""
        cli_env.mark_tls_degraded(stream=StringIO())
        with mock.patch.object(lark_base, "_run_cli_once",
                               side_effect=self.TLS_ERR) as once:
            with self.assertRaises(LedgerError):
                lark_base._run_cli("+table-list", [])
        self.assertEqual(once.call_count, 1)


if __name__ == "__main__":
    unittest.main()
