#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wecom_push._post()：HTTPError 分支必须关闭响应对象，不能留给 GC 收。

═══════════════════════════════════════════════════════════════════════
🔴 2026-08-21 复审发现：跟 qqdoc.py 是同一个坑，只是换了一个模块——
   `except urllib.error.HTTPError as e: return False, ...` 从没调用过
   `e.close()`。cron 每天调，遇到 webhook 失效这类会反复重试，长期
   攒下来一堆 ResourceWarning、占着没释放的连接资源。

   跟 test_qqdoc_http_error_cleanup.py 同一个验证思路：不等 GC 触发
   ResourceWarning（时机不稳定），直接给 HTTPError 塞一个可跟踪的 fp
   mock，断言 `.close()` 被调用。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import unittest
import urllib.error
from unittest import mock

from harness import core  # noqa: F401 —— 挂 sys.path

import wecom_push


class HTTPErrorResourceCleanupTest(unittest.TestCase):

    def test_close_called_on_http_error(self):
        fp = mock.MagicMock()
        err = urllib.error.HTTPError("http://x", 500, "server error", {}, fp)
        with mock.patch.object(wecom_push.nethttp, "fetch", side_effect=err):
            ok, msg = wecom_push._post("http://x", {"msgtype": "text"})
        self.assertFalse(ok)
        self.assertIn("500", msg)
        fp.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
