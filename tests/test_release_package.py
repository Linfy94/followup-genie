#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布包结构、校验值与成品安装测试。"""

from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# 🔴 build_release.py 是开发工具，**不进交付包**（_manifest.EXCLUDED_SCRIPT_NAMES）。
#    从 zip 装出来的副本里没有它，所以这里不能硬 import ——
#    否则业务点一下 run_tests.sh 就是两条 ImportError。
#    交付包里没有可打包的东西，本组测试跳过才是对的。
try:
    import build_release  # noqa: E402
except ModuleNotFoundError:  # pragma: no cover —— 只在装出来的副本里发生
    build_release = None


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# 同上：版本号从 VERSION 文件读，不写死。
VERSION = (pathlib.Path(__file__).resolve().parent.parent / "VERSION"
           ).read_text(encoding="utf-8").strip()


# 装出来的副本里没有打包工具，整组跳过（见上面的说明）。
requires_build_release = unittest.skipIf(
    build_release is None, "交付包里没有 build_release.py，无需也无法测打包")


@requires_build_release
class SkillMetadataTest(unittest.TestCase):

    def test_frontmatter_uses_only_standard_fields(self):
        build_release.validate_skill_frontmatter()


@requires_build_release
class ReleasePackageTest(unittest.TestCase):

    def test_release_rejects_unregistered_organization_name(self):
        """不只测正则：往待打包文件塞一个客户名，完整 build() 必须失败。"""
        with tempfile.TemporaryDirectory(prefix="release-leak-") as raw:
            clone = Path(raw) / "source"
            shutil.copytree(ROOT, clone, ignore=shutil.ignore_patterns(".git"))
            leak = clone / "tests" / "test_privacy_leak.py"
            customer = "真实客户科技" + "有限公司"
            leak.write_text(f'CUSTOMER = "{customer}"\n', encoding="utf-8")
            with mock.patch.object(build_release, "ROOT", clone):
                with self.assertRaisesRegex(build_release.BuildError,
                                            "未登记的组织名"):
                    build_release.build(Path(raw) / "dist")

    def test_registered_examples_are_allowed(self):
        data = "\n".join(sorted(
            build_release._manifest.ALLOWED_EXAMPLE_ORGANIZATIONS
        )).encode("utf-8")
        self.assertEqual(build_release.unapproved_organizations(data), [])

    def test_zip_and_skill_each_install_in_isolation(self):
        with tempfile.TemporaryDirectory(prefix="release-formats-") as raw:
            base = Path(raw)
            output = base / "dist"
            build_release.build(output)
            for filename in ("followup-genie-agent.zip",
                             "followup-genie-workbuddy.skill"):
                with self.subTest(filename=filename):
                    extract = base / f"extract-{Path(filename).suffix[1:]}"
                    with zipfile.ZipFile(output / filename) as archive:
                        archive.extractall(extract)
                    workspace = base / f"workspace-{Path(filename).suffix[1:]}"
                    env = os.environ.copy()
                    env.pop("FOLLOWUP_HOME", None)
                    env.pop("HERMES_HOME", None)
                    result = subprocess.run(
                        [sys.executable,
                         str(extract / "followup-genie" / "scripts" / "bootstrap.py"),
                         "--host", "workbuddy", "--workspace", str(workspace)],
                        cwd=str(extract / "followup-genie"), env=env, text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        (workspace / ".followup-genie" / "VERSION")
                        .read_text(encoding="utf-8").strip(), VERSION)

    def test_build_hashes_contents_and_install_from_archive(self):
        with tempfile.TemporaryDirectory(prefix="release 输出 ") as raw:
            base = Path(raw)
            output = base / "dist"
            paths = build_release.build(output)
            self.assertEqual(
                {path.name for path in paths},
                {
                    "followup-genie-agent.zip",
                    f"followup-genie-agent-{VERSION}.zip",
                    "followup-genie-workbuddy.skill",
                    f"followup-genie-workbuddy-{VERSION}.skill",
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

                # 🔴 口径变更（2026-08-03）：tests/ 与 run_tests.sh 现在**进包**。
                #    交付标准是「最小能让业务接入其他台账并自测」，
                #    而 run_tests.sh 的内容是 `unittest discover -s tests` ——
                #    只给脚本不给目录等于给了一个必然报错的按钮。
                self.assertIn("followup-genie/run_tests.sh", names)
                self.assertTrue(any("/tests/" in name for name in names),
                                "没有 tests/ 的话 run_tests.sh 是坏的")

                # 运行时数据与开发过程记录一律不进包
                self.assertFalse(any("__pycache__" in name for name in names))
                self.assertFalse(any(name.endswith("/.env") for name in names))
                self.assertFalse(any("/notes/" in name for name in names),
                                 "开发过程记录含开发机路径，不交付")
                for part in ("/followup/", "/state/", "/runtime/"):
                    self.assertFalse(any(part in name for name in names),
                                     f"{part} 是运行时数据，绝不能进包")
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
