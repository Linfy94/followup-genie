#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安装链路集成测试：全部在临时目录中，不接触真实 Hermes 或台账。"""

from __future__ import annotations

import os
import json
import stat
import pathlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def clean_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("FOLLOWUP_HOME", None)
    env.pop("HERMES_HOME", None)
    env["LANG"] = "C.UTF-8"
    env["LC_ALL"] = "C.UTF-8"
    return env


def run(command: list[str], *, env: dict[str, str], cwd: Path = ROOT):
    return subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


# 🔴 从 VERSION 文件读，不写死。
#    写死的版本号注定过期 —— 每次发版都要有人记得改这里，而没人会记得。
VERSION = (pathlib.Path(__file__).resolve().parent.parent / "VERSION"
           ).read_text(encoding="utf-8").strip()


class SetupScriptTest(unittest.TestCase):

    def test_utf8_space_path_idempotency_and_env_permissions(self):
        with tempfile.TemporaryDirectory(prefix="项目 跟进 ") as raw:
            runtime = Path(raw) / "业务 空间" / "runtime"
            env = clean_env()
            env["FOLLOWUP_HOME"] = str(runtime)

            first = run(["bash", "scripts/setup.sh"], env=env)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("运行时目录", first.stdout)
            for name in ("holidays", "ledgers", "output", "rules"):
                self.assertTrue(
                    (runtime / "followup" / "config" / f"{name}.json").is_file()
                )

            holiday_file = runtime / "followup" / "config" / "holidays.json"
            holidays = json.loads(holiday_file.read_text(encoding="utf-8"))
            self.assertTrue(holidays["verified"])
            self.assertEqual(holidays["covers_year"], 2026)
            self.assertEqual(len(holidays["holidays"]), 33)
            self.assertEqual(len(holidays["workdays"]), 6)
            rules = json.loads(
                (runtime / "followup" / "config" / "rules.json")
                .read_text(encoding="utf-8")
            )
            self.assertTrue(rules["workday"]["exclude_holidays"])

            ledgers = runtime / "followup" / "config" / "ledgers.json"
            sentinel = ledgers.read_text(encoding="utf-8") + "\n"
            ledgers.write_text(sentinel, encoding="utf-8")
            holiday_sentinel = b'{"business_override": true}\n'
            holiday_file.write_bytes(holiday_sentinel)
            state_file = runtime / "followup" / "state" / "business-state.json"
            state_file.write_bytes(b"keep-state\n")
            env_file = runtime / ".env"
            env_file.write_bytes(b"KEEP_SECRET=fake-but-preserved\n")
            env_sentinel = env_file.read_bytes()
            env_file.chmod(0o644)

            second = run(["bash", "scripts/setup.sh"], env=env)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(ledgers.read_text(encoding="utf-8"), sentinel)
            self.assertEqual(holiday_file.read_bytes(), holiday_sentinel)
            self.assertEqual(state_file.read_bytes(), b"keep-state\n")
            self.assertEqual(env_file.read_bytes(), env_sentinel)
            self.assertEqual(
                stat.S_IMODE(env_file.stat().st_mode),
                0o600,
            )

    def test_install_stops_when_setup_fails_and_does_not_create_shim(self):
        with tempfile.TemporaryDirectory(prefix="hermes bad ") as raw:
            home = Path(raw)
            config = home / "followup" / "config"
            config.mkdir(parents=True)
            (config / "ledgers.json").write_text("[]\n", encoding="utf-8")
            env = clean_env()
            env["HERMES_HOME"] = str(home)

            result = run(["bash", "scripts/install.sh"], env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((home / "scripts" / "followup_genie.py").exists())
            self.assertNotIn("cron 启动器已写入", result.stdout)


class BootstrapTest(unittest.TestCase):

    def test_workbuddy_install_uses_workspace_runtime(self):
        with tempfile.TemporaryDirectory(prefix="workbuddy 空间 ") as raw:
            workspace = Path(raw) / "项目 空间"
            result = run(
                [
                    sys.executable,
                    "scripts/bootstrap.py",
                    "--host",
                    "workbuddy",
                    "--workspace",
                    str(workspace),
                ],
                env=clean_env(),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("安装完成", result.stdout)
            self.assertEqual(
                (workspace / ".followup-genie" / "VERSION")
                .read_text(encoding="utf-8")
                .strip(),
                VERSION,
            )
            self.assertTrue(
                (workspace / "runtime" / "followup" / "config" / "rules.json")
                .is_file()
            )

    def test_hermes_install_copies_code_and_creates_shim(self):
        with tempfile.TemporaryDirectory(prefix="hermes 空间 ") as raw:
            home = Path(raw) / "home"
            home.mkdir()
            result = run(
                [
                    sys.executable,
                    "scripts/bootstrap.py",
                    "--host",
                    "hermes",
                    "--hermes-home",
                    str(home),
                ],
                env=clean_env(),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(
                (home / "skills" / "work" / "followup-genie" / "SKILL.md")
                .is_file()
            )
            self.assertTrue((home / "scripts" / "followup_genie.py").is_file())
            self.assertTrue(
                (home / "followup" / "config" / "ledgers.json").is_file()
            )


if __name__ == "__main__":
    unittest.main()
