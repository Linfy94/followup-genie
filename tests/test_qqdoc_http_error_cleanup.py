#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qqdoc._rpc()：HTTPError 分支必须关闭响应对象，不能留给 GC 收。

═══════════════════════════════════════════════════════════════════════
🔴 2026-08-21 复审发现：`except urllib.error.HTTPError as e:` 这两条
   后续路径——记录 last 继续重试、401/403 直接 raise——都从没调用过
   `e.close()`，也没用 `with` 管理它。`HTTPError` 本身是个类文件对象，
   持有底层连接；不主动关，只能等 Python 的垃圾回收器收，长期运行
   （cron 每天调，遇到 851014 这类权限过期会连续重试）会积累一批
   ResourceWarning，占着没释放的连接资源。

   不用真的等 GC 触发 ResourceWarning（那要看 gc 时机，测试会不稳定）：
   直接给 HTTPError 塞一个可跟踪的 fp mock，断言 `.close()` 被调用，
   这是更直接、更快、不依赖 GC 时机的验证方式。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import unittest
import urllib.error
from unittest import mock

from harness import core  # noqa: F401 —— 挂 sys.path

import qqdoc


class HTTPErrorResourceCleanupTest(unittest.TestCase):

    def test_close_called_on_retry(self):
        """
        普通 HTTP 错误（非 401/403）会重试 RETRIES 次。

        🔴 这里只断言「至少关过一次」，不追求精确的调用次数——标准库
        `HTTPError.close()` 本身对同一个实例是幂等的（第一次真正关闭
        底层 fp，之后重复调用不再触发）。测试用 side_effect 复用同一个
        HTTPError 对象来模拟三次重试，这是测试自己的简化，不代表真实
        运行时的重试也一定复用同一个对象；真正要证明的只是「这条路径
        会调用 close()，不再是从来不关」。
        """
        fp = mock.MagicMock()
        err = urllib.error.HTTPError("http://x", 500, "server error", {}, fp)
        with mock.patch.object(qqdoc, "load_token", return_value="tok"), \
             mock.patch.object(qqdoc.nethttp, "fetch", side_effect=err), \
             mock.patch.object(qqdoc.time, "sleep"):  # 不真的等重试间隔
            with self.assertRaises(qqdoc.LedgerError):
                qqdoc._rpc("tools/list")
        fp.close.assert_called()

    def test_close_called_before_401_raises(self):
        """401/403 不重试、直接 raise，但响应体同样不能漏关。"""
        fp = mock.MagicMock()
        err = urllib.error.HTTPError("http://x", 401, "unauthorized", {}, fp)
        with mock.patch.object(qqdoc, "load_token", return_value="tok"), \
             mock.patch.object(qqdoc.nethttp, "fetch", side_effect=err):
            with self.assertRaises(qqdoc.LedgerError):
                qqdoc._rpc("tools/list")
        fp.close.assert_called_once()

    def test_close_called_before_403_raises(self):
        fp = mock.MagicMock()
        err = urllib.error.HTTPError("http://x", 403, "forbidden", {}, fp)
        with mock.patch.object(qqdoc, "load_token", return_value="tok"), \
             mock.patch.object(qqdoc.nethttp, "fetch", side_effect=err):
            with self.assertRaises(qqdoc.LedgerError):
                qqdoc._rpc("tools/list")
        fp.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
