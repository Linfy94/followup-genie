#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
升级行为：旧代码不许留下，业务数据不许动。

═══════════════════════════════════════════════════════════════════════
两个方向的要求，缺一不可，而且互相拉扯：

  ① **不得保留会继续执行的旧版代码**
     0.3.0-rc1 的 copy_package 是纯合并（原注释：「不删除目标目录中的
     任何文件」）。上一版的 scripts/old.py 升级后还躺在那儿，
     仍然 import 得到、仍然跑得起来，`unittest discover` 还会跑它的测试。
     两个版本混在一个目录里执行，出问题连是哪一版都说不清。

  ② **绝不能覆盖业务配置、状态和凭证**
     那是业务填的台账映射、几个月积累的催办时钟、和腾讯文档凭证。
     清理动作一旦手滑越界，业务的东西就没了 —— 比留着旧代码严重得多。

所以清理范围严格限定在 _manifest 清单内，且是**移动不是删除**。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness import temp_home       # noqa: F401 —— 顺带挂 sys.path

import _manifest    # noqa: E402
import bootstrap    # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def fingerprint(root: Path) -> dict:
    """整棵树的逐字节指纹 —— 用来证明「一个字节都没动」。"""
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


class RetireStaleCodeTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fg-up-")
        self.addCleanup(self.tmp.cleanup)
        self.dest = Path(self.tmp.name) / "skill"
        # 先装一遍「上一版」
        bootstrap.copy_package(ROOT, self.dest)

    def _upgrade(self):
        return bootstrap.copy_package(ROOT, self.dest)

    def _backup_dirs(self):
        return [p for p in self.dest.iterdir()
                if p.is_dir() and p.name.startswith(".upgrade-backup-")]

    def test_stale_script_is_moved_out_of_the_import_path(self):
        stale = self.dest / "scripts" / "legacy_module.py"
        stale.write_text("raise SystemExit('上一版的代码不该还能跑')", encoding="utf-8")

        retired = self._upgrade()

        self.assertFalse(stale.exists(), "🔴 上一版的 .py 还留在 scripts/ 里")
        self.assertIn("scripts/legacy_module.py", retired)

    def test_stale_test_is_moved_so_discover_cannot_find_it(self):
        stale = self.dest / "tests" / "test_from_last_version.py"
        stale.write_text("assert False", encoding="utf-8")

        self._upgrade()
        self.assertFalse(stale.exists(),
                         "🔴 上一版的测试还在，discover 会连它一起跑")

    def test_retired_files_are_kept_not_deleted(self):
        """用户的一贯要求是「不删除文件，只新增」。挪走 ≠ 销毁。"""
        stale = self.dest / "scripts" / "legacy_module.py"
        stale.write_text("上一版的内容", encoding="utf-8")

        self._upgrade()

        backups = self._backup_dirs()
        self.assertEqual(len(backups), 1)
        kept = backups[0] / "scripts" / "legacy_module.py"
        self.assertTrue(kept.is_file(), "🔴 旧文件被删掉了，应该是挪走")
        self.assertEqual(kept.read_text(encoding="utf-8"), "上一版的内容")

    def test_backup_dir_is_not_importable_or_discoverable(self):
        """
        挪到一个还会被执行的地方等于没挪。备份目录以 `.` 开头且不在清单里 ——
        import 和 unittest discover 都够不着。
        """
        (self.dest / "scripts" / "legacy_module.py").write_text("x", encoding="utf-8")
        self._upgrade()
        backup = self._backup_dirs()[0]

        self.assertTrue(backup.name.startswith("."), "隐藏目录才不会被 discover 到")
        self.assertNotIn(backup.name, _manifest.TOP_DIRS)
        self.assertTrue(_manifest.should_skip("__pycache__"))

    def test_discover_does_not_descend_into_the_backup_dir(self):
        """
        「挪走了」要落到实处：discover **收不到**备份目录里的旧测试。

        在一棵干净的小树上验，不在真 skill 上跑 —— 在真 skill 上跑 discover
        会把整套测试再跑一遍（这个文件也在里面），无限套娃，
        实测跑爆 600 秒。这里要证的只是「收不到」，收集阶段就够了。
        """
        with tempfile.TemporaryDirectory(prefix="fg-disc-") as raw:
            tree = Path(raw) / "tests"
            (tree / ".upgrade-backup-20260803-101112").mkdir(parents=True)
            (tree / "test_current.py").write_text(
                "import unittest\n"
                "class T(unittest.TestCase):\n"
                "    def test_ok(self): pass\n", encoding="utf-8")
            (tree / ".upgrade-backup-20260803-101112" / "test_old.py").write_text(
                "raise SystemExit('上一版的测试不该被收集到')", encoding="utf-8")

            names = []

            def walk(suite):
                for item in suite:
                    if isinstance(item, unittest.TestSuite):
                        walk(item)
                    else:
                        names.append(str(item))

            walk(unittest.TestLoader().discover(str(tree), top_level_dir=str(tree)))

            self.assertTrue([n for n in names if "test_current" in n],
                            "这一版自己的测试应该被收集到")
            self.assertFalse([n for n in names if "test_old" in n],
                             "🔴 备份目录里的旧测试被 discover 收走了")

    def test_pycache_from_the_old_version_is_retired(self):
        cache = self.dest / "scripts" / "__pycache__"
        cache.mkdir(exist_ok=True)
        (cache / "core.cpython-39.pyc").write_bytes(b"\x00old-version-bytecode")

        self._upgrade()
        self.assertFalse(cache.exists(), "上一版的 .pyc 对应的是上一版源码")

    def test_current_files_are_left_alone(self):
        """清理不能误伤这一版自己的文件。"""
        self._upgrade()
        for name in _manifest.TOP_FILES:
            self.assertTrue((self.dest / name).is_file(), f"{name} 被误伤了")
        self.assertTrue((self.dest / "scripts" / "core.py").is_file())
        self.assertTrue((self.dest / "scripts" / "watchdog.py").is_file())

    def test_clean_upgrade_creates_no_backup_dir(self):
        """没有残留就不该凭空造一个空备份目录出来。"""
        retired = self._upgrade()
        self.assertEqual(retired, [])
        self.assertEqual(self._backup_dirs(), [])


class BusinessDataIsUntouchedTest(unittest.TestCase):
    """
    🔴 本组比上面那组更重要。

    清理旧代码是「好一点」，误删业务配置是「灾难」——
    那是业务填的台账映射、几个月积累的催办时钟、和腾讯文档凭证。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fg-keep-")
        self.addCleanup(self.tmp.cleanup)
        self.dest = Path(self.tmp.name) / "skill"
        bootstrap.copy_package(ROOT, self.dest)

    def test_runtime_data_inside_the_skill_dir_survives(self):
        """有人把运行时目录放在了 skill 里面 —— 也不能碰。"""
        runtime = self.dest / "followup"
        (runtime / "config").mkdir(parents=True)
        (runtime / "state").mkdir(parents=True)
        (runtime / "config" / "ledgers.json").write_text(
            json.dumps({"ledgers": [{"id": "业务自己配的"}]}), encoding="utf-8")
        (runtime / "state" / "stage_entered.json").write_text(
            json.dumps({"box|3|test": "2026-02-09"}), encoding="utf-8")
        env = self.dest / ".env"
        env.write_text("TENCENT_DOCS_TOKEN=不该被读也不该被动\n", encoding="utf-8")

        before = fingerprint(runtime)
        env_before = env.read_bytes()

        bootstrap.copy_package(ROOT, self.dest)

        self.assertEqual(fingerprint(runtime), before, "🔴 运行时数据被动了")
        self.assertEqual(env.read_bytes(), env_before, "🔴 凭证文件被动了")

    def test_files_outside_the_manifest_survive(self):
        """
        生产目录里有 notes/、CHANGELOG.md 这些不在清单里的东西。
        清理范围必须严格限定在清单内，不在清单里的一律不管。
        """
        (self.dest / "notes").mkdir(exist_ok=True)
        (self.dest / "notes" / "开发记录.md").write_text("留着", encoding="utf-8")
        (self.dest / "CHANGELOG.md").write_text("留着", encoding="utf-8")
        (self.dest / "本地记的东西.txt").write_text("留着", encoding="utf-8")

        bootstrap.copy_package(ROOT, self.dest)

        self.assertTrue((self.dest / "notes" / "开发记录.md").is_file())
        self.assertTrue((self.dest / "CHANGELOG.md").is_file())
        self.assertTrue((self.dest / "本地记的东西.txt").is_file())

    def test_end_to_end_upgrade_keeps_config_and_state(self):
        """
        整条链路走一遍：装 → 业务改配置、攒状态 → 再装一次（升级）。
        配置与状态必须逐字节不变，旧代码必须已经不在执行路径上。
        """
        with tempfile.TemporaryDirectory(prefix="fg-e2e-up-") as raw:
            tmp = Path(raw)
            ws = tmp / "workspace"
            envp = {"PATH": "/usr/bin:/bin", "HOME": str(tmp)}

            def install():
                return subprocess.run(
                    [sys.executable, str(ROOT / "scripts" / "bootstrap.py"),
                     "--host", "workbuddy", "--workspace", str(ws)],
                    capture_output=True, text=True, env=envp, timeout=600)

            r = install()
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

            skill = ws / ".followup-genie"
            runtime = ws / "runtime"

            # 业务改了配置、攒了状态、填了凭证。
            # 🔴 改动必须保持配置**合法** —— setup.sh 末尾会跑
            #    doctor --validate-config，配置不合法它就该拒绝安装（那是对的），
            #    但那样测的就不是「升级会不会动业务数据」了。
            cfgp = runtime / "followup" / "config" / "ledgers.json"
            cfg = json.loads(cfgp.read_text(encoding="utf-8"))
            cfg["_业务自己加的标记"] = "升级后必须还在"
            cfgp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            (runtime / "followup" / "state" / "stage_entered.json").write_text(
                json.dumps({"box|3|efficiency_test": "2026-02-09"}),
                encoding="utf-8")
            (runtime / ".env").write_text("TENCENT_DOCS_TOKEN=x\n", encoding="utf-8")

            before = fingerprint(runtime)

            # 上一版留下的代码
            (skill / "scripts" / "legacy_module.py").write_text(
                "print('旧版')", encoding="utf-8")

            r = install()            # ← 升级
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

            self.assertEqual(fingerprint(runtime), before,
                             "🔴 升级动了业务的配置或状态")
            self.assertFalse((skill / "scripts" / "legacy_module.py").exists(),
                             "🔴 升级后旧代码还在执行路径上")
            self.assertTrue(
                any(p.name.startswith(".upgrade-backup-")
                    for p in skill.iterdir() if p.is_dir()),
                "旧代码应该被挪进备份目录而不是删掉")
            self.assertIn("移出执行路径", r.stdout, "挪走了要说一声")


if __name__ == "__main__":
    unittest.main()
