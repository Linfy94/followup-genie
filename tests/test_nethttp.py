#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TLS 1.3 被中间层搞坏时，必须自己降到 1.2 —— 而且要降得可见、可恢复。

═══════════════════════════════════════════════════════════════════════
2026-08-14 真实故障：业务电脑必须常开代理，代理把所有 TLS 1.3 记录搞坏
（腾讯文档 / 企微文档 / 企微推送 / 飞书 / Google 全断，TLS 1.2 全通）。
当天读数正常但**推送 0/1 条失败，业务没收到清单**。

🔴 rc3 的测试只模拟了裸 `ssl.SSLError`，于是「只对了一半」也全绿了。
   实测 `urllib.request.AbstractHTTPHandler.do_open` 只包装 `h.request(...)`
   那一段，同一个 bad record mac 因此有三种长相：

     失败位置            抛出                        rc3
     连接/握手/发请求    URLError(reason=SSLError)   ❌ 漏掉
     读响应头            裸 ssl.SSLError             ✅ 当天恰好命中它
     读响应体            裸 ssl.SSLError（模块外）    ❌ 漏掉

   **三种都要有用例**，否则下次换一种长相又会静默失效，
   而表现依旧是「今天没有要催的」。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import socket
import ssl
import unittest
import urllib.error
from io import StringIO
from unittest import mock

from harness import ledgers_cfg          # noqa: F401  把 scripts/ 挂上 sys.path

import nethttp

BAD_MAC = "[SSL: SSLV3_ALERT_BAD_RECORD_MAC] ssl/tls alert bad record mac"


def handshake_error(msg=BAD_MAC):
    """连接/握手/发请求阶段的真实长相：被 urllib 包装成 URLError。"""
    return urllib.error.URLError(ssl.SSLError(msg))


def response_error(msg=BAD_MAC):
    """读响应阶段的真实长相：裸 SSLError，urllib 不包装。"""
    return ssl.SSLError(msg)


class FakeResp:
    """假响应。body_error 不为 None 时，在 read() 阶段炸 —— 这正是 rc3 漏掉的那种。"""

    def __init__(self, body=b"ok", body_error=None):
        self.body = body
        self.body_error = body_error

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        if self.body_error is not None:
            raise self.body_error
        return self.body


def _max_version(call):
    ctx = call.kwargs.get("context")
    return None if ctx is None else ctx.maximum_version


class PhaseClassifierTest(unittest.TestCase):
    """降级的判据本身。判错一类，整个兜底就静默失效。"""

    def test_wrapped_ssl_error_is_request_phase(self):
        self.assertEqual(nethttp.tls_failure_phase(handshake_error()), "request")

    def test_bare_ssl_error_is_response_phase(self):
        self.assertEqual(nethttp.tls_failure_phase(response_error()), "response")

    def test_http_error_is_not_a_transport_failure(self):
        """
        🔴 HTTPError 是 URLError 的子类，必须先判掉。
           漏判会把 401/403 这类凭证失效当成网络抖动重试，
           而 qqdoc 正靠 403 分支报「请重新扫码授权」。
        """
        err = urllib.error.HTTPError("u", 403, "forbidden", {}, None)
        self.assertIsNone(nethttp.tls_failure_phase(err))

    def test_non_ssl_url_errors_are_left_alone(self):
        """DNS 挂了、连接被拒，降到 1.2 也没用，重试只是白等一轮。"""
        for reason in (socket.gaierror("name resolution"),
                       ConnectionRefusedError("refused"),
                       TimeoutError("timed out")):
            with self.subTest(reason=type(reason).__name__):
                self.assertIsNone(
                    nethttp.tls_failure_phase(urllib.error.URLError(reason)))

    def test_plain_timeout_is_left_alone(self):
        self.assertIsNone(nethttp.tls_failure_phase(TimeoutError("timed out")))


class FallbackTest(unittest.TestCase):

    def setUp(self):
        # 模块级粘滞标志是进程内共享的，每条用例都要归零，
        # 否则用例之间互相污染，且**顺序一变结论就变**。
        nethttp.reset()
        self.addCleanup(nethttp.reset)

    def _run(self, side_effect, *, idempotent=True):
        out = StringIO()
        with mock.patch.object(nethttp.urllib.request, "urlopen",
                               side_effect=side_effect) as uo:
            data = nethttp.fetch("REQ", timeout=9,
                                 idempotent=idempotent, stream=out)
        return data, uo, out.getvalue()

    def test_handshake_phase_failure_falls_back(self):
        """🔴 rc3 漏的就是这一种 —— 而它才是握手真正失败时的长相。"""
        data, uo, said = self._run([handshake_error(), FakeResp(b"payload")])
        self.assertEqual(data, b"payload")
        self.assertEqual(uo.call_count, 2)
        self.assertIsNone(_max_version(uo.call_args_list[0]))
        self.assertEqual(_max_version(uo.call_args_list[1]),
                         ssl.TLSVersion.TLSv1_2)
        self.assertTrue(nethttp.degraded())
        self.assertIn("TLS 1.2", said)

    def test_response_header_phase_failure_falls_back(self):
        """2026-08-14 当天真实命中的那一种。"""
        data, uo, _ = self._run([response_error(), FakeResp(b"payload")])
        self.assertEqual(data, b"payload")
        self.assertTrue(nethttp.degraded())

    def test_body_read_failure_falls_back(self):
        """
        🔴 rc3 把 read() 留在调用方，读到一半断掉既不降级，
           也只表现成调用方的普通重试失败。
        """
        data, uo, _ = self._run(
            [FakeResp(body_error=response_error()), FakeResp(b"payload")])
        self.assertEqual(data, b"payload")
        self.assertEqual(uo.call_count, 2)
        self.assertEqual(_max_version(uo.call_args_list[1]),
                         ssl.TLSVersion.TLSv1_2)
        self.assertTrue(nethttp.degraded())

    def test_fallback_is_announced_not_silent(self):
        _, _, said = self._run([handshake_error(), FakeResp()])
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

    def test_sticky_skips_the_doomed_tls13_attempt(self):
        """9 份台账各自带重试，每个请求都先赔一次失败握手是纯浪费。"""
        with mock.patch.object(nethttp.urllib.request, "urlopen",
                               side_effect=[handshake_error(), FakeResp(),
                                            FakeResp()]) as uo:
            nethttp.fetch("A", timeout=9, idempotent=True, stream=StringIO())
            nethttp.fetch("B", timeout=9, idempotent=True, stream=StringIO())
        self.assertEqual(uo.call_count, 3)   # 1 失败 + 1 降级 + 1 直接降级
        self.assertEqual(_max_version(uo.call_args_list[2]),
                         ssl.TLSVersion.TLSv1_2)

    def test_non_tls_errors_are_not_retried(self):
        """
        🔴 超时 / HTTP 错误不是传输层握手问题。在这里重试会让
           调用方原有的重试次数悄悄翻倍，也会掩盖真实错误。
        """
        for err in (urllib.error.HTTPError("u", 403, "no", {}, None),
                    urllib.error.URLError(socket.gaierror("dns")),
                    TimeoutError("timed out")):
            with self.subTest(err=type(err).__name__):
                nethttp.reset()
                with mock.patch.object(nethttp.urllib.request, "urlopen",
                                       side_effect=err) as uo:
                    with self.assertRaises(type(err)):
                        nethttp.fetch("REQ", timeout=9, idempotent=True,
                                      stream=StringIO())
                self.assertEqual(uo.call_count, 1)
                self.assertFalse(nethttp.degraded())

    def test_when_fallback_also_fails_the_original_error_wins(self):
        """
        🔴 报第二次的错会把「TLS 1.3 被搞坏」换成「连不上」，
           排查方向整个偏掉 —— 当天就是靠原始错误码定位到代理的。
        """
        first = handshake_error("原始原因")
        with mock.patch.object(nethttp.urllib.request, "urlopen",
                               side_effect=[first, OSError("网络不可达")]):
            with self.assertRaises(urllib.error.URLError) as caught:
                nethttp.fetch("REQ", timeout=9, idempotent=True,
                              stream=StringIO())
        self.assertIn("原始原因", str(caught.exception))
        self.assertFalse(nethttp.degraded(), "没降级成功就不该标记为已降级")


class DuplicateSendWarningTest(unittest.TestCase):
    """
    发出去就收不回的调用（企微推送），响应阶段失败后重试有重复风险。

    本项目的取舍是「宁可重复，也不静默漏催」，所以仍然重试 ——
    但必须说出来，业务真收到两条时原因要在日志里查得到。
    """

    def setUp(self):
        nethttp.reset()
        self.addCleanup(nethttp.reset)

    def _said(self, side_effect, idempotent):
        out = StringIO()
        with mock.patch.object(nethttp.urllib.request, "urlopen",
                               side_effect=side_effect):
            nethttp.fetch("REQ", timeout=9, idempotent=idempotent, stream=out)
        return out.getvalue()

    def test_non_idempotent_response_phase_retry_warns_about_duplicates(self):
        said = self._said([response_error(), FakeResp()], idempotent=False)
        self.assertIn("重复", said)

    def test_non_idempotent_request_phase_retry_does_not_warn(self):
        """请求阶段失败＝没发完整，服务端不可能处理过，不该吓唬人。"""
        said = self._said([handshake_error(), FakeResp()], idempotent=False)
        self.assertNotIn("重复", said)

    def test_idempotent_calls_never_warn(self):
        said = self._said([response_error(), FakeResp()], idempotent=True)
        self.assertNotIn("重复", said)


class WiredUpTest(unittest.TestCase):
    """
    🔴 只写模块不接线 == 没写，而且不报错。
       断言打在**源码**上，因为这两处都埋在网络调用里，
       单测走不到、真跑又要联网。
    """

    def _src(self, name):
        from pathlib import Path
        return (Path(nethttp.__file__).parent / name).read_text(encoding="utf-8")

    def test_both_call_sites_go_through_nethttp(self):
        for name in ("qqdoc.py", "wecom_push.py"):
            with self.subTest(name=name):
                src = self._src(name)
                self.assertIn("nethttp.fetch(", src,
                              f"{name} 没有走 nethttp，TLS 一坏就整条线失败")
                self.assertNotIn("urllib.request.urlopen(", src,
                                 f"{name} 还留着直连的 urlopen —— "
                                 f"同一件事两份实现，改了一处忘另一处")

    def test_push_declares_itself_non_idempotent(self):
        """🔴 推送标成幂等，就等于默许「响应阶段失败后闷声重发」。"""
        self.assertIn("idempotent=False", self._src("wecom_push.py"))

    def test_reads_declare_themselves_idempotent(self):
        self.assertIn("idempotent=True", self._src("qqdoc.py"))


if __name__ == "__main__":
    unittest.main()
