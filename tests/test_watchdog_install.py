#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
存活监控的独立安装。

═══════════════════════════════════════════════════════════════════════
🔴 0.3.0-rc1 把监控器留在 `skills/work/followup-genie/scripts/` 里，
   plist 也指向那儿。于是：

     skill 被移动 / 删除 / `hermes skills update` 装坏
       → plist 指向的文件不存在
         → launchd 静默失败
           → **监控器和被监控对象一起消失**

   而「skill 坏了」正是它最该报警的场景之一。
   监控器死在被监控对象的目录里，等于没有监控。

本组测试的核心是最后那条：**把整个 skill 目录删掉，副本仍然工作。**
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import temp_home       # noqa: F401 —— 顺带把 scripts/ 挂上 sys.path

import watchdog  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class InstallLocationTest(unittest.TestCase):

    def test_watchdog_dir_is_outside_the_skill(self):
        """装的位置必须在运行时目录下，与包内代码没有任何路径关系。"""
        with temp_home() as home:
            d = watchdog.watchdog_dir()
            self.assertEqual(d, home / "watchdog")
            self.assertNotIn("skills", str(d))
            self.assertFalse(
                str(d).startswith(str(ROOT)),
                "🔴 监控器装进了 skill 目录里 —— skill 一没它就跟着没")

    def test_install_lays_down_three_things(self):
        with temp_home() as home:
            script, plist, python = watchdog.install("0.3.0-rc2")
            self.assertTrue(script.is_file())
            self.assertTrue(plist.is_file())
            self.assertEqual((home / "watchdog" / "VERSION")
                             .read_text(encoding="utf-8"), "0.3.0-rc2")
            self.assertTrue(os.access(script, os.X_OK), "副本要可执行")

    def test_installed_copy_is_byte_identical(self):
        with temp_home():
            script, _, _ = watchdog.install()
            self.assertEqual(script.read_bytes(),
                             Path(watchdog.__file__).read_bytes())

    def test_reinstall_is_idempotent(self):
        """升级会重复调用它，第二次不能报错，且要刷新成新版本。"""
        with temp_home() as home:
            watchdog.install("0.3.0-rc1")
            script, plist, _ = watchdog.install("0.3.0-rc2")
            self.assertTrue(script.is_file())
            self.assertEqual((home / "watchdog" / "VERSION")
                             .read_text(encoding="utf-8"), "0.3.0-rc2")

    def test_install_does_not_load_launchd(self):
        """
        装不装由人决定。脚本自己 `launchctl load` 是越权 ——
        它会在用户不知情时改动系统的常驻任务。
        """
        with temp_home():
            import unittest.mock as mock
            with mock.patch.object(watchdog.subprocess, "run") as run:
                watchdog.install()
                for call in run.call_args_list:
                    argv = call.args[0] if call.args else []
                    self.assertNotIn("launchctl", " ".join(map(str, argv)))

    def test_installed_copy_can_reinstall_itself(self):
        """业务从独立副本执行更新时，不能因源和目标是同一文件而失败。"""
        with temp_home() as home:
            script, _, _ = watchdog.install("old")
            env = os.environ.copy()
            env["FOLLOWUP_HOME"] = str(home)

            result = subprocess.run(
                [sys.executable, str(script), "--install", "--version", "new"],
                capture_output=True, text=True, env=env, timeout=120)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((home / "watchdog" / "VERSION")
                             .read_text(encoding="utf-8"), "new")

    def test_failed_reinstall_keeps_the_previous_working_copy(self):
        """发布新副本失败时，旧副本必须仍是完整可运行文件。"""
        with temp_home() as home:
            script, _, _ = watchdog.install("old")
            previous = script.read_bytes()

            with mock.patch.object(watchdog.os, "replace",
                                   side_effect=PermissionError("只读")):
                with self.assertRaises(PermissionError):
                    watchdog.install("new")

            self.assertEqual(script.read_bytes(), previous,
                             "原地覆盖失败不应留下半个 watchdog.py")


class PythonDetectionTest(unittest.TestCase):
    """
    🔴 plist 里的 python 路径必须是**安装时检测到的**，不能写死。

    硬编码 /usr/bin/python3 在精简系统、Linux、或 CommandLineTools
    没装时指向一个不存在的文件 —— launchd 静默失败，
    而「监控器自己没跑」恰恰没有任何人会发现。
    """

    def test_finds_a_working_python(self):
        p = watchdog.find_python()
        self.assertTrue(Path(p).is_file())
        self.assertTrue(os.access(p, os.X_OK))
        out = subprocess.run([p, "-c", "print(1)"], capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.stdout.strip(), "1")

    def test_falls_back_to_path_when_candidates_are_missing(self):
        import unittest.mock as mock
        with mock.patch.object(watchdog, "PYTHON_CANDIDATES", ("/不存在/python3",)), \
             mock.patch.object(watchdog.shutil, "which",
                               return_value="/找到的/python3"):
            self.assertEqual(watchdog.find_python(), "/找到的/python3")

    def test_raises_a_readable_error_when_nothing_is_found(self):
        import unittest.mock as mock
        with mock.patch.object(watchdog, "PYTHON_CANDIDATES", ("/不存在/python3",)), \
             mock.patch.object(watchdog.shutil, "which", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                watchdog.find_python()
            self.assertIn("找不到可用的 python3", str(ctx.exception))

    def test_does_not_use_sys_executable(self):
        """
        装的时候可能是 hermes 的 venv python 在跑。监控器一旦依赖那个 venv，
        hermes 装坏了它就跟着哑 —— 正是它要抓的场景。
        """
        # 只看真正的代码，不看文档字符串 —— find_python 的 docstring 里
        # 正写着「刻意不用 sys.executable」，全文搜会搜到那句说明。
        # （test_watchdog.test_does_not_import_core 踩过同一个坑。）
        import ast
        tree = ast.parse(Path(watchdog.__file__).read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "find_python")
        fn.body = [s for s in fn.body
                   if not (isinstance(s, ast.Expr)
                           and isinstance(s.value, ast.Constant)
                           and isinstance(s.value.value, str))]
        attrs = {f"{ast.dump(n.value)}.{n.attr}" for n in ast.walk(fn)
                 if isinstance(n, ast.Attribute)}
        self.assertFalse(
            any(a.endswith(".executable") for a in attrs),
            "find_python 不能用 sys.executable —— 那可能是 hermes 的 venv python")


class PlistContentTest(unittest.TestCase):

    def _plist(self, home):
        _, plist, python = watchdog.install("0.3.0-rc2")
        return plist.read_text(encoding="utf-8"), python

    def test_generated_plist_is_valid_xml_and_parses_as_a_plist(self):
        """
        🔴 生成的 plist 必须能被**严格的**解析器读出来。

        踩过一次：注释里写了 `--install`，而 XML 规范禁止注释中出现连续两个
        减号。launchd 自己的解析器容忍度高、照样加载了，所以在开发机上
        完全看不出问题；但 plistlib、xmllint 这类严格解析器直接报
        `not well-formed`。

        为什么必须守住：plist 一旦被某个 macOS 版本或某个工具判为非法，
        launchd 就不加载它 —— 而**监控器不加载的表现就是「一切安静」**，
        和「一切正常」长得一模一样。这正是整个 watchdog 存在的理由。
        """
        import plistlib
        with temp_home() as home:
            _, plist, python = watchdog.install("0.3.0-rc2")
            raw = plist.read_bytes()

            # 严格 XML 解析 —— launchd 的宽容度不能当作正确性依据
            import xml.dom.minidom
            xml.dom.minidom.parseString(raw.decode("utf-8"))

            data = plistlib.loads(raw)
            self.assertEqual(data["Label"], watchdog.LAUNCHD_LABEL)
            self.assertEqual(data["ProgramArguments"][0], python)
            self.assertEqual(data["ProgramArguments"][1],
                             str(home / "watchdog" / "watchdog.py"))
            self.assertEqual(data["EnvironmentVariables"]["FOLLOWUP_HOME"],
                             str(home))
            self.assertTrue(data["RunAtLoad"])
            self.assertTrue(data["StartCalendarInterval"])

    def test_no_comment_contains_a_double_dash(self):
        """把上面那条的根因单独钉死，报错信息才指得准。"""
        import re
        with temp_home():
            _, plist, _ = watchdog.install()
            text = plist.read_text(encoding="utf-8")
            for c in re.findall(r"<!--(.*?)-->", text, re.S):
                self.assertNotIn("--", c,
                                 "XML 注释里不许有连续两个减号，会让 plist 非法")

    def test_points_at_the_independent_copy_not_the_skill(self):
        with temp_home() as home:
            text, _ = self._plist(home)
            self.assertIn(str(home / "watchdog" / "watchdog.py"), text)
            self.assertNotIn("skills/work/followup-genie", text,
                             "🔴 plist 还指着 skill 里那份")

    def test_uses_the_detected_python(self):
        import plistlib
        with temp_home() as home:
            text, python = self._plist(home)
            data = plistlib.loads(text.encode("utf-8"))
            self.assertEqual(data["ProgramArguments"][0], python)

    def test_has_no_placeholders_left(self):
        """模板时代要人手抄「请改成…」，抄错了 launchd 静默失败。"""
        with temp_home() as home:
            text, _ = self._plist(home)
            for bad in ("请改成", "<请", "TODO", "…/"):
                self.assertNotIn(bad, text, f"plist 里还留着占位符 {bad!r}")

    def test_carries_the_right_runtime_home(self):
        """指错运行目录的话，监控器看的不是主任务写的那个 health.json。"""
        import plistlib
        with temp_home() as home:
            text, _ = self._plist(home)
            data = plistlib.loads(text.encode("utf-8"))
            self.assertEqual(data["EnvironmentVariables"]["FOLLOWUP_HOME"], str(home))

    def test_check_times_follow_the_configured_hour(self):
        import plistlib
        from harness import output_cfg
        with temp_home(output=output_cfg(watchdog={"schedule_hour": 7})) as home:
            text, _ = self._plist(home)
            data = plistlib.loads(text.encode("utf-8"))
            hours = [item["Hour"] for item in data["StartCalendarInterval"]]
            self.assertEqual(hours, [8, 13], "两次检查都应排在主任务之后")

    def test_special_characters_in_paths_are_valid_xml(self):
        """路径来自用户目录，&、<、中文都必须作为值转义，不能拼坏 XML。"""
        import plistlib
        home = Path("/tmp/业务 & <测试>")
        script = home / "watchdog" / "监控 & <脚本>.py"

        raw = watchdog.render_plist(
            "/路径/含 & <符号>/python3", script, home, 9).encode("utf-8")
        data = plistlib.loads(raw)

        self.assertEqual(data["ProgramArguments"][0], "/路径/含 & <符号>/python3")
        self.assertEqual(data["ProgramArguments"][1], str(script))
        self.assertEqual(data["EnvironmentVariables"]["FOLLOWUP_HOME"], str(home))

    def test_late_schedule_wraps_to_valid_launchd_hours(self):
        """23 点主任务的一小时后是次日 0 点，不能生成非法的 Hour=24。"""
        import plistlib
        data = plistlib.loads(watchdog.render_plist(
            "/usr/bin/python3", Path("/tmp/watchdog.py"), Path("/tmp/home"), 23
        ).encode("utf-8"))
        hours = [item["Hour"] for item in data["StartCalendarInterval"]]
        self.assertEqual(hours, [0, 5])
        self.assertTrue(all(0 <= hour <= 23 for hour in hours))


class SurvivesSkillDeletionTest(unittest.TestCase):
    """
    **本组的重点。** 前面几条都是形式检查，这条是真的把 skill 删掉。
    """

    def _make_env(self, tmp: Path) -> tuple[Path, Path]:
        home = tmp / "home"
        (home / "followup" / "config").mkdir(parents=True)
        (home / "followup" / "state").mkdir(parents=True)
        (home / "followup" / "config" / "output.json").write_text(
            json.dumps({"watchdog": {"enabled": True}}), encoding="utf-8")
        (home / "followup" / "state" / "health.json").write_text(
            json.dumps({"last_full_success": "2026-07-01T09:00:00+08:00"}),
            encoding="utf-8")

        skill = tmp / "skill"
        (skill / "scripts").mkdir(parents=True)
        shutil.copyfile(ROOT / "scripts" / "watchdog.py",
                        skill / "scripts" / "watchdog.py")
        (skill / "VERSION").write_text("0.3.0-rc2", encoding="utf-8")
        return home, skill

    def _run(self, script: Path, home: Path, *args):
        env = {"PATH": "/usr/bin:/bin", "HOME": str(home.parent),
               "FOLLOWUP_HOME": str(home)}
        return subprocess.run([sys.executable, str(script), *args],
                              capture_output=True, text=True, env=env, timeout=120)

    def test_watchdog_still_alerts_after_the_skill_is_deleted(self):
        with tempfile.TemporaryDirectory(prefix="fg-wd-") as raw:
            tmp = Path(raw)
            home, skill = self._make_env(tmp)

            r = self._run(skill / "scripts" / "watchdog.py", home, "--install")
            self.assertEqual(r.returncode, 0, r.stderr)

            # 🔴 把整个 skill 删掉 —— 模拟 skill 被移动/删除/升级装坏
            shutil.rmtree(skill)
            self.assertFalse(skill.exists())

            copy = home / "watchdog" / "watchdog.py"
            self.assertTrue(copy.is_file(), "🔴 skill 一删监控器就没了")

            r = self._run(copy, home, "--dry-run")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("需要告警", r.stdout,
                          "skill 没了之后，监控器仍必须能判定出故障")

    def test_the_copy_does_not_reach_back_into_the_skill(self):
        """副本不能靠相对路径回头找包里的东西 —— 那等于没搬走。"""
        with tempfile.TemporaryDirectory(prefix="fg-wd2-") as raw:
            tmp = Path(raw)
            home, skill = self._make_env(tmp)
            self._run(skill / "scripts" / "watchdog.py", home, "--install")
            shutil.rmtree(skill)

            # 必须用 dry-run：本测试只验证副本独立，正常模式会真的尝试
            # hermes send / osascript，违反「测试不碰真实世界」的硬约束。
            r = self._run(home / "watchdog" / "watchdog.py", home, "--dry-run")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("需要告警", r.stdout)
            self.assertNotIn("ModuleNotFoundError", r.stderr)
            self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
