#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
找得到 lark-cli，以及找到之后跑得起来。

═══════════════════════════════════════════════════════════════════════
🔴 2026-08-04 09:00 真实故障：两条哨兵线双双报「本机没有安装 lark-cli」，
   主任务退出码 1，业务只收到盒子线。

   而 lark-cli 明明装在 ~/.local/bin —— **定时任务的 PATH 比登录 shell 短**
   （cron 由 launchd 托管的 gateway 派生）。

   这个坑项目里早防过一次：check_followup._hermes_bin() 的 docstring 原文
   就写着「不能只靠 PATH…找不到就得报错，不能静默地什么也没发」。
   只是当时没推广到 lark_base —— 同一个坑，一处防住了一处没防。

🔴 修的时候还暴露了第二层：lark-cli 的 shebang 是 `#!/usr/bin/env node`，
   **执行时要再找一次 node**。PATH 里没有 node 时报的是
   `env: node: No such file or directory` —— 一句和「没装 lark-cli」
   毫不相干的错，排查会被带偏。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from harness import core  # noqa: F401 —— 挂 sys.path

import lark_base  # noqa: E402
from qqdoc import LedgerError  # noqa: E402


class FindsLarkCliTest(unittest.TestCase):

    def test_prefers_path_when_available(self):
        with mock.patch.object(lark_base.shutil, "which",
                               return_value="/somewhere/bin/lark-cli"):
            self.assertEqual(lark_base.lark_cli_bin(), "/somewhere/bin/lark-cli")

    def test_falls_back_to_local_bin_when_path_is_bare(self):
        """
        🔴 这条就是今早那次故障：PATH 里没有，但文件在 ~/.local/bin。
        修好之前 lark_cli_bin 根本不存在，_run_cli 直接 ["lark-cli", ...]。
        """
        home = Path(os.path.expanduser("~"))
        target = home / ".local" / "bin" / "lark-cli"
        with mock.patch.object(lark_base.shutil, "which", return_value=None), \
             mock.patch.object(lark_base.Path, "exists",
                               lambda self: self == target):
            self.assertEqual(lark_base.lark_cli_bin(), str(target))

    def test_returns_none_when_really_absent(self):
        with mock.patch.object(lark_base.shutil, "which", return_value=None), \
             mock.patch.object(lark_base.Path, "exists", lambda self: False):
            self.assertIsNone(lark_base.lark_cli_bin())


class ErrorMessageTest(unittest.TestCase):
    """找不到时说的话必须能把人带到正确的地方。"""

    def _message(self):
        with mock.patch.object(lark_base, "lark_cli_bin", return_value=None):
            with self.assertRaises(LedgerError) as cm:
                lark_base._run_cli("+record-list", [])
        return str(cm.exception)

    def test_message_points_at_PATH_not_at_installation(self):
        msg = self._message()
        self.assertIn("PATH", msg,
                      "得说清可能是 PATH 问题 —— 「本机没有安装」"
                      "那句话把排查方向带偏过一次")
        self.assertIn(".local/bin", msg, "要列出查过哪些路径")

    def test_message_tells_a_non_developer_what_to_do(self):
        """业务不是开发。只说「找不到」等于没说。"""
        msg = self._message()
        self.assertIn("npm install", msg, "要给出可以直接照抄的安装命令")
        self.assertIn("Node.js", msg, "要说清它还需要 Node.js")
        self.assertIn("腾讯文档", msg,
                      "要说清只用腾讯文档的人根本不需要装它，"
                      "否则会以为自己漏装了东西")

    def test_never_offers_to_install_by_itself(self):
        """
        🔴 装一个全局命令行工具会改动这台电脑的环境，必须由人决定。
        程序只能停下来说清楚，不能代劳。
        """
        import inspect
        src = inspect.getsource(lark_base)
        self.assertNotIn("check_call", src)
        self.assertEqual(
            [c for c in ("npm", "brew", "pip") if f'"{c}"' in src], [],
            "🔴 lark_base 里不该出现任何安装器的可执行名 —— 只读取，不安装")


class ChildPathTest(unittest.TestCase):
    """
    子进程的 PATH 必须能找到 node，否则找到了 lark-cli 也跑不起来。
    """

    def test_puts_the_executables_own_dir_first(self):
        # 这里故意不用 macOS 家目录前缀 —— build_release 的敏感内容扫描
        # 会拦下任何含那个字面量的文件（防开发机路径混进交付包），
        # 哪怕是虚构路径也照拦。这条注释本身也得避开它。
        p = lark_base._child_path("/opt/fake-home/.local/bin/lark-cli")
        self.assertTrue(p.startswith("/opt/fake-home/.local/bin:"),
                        f"lark-cli 所在目录要排最前（node 通常同目录），实际 {p!r}")

    def test_includes_common_locations(self):
        p = lark_base._child_path("/somewhere/lark-cli").split(":")
        for need in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"):
            self.assertIn(need, p)

    def test_no_duplicates(self):
        parts = lark_base._child_path("/usr/bin/lark-cli").split(":")
        self.assertEqual(len(parts), len(set(parts)), f"PATH 有重复段：{parts}")

    def test_survives_an_empty_inherited_path(self):
        """env -i 那种极端情形下也不能拼出空段。"""
        with mock.patch.dict(os.environ, {"PATH": ""}, clear=False):
            parts = lark_base._child_path("/usr/bin/lark-cli").split(":")
        self.assertNotIn("", parts)


class WhitelistStillHoldsTest(unittest.TestCase):
    """改动没有松掉只读白名单 —— 那是铁律。"""

    def test_non_whitelisted_subcommand_is_refused_before_lookup(self):
        with mock.patch.object(lark_base, "lark_cli_bin") as look:
            with self.assertRaises(LedgerError) as cm:
                lark_base._run_cli("+record-create", ["--x"])
        look.assert_not_called()  # 连找可执行文件都不该走到
        self.assertIn("只读子命令", str(cm.exception))

    def test_whitelist_contents(self):
        self.assertEqual(
            lark_base.ALLOWED_SUBCOMMANDS,
            frozenset({"+table-list", "+field-list", "+record-list"}))


if __name__ == "__main__":
    unittest.main()
