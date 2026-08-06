#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部 CLI 子进程环境的回归测试。"""

from __future__ import annotations

import ast
import os
import unittest
from pathlib import Path
from unittest import mock

from harness import temp_home  # noqa: F401 —— 同时挂载 scripts/

import lark_base  # noqa: E402

EXE = "/opt/fake/bin/lark-cli"
ROOT = Path(__file__).resolve().parent.parent

# 实测会让 lark-cli 判定「处在 Agent 上下文」并拒绝执行的变量。
# 做法：同一个二进制、同一秒，唯一变量是某一个环境变量，逐个跑
# `lark-cli config show` 看是否报 "not bound to it"。
# 这份清单**只增不减** —— 收缩它就等于把 0.4.0-rc2/rc4 那两次故障放回来。
MEASURED_SIGNALS = (
    ("HERMES_HOME", "/opt/fake-home/someone/.hermes"),
    ("OPENCLAW_HOME", "/opt/fake-home/someone/.openclaw"),
    ("HERMES_EXEC_ASK", "1"),
    ("HERMES_GATEWAY_TOKEN", "tok-not-real"),
    ("HERMES_SESSION_KEY", "sess-not-real"),
    ("HERMES_QUIET", "1"),
)


class StripsAgentContextTest(unittest.TestCase):

    def test_hermes_home_is_removed(self):
        with mock.patch.dict(os.environ, {"HERMES_HOME": "/somewhere/.hermes"}):
            env = lark_base._child_env(EXE)
        self.assertNotIn("HERMES_HOME", env)

    def test_openclaw_home_is_removed(self):
        with mock.patch.dict(os.environ, {"OPENCLAW_HOME": "/somewhere/.openclaw"}):
            env = lark_base._child_env(EXE)
        self.assertNotIn("OPENCLAW_HOME", env)

    def test_both_at_once(self):
        with mock.patch.dict(os.environ, {"HERMES_HOME": "/a", "OPENCLAW_HOME": "/b"}):
            env = lark_base._child_env(EXE)
        self.assertEqual([k for k in lark_base.AGENT_CONTEXT_VARS if k in env], [])

    def test_exec_ask_is_removed(self):
        """0.4.0-rc4 的真凶。gateway/run.py 模块级无条件注入，cron 子进程继承。"""
        with mock.patch.dict(os.environ, {"HERMES_EXEC_ASK": "1"}):
            env = lark_base._child_env(EXE)
        self.assertNotIn("HERMES_EXEC_ASK", env)

    def test_all_measured_signals_removed_at_once(self):
        """六个实测会触发的变量同时存在时，子进程环境里一个不剩。"""
        with mock.patch.dict(os.environ, dict(MEASURED_SIGNALS)):
            env = lark_base._child_env(EXE)
        self.assertEqual([k for k, _ in MEASURED_SIGNALS if k in env], [])

    def test_absent_is_fine(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            env = lark_base._child_env(EXE)
        self.assertNotIn("HERMES_HOME", env)

    def test_followup_home_survives(self):
        with mock.patch.dict(os.environ, {"FOLLOWUP_HOME": "/rt"}):
            env = lark_base._child_env(EXE)
        self.assertEqual(env.get("FOLLOWUP_HOME"), "/rt")


class KeepsEverythingElseTest(unittest.TestCase):

    def test_home_survives(self):
        with mock.patch.dict(os.environ, {"HOME": "/opt/fake-home/someone"}):
            env = lark_base._child_env(EXE)
        self.assertEqual(env.get("HOME"), "/opt/fake-home/someone")

    def test_proxy_and_locale_survive(self):
        extra = {"HTTPS_PROXY": "http://127.0.0.1:7890",
                 "LANG": "zh_CN.UTF-8", "SSL_CERT_FILE": "/etc/ssl/cert.pem"}
        with mock.patch.dict(os.environ, extra):
            env = lark_base._child_env(EXE)
        for key, value in extra.items():
            self.assertEqual(env.get(key), value)

    def test_unrelated_vars_survive(self):
        with mock.patch.dict(os.environ, {"SOME_UNRELATED": "1"}):
            env = lark_base._child_env(EXE)
        self.assertEqual(env.get("SOME_UNRELATED"), "1")


class OurSettingsWinTest(unittest.TestCase):

    def test_notifier_switches_cannot_be_overridden(self):
        with mock.patch.dict(os.environ, {
                "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "0",
                "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "0"}):
            env = lark_base._child_env(EXE)
        self.assertEqual(env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"], "1")
        self.assertEqual(env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"], "1")

    def test_path_is_computed_not_inherited(self):
        with mock.patch.dict(os.environ, {"PATH": "/only/this"}):
            env = lark_base._child_env(EXE)
        self.assertTrue(env["PATH"].startswith("/opt/fake/bin:"))
        self.assertIn("/only/this", env["PATH"].split(":"))


class RealCallUnderCronShapedEnvTest(unittest.TestCase):

    def _capture(self, extra_env=None):
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["env"] = kwargs.get("env")
            seen["cmd"] = cmd
            return mock.Mock(returncode=0, stdout='{"ok": true, "data": {}}',
                             stderr="")

        with temp_home():
            with mock.patch.dict(os.environ, dict(extra_env or {})), \
                 mock.patch.object(lark_base, "lark_cli_bin", return_value=EXE), \
                 mock.patch.object(lark_base.subprocess, "run", side_effect=fake_run):
                lark_base._run_cli("+table-list", ["--base-token", "bas"])
        return seen

    def test_subprocess_never_sees_hermes_home(self):
        self.assertNotIn("HERMES_HOME", self._capture()["env"])

    def test_subprocess_never_sees_any_measured_signal(self):
        """
        🔴 本轮锚点，直接对应 2026-08-06 09:00 的真实故障。

        `temp_home()` 本来就会设 HERMES_HOME；这里再补上 gateway 运行时
        注入的另外几个，捕获真正交给 subprocess.run 的那份 env。
        cron 的环境不是「更少」，是**不一样** —— PATH 更短，但 Agent
        注入的变量更多。
        """
        env = self._capture(dict(MEASURED_SIGNALS))["env"]
        self.assertEqual([k for k, _ in MEASURED_SIGNALS if k in env], [])

    def test_subprocess_still_gets_a_usable_env(self):
        env = self._capture()["env"]
        self.assertIn("PATH", env)
        self.assertEqual(env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"], "1")

    def test_command_shape_unchanged(self):
        self.assertEqual(self._capture()["cmd"][:3], [EXE, "base", "+table-list"])


class NoOtherPlaceBuildsASubprocessEnvTest(unittest.TestCase):

    OURS = ("setup.sh", "install.sh", "check_followup.py",
            "sys.executable", "'bash'")

    def test_every_env_kwarg_goes_through_child_env(self):
        offenders = []
        for py in sorted((ROOT / "scripts").glob("*.py")):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if ast.unparse(node.func) not in ("subprocess.run",
                                                  "subprocess.check_call",
                                                  "subprocess.Popen"):
                    continue
                for keyword in node.keywords:
                    if keyword.arg != "env":
                        continue
                    value = ast.unparse(keyword.value)
                    if value.startswith("_child_env"):
                        continue
                    target = ast.unparse(node.args[0]) if node.args else ""
                    if any(own in target for own in self.OURS):
                        continue
                    offenders.append(f"{py.name}:{node.lineno} env={value}")
        self.assertEqual(offenders, [])

    def test_run_cli_does_not_touch_os_environ_directly(self):
        import inspect
        source = inspect.getsource(lark_base._run_cli)
        self.assertNotIn("os.environ", source)
        self.assertNotIn("_os_environ", source)

    def test_context_var_list_is_shared_not_inlined(self):
        self.assertIn("HERMES_HOME", lark_base.AGENT_CONTEXT_VARS)
        self.assertIn("OPENCLAW_HOME", lark_base.AGENT_CONTEXT_VARS)

    def test_context_var_list_covers_every_measured_signal(self):
        """清单只增不减：删掉任何一个实测信号都要在这里显式失败。"""
        missing = [k for k, _ in MEASURED_SIGNALS
                   if k not in lark_base.AGENT_CONTEXT_VARS]
        self.assertEqual(missing, [])

    def test_context_var_list_is_not_a_prefix_wildcard(self):
        """
        实测编造的 HERMES_ZZZ_BUKEN **不触发** lark-cli 的探测。
        改成 `HERMES_*` 通配等于凭空猜上游语义，会把无关变量一起剔掉。
        """
        with mock.patch.dict(os.environ, {"HERMES_ZZZ_BUKEN": "1"}):
            env = lark_base._child_env(EXE)
        self.assertEqual(env.get("HERMES_ZZZ_BUKEN"), "1")


if __name__ == "__main__":
    unittest.main()
