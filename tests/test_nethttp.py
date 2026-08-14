#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TLS 1.3 被中间层搞坏时，必须自己降到 1.2 —— 而且要降得可见、可恢复。

═══════════════════════════════════════════════════════════════════════
2026-08-14 真实故障：业务电脑必须常开代理，代理把所有 TLS 1.3 记录搞坏
（腾讯文档 / 企微文档 / 企微推送 / 飞书 / Google 全断，TLS 1.2 全通）。
当天读数正常但**推送 0/1 条失败，业务没收到清单**。

这里钉住四件会静默出错的事：
  ① 降级真的发生（否则推送永远失败，而失败长得像「网络不好」）
  ② 降级只在 TLS 失败时发生（否则会把超时、HTTP 错误也吞掉重试）
  ③ 降级失败要抛**原来那个**异常（否则真实原因被替换成第二次的错）
  ④ 两个调用点真的接上了（只写模块不接线 == 没写）
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import ssl
import unittest
import urllib.error
from io import StringIO
from unittest import mock

from harness import ledgers_cfg          # noqa: F401  把 scripts/ 挂上 sys.path

import nethttp


class FakeResp:
    def __init__(self, tag="ok"):
        self.tag = tag

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _max_version(call):
    """从一次 urlopen 调用里取出它用的 SSLContext 的版本上限。"""
    ctx = call.kwargs.get("context")
    return None if ctx is None else ctx.maximum_version


class FallbackTest(unittest.TestCase):

    def setUp(self):
        # 模块级粘滞标志是进程内共享的，每条用例都要归零，
        # 否则用例之间会互相污染，且**顺序一变结论就变**。
        nethttp._degraded = False
        self.addCleanup(setattr, nethttp, "_degraded", False)

    def test_tls_failure_falls_back_to_tls12_and_succeeds(self):
        """🔴 不降级 == 推送永远发不出去，而且长得像普通网络故障。"""
        good = FakeResp()
        with mock.patch.object(
            nethttp.urllib.request, "urlopen",
            side_effect=[ssl.SSLError("[SSL: SSLV3_ALERT_BAD_RECORD_MAC] bad"),
                         good],
        ) as uo:
            out = StringIO()
            got = nethttp.urlopen("REQ", timeout=9, stream=out)

        self.assertIs(got, good)
        self.assertEqual(uo.call_count, 2)
        # 第一次不带 context（走默认，可能是 1.3）；第二次必须封顶到 1.2
        self.assertIsNone(_max_version(uo.call_args_list[0]))
        self.assertEqual(_max_version(uo.call_args_list[1]),
                         ssl.TLSVersion.TLSv1_2)
        self.assertTrue(nethttp.degraded())

    def test_fallback_is_announced_not_silent(self):
        """降级是「网络坏了但我扛住了」，不说出来就没人会去修网络。"""
        with mock.patch.object(
            nethttp.urllib.request, "urlopen",
            side_effect=[ssl.SSLError("boom"), FakeResp()],
        ):
            out = StringIO()
            nethttp.urlopen("REQ", timeout=9, stream=out)
        said = out.getvalue()
        self.assertIn("TLS 1.2", said)
        self.assertIn("代理", said, "要指向真正的原因，不能只说「已降级」")

    def test_fallback_still_verifies_certificates(self):
        """
        🔴 降级只许封顶协议版本，**不许顺手关掉证书校验**。
        关掉了不会报错、请求照样成功 —— 而中间人就此畅通无阻。
        """
        ctx = nethttp._tls12_context()
        self.assertTrue(ctx.check_hostname)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(ctx.maximum_version, ssl.TLSVersion.TLSv1_2)

    def test_degradation_sticks_within_the_process(self):
        """
        9 份台账各自带重试，每个请求都先赔一次失败的 1.3 握手是纯浪费。
        降级后应直接用 1.2。
        """
        with mock.patch.object(
            nethttp.urllib.request, "urlopen",
            side_effect=[ssl.SSLError("boom"), FakeResp(), FakeResp()],
        ) as uo:
            nethttp.urlopen("REQ", timeout=9, stream=StringIO())
            nethttp.urlopen("REQ2", timeout=9, stream=StringIO())

        self.assertEqual(uo.call_count, 3)   # 1 失败 + 1 降级 + 1 直接降级
        self.assertEqual(_max_version(uo.call_args_list[2]),
                         ssl.TLSVersion.TLSv1_2)

    def test_non_tls_errors_are_not_retried(self):
        """
        🔴 超时 / HTTP 错误不是传输层握手问题。在这里重试会让
        调用方原有的重试次数悄悄翻倍，也会掩盖真实错误。
        """
        for err in (urllib.error.HTTPError("u", 403, "no", {}, None),
                    TimeoutError("timed out")):
            with self.subTest(err=type(err).__name__):
                nethttp._degraded = False
                with mock.patch.object(nethttp.urllib.request, "urlopen",
                                       side_effect=err) as uo:
                    with self.assertRaises(type(err)):
                        nethttp.urlopen("REQ", timeout=9, stream=StringIO())
                self.assertEqual(uo.call_count, 1)
                self.assertFalse(nethttp.degraded())

    def test_when_fallback_also_fails_the_original_error_wins(self):
        """
        🔴 报第二次的错会把「TLS 1.3 被搞坏」换成「连不上」，
        排查方向整个偏掉 —— 当天就是靠原始错误码定位到代理的。
        """
        first = ssl.SSLError("[SSL: SSLV3_ALERT_BAD_RECORD_MAC] 原始原因")
        with mock.patch.object(nethttp.urllib.request, "urlopen",
                               side_effect=[first, OSError("网络不可达")]):
            with self.assertRaises(ssl.SSLError) as caught:
                nethttp.urlopen("REQ", timeout=9, stream=StringIO())
        self.assertIn("原始原因", str(caught.exception))
        self.assertFalse(nethttp.degraded(), "没降级成功就不该标记为已降级")


class WiredUpTest(unittest.TestCase):
    """
    🔴 只写模块不接线 == 没写，而且不报错。
       断言打在**源码**上，因为这两处都埋在网络调用里，
       单测走不到、真跑又要联网。
    """

    def _src(self, name):
        from pathlib import Path
        p = Path(nethttp.__file__).parent / name
        return p.read_text(encoding="utf-8")

    def test_both_call_sites_go_through_nethttp(self):
        for name in ("qqdoc.py", "wecom_push.py"):
            with self.subTest(name=name):
                src = self._src(name)
                self.assertIn("nethttp.urlopen(", src,
                              f"{name} 没有走 nethttp，TLS 一坏就整条线失败")
                self.assertNotIn("urllib.request.urlopen(", src,
                                 f"{name} 还留着直连的 urlopen —— "
                                 f"同一件事两份实现，改了一处忘另一处")


if __name__ == "__main__":
    unittest.main()
