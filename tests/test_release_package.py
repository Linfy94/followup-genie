#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布包结构、校验值与成品安装测试。"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_release  # noqa: E402


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SkillMetadataTest(unittest.TestCase):

    def test_frontmatter_uses_only_standard_fields(self):
        build_release.validate_skill_frontmatter()


class ReleasePackageTest(unittest.TestCase):

    def test_build_hashes_contents_and_install_from_archive(self):
        with tempfile.TemporaryDirectory(prefix="release 输出 ") as raw:
            base = Path(raw)
            output = base / "dist"
            paths = build_release.build(output)
            self.assertEqual(
                {path.name for path in paths},
                {
                    "followup-genie-agent.zip",
                    "followup-genie-agent-0.2.0-rc2.zip",
                    "followup-genie-workbuddy.skill",
                    "followup-genie-workbuddy-0.2.0-rc2.skill",
                    "SHA256SUMS.txt",
                },
            )

            checksums = {}
            for line in (output / "SHA256SUMS.txt").read_text(
                encoding="utf-8"
            ).splitlines():
                digest, name = line.split("  ", 1)
                checksums[name] = digest
            for path in paths[:4]:
                self.assertEqual(checksums[path.name], file_sha256(path))

            archive_path = output / "followup-genie-agent.zip"
            with zipfile.ZipFile(archive_path) as archive:
                names = archive.namelist()
                self.assertIn("followup-genie/scripts/bootstrap.py", names)
                self.assertIn("followup-genie/README.md", names)
                self.assertFalse(any("/tests/" in name for name in names))
                self.assertFalse(any("__pycache__" in name for name in names))
                self.assertFalse(any(name.endswith("/.env") for name in names))
                self.assertNotIn(
                    "followup-genie/scripts/build_release.py",
                    names,
                )
                extract_dir = base / "解压 目录"
                archive.extractall(extract_dir)

            workspace = base / "业务 工作空间"
            env = os.environ.copy()
            env.pop("FOLLOWUP_HOME", None)
            env.pop("HERMES_HOME", None)
            env["LANG"] = "C.UTF-8"
            env["LC_ALL"] = "C.UTF-8"
            result = subprocess.run(
                [
                    sys.executable,
                    str(
                        extract_dir
                        / "followup-genie"
                        / "scripts"
                        / "bootstrap.py"
                    ),
                    "--host",
                    "workbuddy",
                    "--workspace",
                    str(workspace),
                ],
                cwd=str(extract_dir / "followup-genie"),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(
                (workspace / "runtime" / "followup" / "config" / "rules.json")
                .is_file()
            )


if __name__ == "__main__":
    unittest.main()
