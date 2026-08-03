#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交付包清单的单一来源。

═══════════════════════════════════════════════════════════════════════
这份清单原本在两处各写一份，然后**漂移了**：

    bootstrap.py 装的      比 release zip 多 .gitignore / CHANGELOG.md / run_tests.sh
    release zip 解压的     两者都没有 tests/

同一个产品，两条安装路径装出不同的文件集。最难受的是
**bootstrap 给了 run_tests.sh 却没给 tests/** —— 业务点一下必然报错，
一个坏掉的自测按钮比没有按钮更糟。

所以本文件守两件事：
  ① 两条路径用的是同一份清单（防止再次漂移）
  ② 清单本身自洽 —— 尤其 run_tests.sh 与 tests/ 必须同进同出
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import harness  # noqa: F401 —— 只为触发它把 scripts/ 挂上 sys.path

import _manifest        # noqa: E402
import bootstrap        # noqa: E402

# 🔴 build_release.py 是开发工具，不进交付包。装出来的副本里没有它 ——
#    硬 import 的话，业务点一下 run_tests.sh 就是一条 ImportError。
try:
    import build_release    # noqa: E402
except ModuleNotFoundError:  # pragma: no cover —— 只在装出来的副本里发生
    build_release = None

requires_build_release = unittest.skipIf(
    build_release is None, "交付包里没有 build_release.py，无需也无法测打包")

ROOT = Path(__file__).resolve().parent.parent


@requires_build_release
class SingleSourceTest(unittest.TestCase):
    """两条安装路径必须用同一份清单。"""

    def test_bootstrap_and_build_release_agree(self):
        self.assertEqual(tuple(bootstrap.PACKAGE_FILES),
                         tuple(build_release.TOP_FILES),
                         "🔴 装出来的文件集与打包出来的不一致，清单又漂移了")
        self.assertEqual(tuple(bootstrap.PACKAGE_DIRS),
                         tuple(build_release.TOP_DIRS))

    def test_both_come_from_manifest(self):
        self.assertIs(bootstrap.PACKAGE_FILES, _manifest.TOP_FILES)
        self.assertIs(build_release.TOP_FILES, _manifest.TOP_FILES)

    def test_ignore_logic_is_shared(self):
        """copytree 的 ignore 与打包的过滤必须是同一套判断。"""
        names = [".env", "state", "followup", "notes", "a.pyc",
                 "x.corrupt.20260101", "README.md", "core.py"]
        ignored = bootstrap.should_ignore("", names)
        for n in names:
            self.assertEqual(n in ignored, _manifest.should_skip(n), n)


class ManifestIsCoherentTest(unittest.TestCase):

    def test_every_entry_exists(self):
        for name in _manifest.TOP_FILES:
            self.assertTrue((ROOT / name).is_file(), f"清单里的 {name} 不存在")
        for name in _manifest.TOP_DIRS:
            self.assertTrue((ROOT / name).is_dir(), f"清单里的 {name}/ 不存在")

    def test_self_test_needs_both_script_and_tests(self):
        """
        🔴 本轮修的就是这个：`run_tests.sh` 的内容是
        `unittest discover -s tests`，只给脚本不给目录 = 必然报错的按钮。
        """
        has_script = "run_tests.sh" in _manifest.TOP_FILES
        has_tests = "tests" in _manifest.TOP_DIRS
        self.assertEqual(has_script, has_tests,
                         "run_tests.sh 与 tests/ 必须同进同出")

    def test_runtime_data_is_never_packaged(self):
        """followup / state / .env 是业务的真实配置、催办状态和凭证。"""
        for bad in ("followup", "state", "runtime", ".env", ".git", "notes"):
            self.assertTrue(_manifest.should_skip(bad), f"{bad} 必须被排除")
            self.assertNotIn(bad, _manifest.TOP_DIRS)
            self.assertNotIn(bad, _manifest.TOP_FILES)

    def test_dev_only_scripts_stay_home(self):
        self.assertIn("build_release.py", _manifest.EXCLUDED_SCRIPT_NAMES)


class VersionStampTest(unittest.TestCase):
    """
    交付文件里写的版本号必须与 VERSION 一致。

    🔴 这条是 0.3.0-rc1 之后补的：发版时 VERSION、README、SKILL.md 都改了，
       但 docs/ 下两份给业务看的文档仍写着 `0.2.0-rc2`，而当时的守卫只查
       README 与 SKILL.md，于是一路放行推上了 GitHub。
    """

    def setUp(self):
        self.version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()

    def test_stamped_files_exist_and_match_version(self):
        for name in _manifest.VERSION_STAMPED_FILES:
            path = ROOT / name
            self.assertTrue(path.is_file(), f"清单里的 {name} 不存在")
            self.assertIn(self.version, path.read_text(encoding="utf-8"),
                          f"{name} 里没写当前版本 {self.version}")

    def test_stamped_files_carry_no_other_version(self):
        """写了当前版本还不够 —— 旧版本号残留同样会让人对不上账。"""
        pattern = re.compile(r"\b\d+\.\d+\.\d+(?:-rc\d+)?\b")
        for name in _manifest.VERSION_STAMPED_FILES:
            text = (ROOT / name).read_text(encoding="utf-8")
            others = {m for m in pattern.findall(text) if m != self.version}
            self.assertEqual(others, set(),
                             f"{name} 里还留着别的版本号：{sorted(others)}")

    @requires_build_release
    def test_build_release_guard_covers_these_files(self):
        """守卫读的必须是这份清单，不能又在别处硬编码一份。"""
        self.assertIs(
            _manifest.VERSION_STAMPED_FILES,
            build_release._manifest.VERSION_STAMPED_FILES)


# 🔴 防递归。
#    本文件会「装一份出来，再跑装出来那份的 run_tests.sh」，
#    而 tests/ 现在是交付内容 —— 装出来的那份里也有 test_manifest.py。
#    不设开关的话它会再装一份、再跑一次，无限套娃（实测跑爆了 120 秒）。
#    嵌套那一层跳过这两条，其余 200+ 条照跑 —— 「业务能自测」照样被证明。
NESTED = os.environ.get("FG_NESTED_TEST") == "1"


def _install(dest_home: str, workspace: Path) -> Path:
    """把包装到 workspace，返回装出来的代码目录。"""
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "bootstrap.py"),
         "--host", "workbuddy", "--workspace", str(workspace)],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": dest_home},
    )
    if r.returncode != 0:
        raise AssertionError(f"bootstrap 失败：\n{r.stdout}\n{r.stderr}")
    return workspace / ".followup-genie"


@unittest.skipIf(NESTED, "嵌套运行，跳过以免无限递归")
class InstalledCopyCanSelfTestTest(unittest.TestCase):
    """
    端到端：装到一个空目录，**在装出来的位置真的跑一遍自测**。

    这是本轮的核心回归。之前 bootstrap 装完的目录里没有 tests/，
    业务照 README 点 run_tests.sh 只会看到一句 ImportError ——
    一个坏掉的自测按钮比没有按钮更糟。
    """

    def test_workbuddy_install_can_run_its_own_tests(self):
        with tempfile.TemporaryDirectory(prefix="fg-e2e-") as d:
            skill = _install(d, Path(d) / "workspace")
            self.assertTrue((skill / "run_tests.sh").is_file())
            self.assertTrue((skill / "tests").is_dir(),
                            "🔴 装了 run_tests.sh 却没装 tests/，按钮是坏的")

            # 真跑一遍。这一步过了才算「业务能自测」。
            t = subprocess.run(
                ["bash", str(skill / "run_tests.sh")],
                capture_output=True, text=True, cwd=str(skill), timeout=600,
                env={"PATH": "/usr/bin:/bin", "HOME": d, "FG_NESTED_TEST": "1"})
            self.assertEqual(t.returncode, 0,
                             f"装出来的副本自测没过：\n{t.stderr[-3000:]}")
            self.assertIn("OK", t.stderr)

    @requires_build_release
    def test_copy_installed_from_the_zip_can_also_self_test(self):
        """
        🔴 补的是一个真实的覆盖盲区。

        上面那条走的是 **bootstrap 从开发树装**，而开发树里有
        `scripts/build_release.py`；zip 却把它排除掉了（它是开发工具）。
        于是两条路装出来的文件集不同，而只有 zip 那条会因为
        `tests/` 里硬 import build_release 而报 ImportError ——
        **业务拿到的正是 zip 那份**，点一下自测就是两条红色错误。

        这个盲区能藏住，就是因为当时只测了 bootstrap 那条路。
        """
        import zipfile
        with tempfile.TemporaryDirectory(prefix="fg-zip-") as d:
            base = Path(d)
            dist = base / "dist"
            build_release.build(dist)

            extract = base / "extract"
            with zipfile.ZipFile(dist / "followup-genie-agent.zip") as z:
                z.extractall(extract)
            pkg = extract / "followup-genie"

            self.assertFalse((pkg / "scripts" / "build_release.py").exists(),
                             "打包工具本来就不该进包")

            ws = base / "ws"
            r = subprocess.run(
                [sys.executable, str(pkg / "scripts" / "bootstrap.py"),
                 "--host", "workbuddy", "--workspace", str(ws)],
                capture_output=True, text=True, timeout=600,
                env={"PATH": "/usr/bin:/bin", "HOME": d})
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

            skill = ws / ".followup-genie"
            t = subprocess.run(
                ["bash", str(skill / "run_tests.sh")],
                capture_output=True, text=True, cwd=str(skill), timeout=900,
                env={"PATH": "/usr/bin:/bin", "HOME": d, "FG_NESTED_TEST": "1"})
            self.assertEqual(
                t.returncode, 0,
                f"🔴 从 zip 装出来的副本自测没过：\n{t.stderr[-3000:]}")

    def test_installed_file_set_matches_manifest(self):
        with tempfile.TemporaryDirectory(prefix="fg-set-") as d:
            skill = _install(d, Path(d) / "workspace")

            top = {p.name for p in skill.iterdir() if p.is_file()}
            self.assertEqual(top, set(_manifest.TOP_FILES),
                             "顶层文件集应与清单完全一致")
            dirs = {p.name for p in skill.iterdir() if p.is_dir()}
            self.assertEqual(dirs, set(_manifest.TOP_DIRS))

            # 运行时数据一个都不许跟过去
            for bad in ("followup", "state", ".env", ".git", "notes"):
                self.assertFalse((skill / bad).exists(), f"{bad} 不该被装过去")


if __name__ == "__main__":
    unittest.main()
