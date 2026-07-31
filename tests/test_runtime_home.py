#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
运行时目录的解析：`FOLLOWUP_HOME` > `HERMES_HOME` > 平台默认。

为什么加 FOLLOWUP_HOME：这个包要能装在 WorkBuddy、launchd、裸 crontab 上，
而在那些地方让人设一个叫 `HERMES_HOME` 的变量是明确的误导 ——
他们没装 Hermes，也不该为了跑这个工具去理解 Hermes 是什么。

🔴 这套优先级在**四个地方**各写了一遍，必须完全一致：
     scripts/core.py        hermes_home()
     scripts/qqdoc.py       _hermes_home()   ← 刻意重复，避免循环导入
     scripts/setup.sh       shell 里的三级判断
     scripts/install.sh     写出去的 cron shim
   不一致的后果特别难查：取数找得到凭证、判定找不到配置，
   或者「cron 跑成功了但配置没生效」。本文件守住前两个（Python 侧）。
"""

from __future__ import annotations

import os
import unittest
from contextlib import contextmanager
from pathlib import Path

from harness import temp_home, make_sheet, row, run_main

import core    # noqa: E402 —— 必须在 harness 之后
import qqdoc   # noqa: E402


@contextmanager
def env(**kw):
    """临时设/清环境变量，退出时精确还原。"""
    old = {k: os.environ.get(k) for k in kw}
    try:
        for k, v in kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class PrecedenceTest(unittest.TestCase):

    def test_followup_home_wins(self):
        with env(FOLLOWUP_HOME="/tmp/fg-a", HERMES_HOME="/tmp/fg-b"):
            self.assertEqual(core.hermes_home(), Path("/tmp/fg-a"))
            self.assertEqual(qqdoc._hermes_home(), Path("/tmp/fg-a"))

    def test_hermes_home_still_works_alone(self):
        """兼容性底线：现有 Hermes 安装与 cron 一行都不用改。"""
        with env(FOLLOWUP_HOME=None, HERMES_HOME="/tmp/fg-b"):
            self.assertEqual(core.hermes_home(), Path("/tmp/fg-b"))
            self.assertEqual(qqdoc._hermes_home(), Path("/tmp/fg-b"))

    def test_falls_back_to_platform_default(self):
        with env(FOLLOWUP_HOME=None, HERMES_HOME=None):
            self.assertEqual(core.hermes_home(), Path.home() / ".hermes")
            self.assertEqual(qqdoc._hermes_home(), Path.home() / ".hermes")

    def test_two_resolvers_never_disagree(self):
        """
        core 与 qqdoc 各写了一遍（避免循环导入）。只要有一处漏改，
        就会出现「取数找得到凭证、判定找不到配置」这种半瘫状态。
        """
        for fh, hh in (("/tmp/x", "/tmp/y"), (None, "/tmp/y"),
                       ("/tmp/x", None), (None, None)):
            with self.subTest(FOLLOWUP_HOME=fh, HERMES_HOME=hh):
                with env(FOLLOWUP_HOME=fh, HERMES_HOME=hh):
                    self.assertEqual(core.hermes_home(), qqdoc._hermes_home())


class EndToEndTest(unittest.TestCase):
    """整个流程都要认 FOLLOWUP_HOME，不只是路径函数。"""

    def test_full_run_under_followup_home(self):
        from datetime import date, timedelta
        today = date(2026, 7, 20)
        sheet = make_sheet([
            row(1, "甲公司", tech="待收资",
                reported=today - timedelta(days=40),
                progress=today - timedelta(days=40)),
        ])
        # temp_home 设的是 HERMES_HOME；把它搬到 FOLLOWUP_HOME 再跑一次，
        # 结果必须一模一样。
        with temp_home() as home:
            with env(FOLLOWUP_HOME=str(home), HERMES_HOME="/nonexistent-on-purpose"):
                r = run_main([f"--today={today}", "--dry-run"], sheet)
                self.assertEqual(r.code, 0, r.err)
                self.assertIn("甲公司", r.out)
                self.assertIn("超期", r.out)


if __name__ == "__main__":
    unittest.main()
