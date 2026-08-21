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
from unittest import mock

from harness import core, run_main, temp_home  # noqa: F401 —— 顺带挂 sys.path

import _manifest    # noqa: E402
import bootstrap    # noqa: E402

from test_sentinel_rules import FakeSheet  # noqa: E402

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

            # 🔴 2026-08-21：升级现在会跑一次状态迁移（见 run_migration），
            # 而迁移要跟每日任务共用运行锁——取锁本身会在 state/ 下留一个
            # 空的 .run.lock.guard flock 文件（core._lock_metadata_guard，
            # 进程退出后不删，是所有取锁动作的共同副作用，不是这次升级
            # 独有的）。它是程序自己的锁基础设施，不是业务配置或状态，
            # 从"必须逐字节不变"的比对里单独排除，其余一个字节都不许变。
            #
            # 🔴 迁移这次真的干净跑完了一次（示例模板台账没配
            # key_tiebreakers，属于"检查过、确认没有可迁移的"这种干净
            # 完成），会落一个 migrations_completed.json 标记，往后升级
            # 靠它跳过重复联网——这也是程序自己的运行记录，不是业务数据，
            # 同样排除在"逐字节不变"之外。
            after = fingerprint(runtime)
            after.pop("followup/state/.run.lock.guard", None)
            after.pop("followup/state/migrations_completed.json", None)
            self.assertEqual(after, before,
                             "🔴 升级动了业务的配置或状态")
            self.assertFalse((skill / "scripts" / "legacy_module.py").exists(),
                             "🔴 升级后旧代码还在执行路径上")
            self.assertTrue(
                any(p.name.startswith(".upgrade-backup-")
                    for p in skill.iterdir() if p.is_dir()),
                "旧代码应该被挪进备份目录而不是删掉")
            self.assertIn("移出执行路径", r.stdout, "挪走了要说一声")


class ManagedPathSafetyTest(unittest.TestCase):
    """安装器只能修改 Skill 自己的真实文件，不能顺链接写到目录外。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fg-path-safe-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.dest = self.root / "skill"
        self.dest.mkdir()

    def test_managed_directory_symlink_is_rejected_before_copy(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("不能动", encoding="utf-8")
        (self.dest / "scripts").symlink_to(outside, target_is_directory=True)

        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.copy_package(ROOT, self.dest)

        self.assertEqual((outside / "keep.txt").read_text(encoding="utf-8"), "不能动")
        self.assertFalse((outside / "core.py").exists(),
                         "🔴 安装器顺着符号链接把代码写到了 Skill 外面")

    def test_managed_hardlink_is_rejected_before_copy(self):
        outside = self.root / "outside.py"
        outside.write_text("不能覆盖", encoding="utf-8")
        (self.dest / "scripts").mkdir()
        os.link(str(outside), str(self.dest / "scripts" / "core.py"))

        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.copy_package(ROOT, self.dest)

        self.assertEqual(outside.read_text(encoding="utf-8"), "不能覆盖",
                         "🔴 覆盖硬链接等于改了管理范围外的文件")

    def test_type_collision_fails_before_any_package_file_is_overwritten(self):
        readme = self.dest / "README.md"
        readme.write_text("旧版 README", encoding="utf-8")
        (self.dest / "scripts").write_text("这里本应是目录", encoding="utf-8")

        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.copy_package(ROOT, self.dest)

        self.assertEqual(readme.read_text(encoding="utf-8"), "旧版 README",
                         "预检失败前不应已经覆盖一半文件")

    def test_failed_atomic_move_keeps_the_source_file(self):
        bootstrap.copy_package(ROOT, self.dest)
        stale = self.dest / "scripts" / "旧 文件🧚\n.py"
        stale.write_text("保留我", encoding="utf-8")

        with mock.patch.object(bootstrap.os, "replace",
                               side_effect=PermissionError("只读")):
            with self.assertRaises(bootstrap.BootstrapError):
                bootstrap.copy_package(ROOT, self.dest)

        self.assertTrue(stale.is_file(), "移动失败时源文件不能消失")
        self.assertEqual(stale.read_text(encoding="utf-8"), "保留我")


# ══════════════════════════════════════════════════════════════════════
# 升级失败要能原样退回去
#
# 🔴 以前的顺序是「先覆盖，再让 setup 去发现问题」。包缺文件、某个 .py
#    有语法错、自检不过 —— 这些都要等覆盖完才暴露，而那时旧版已经没了。
#    定时任务第二天早上 9:00 照样会触发，跑的是**半升级的代码**。
#    催办工具最怕这种：它不会崩得很响，只会「今天没有要催的」。
# ══════════════════════════════════════════════════════════════════════

class VerifyBeforeReplaceTest(unittest.TestCase):
    """校验必须发生在动旧版之前。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fg-verify-")
        self.addCleanup(self.tmp.cleanup)
        self.pkg = Path(self.tmp.name) / "pkg"
        bootstrap.copy_package(ROOT, self.pkg)     # 拿一份完整的包当素材

    def test_good_package_passes(self):
        bootstrap.verify_package(self.pkg)         # 不抛就算过

    def test_missing_top_file(self):
        (self.pkg / "VERSION").unlink()
        with self.assertRaises(bootstrap.BootstrapError) as cm:
            bootstrap.verify_package(self.pkg)
        self.assertIn("VERSION", str(cm.exception))
        self.assertIn("未改动现有程序", str(cm.exception))

    def test_missing_top_dir(self):
        shutil.rmtree(self.pkg / "tests")
        with self.assertRaises(bootstrap.BootstrapError) as cm:
            bootstrap.verify_package(self.pkg)
        self.assertIn("tests/", str(cm.exception))

    def test_missing_entry_script(self):
        (self.pkg / "scripts" / "check_followup.py").unlink()
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.verify_package(self.pkg)

    def test_syntax_error_in_any_script(self):
        (self.pkg / "scripts" / "core.py").write_text(
            "def broken(:\n", encoding="utf-8")
        with self.assertRaises(bootstrap.BootstrapError) as cm:
            bootstrap.verify_package(self.pkg)
        self.assertIn("core.py", str(cm.exception))

    def test_empty_version(self):
        (self.pkg / "VERSION").write_text("  \n", encoding="utf-8")
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.verify_package(self.pkg)

    def test_verify_does_not_import_the_new_code(self):
        """
        import 会执行模块顶层语句，等于在校验阶段就把新版跑起来了。
        只 compile，不 import。
        """
        (self.pkg / "scripts" / "core.py").write_text(
            "raise SystemExit('顶层就退出')\n", encoding="utf-8")
        bootstrap.verify_package(self.pkg)   # 语法没问题，就该通过


class RollbackTest(unittest.TestCase):
    """setup / 自检不过时，把旧版原样放回去。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fg-rb-")
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.pkg = base / "pkg"
        self.dest = base / "skill"
        self.runtime = base / "runtime"
        bootstrap.copy_package(ROOT, self.pkg)
        bootstrap.copy_package(ROOT, self.dest)      # 现有的「旧版」
        # 给旧版打个记号，好证明放回去的确实是它
        (self.dest / "VERSION").write_text("0.0.0-old\n", encoding="utf-8")
        (self.dest / "scripts" / "only_in_old.py").write_text(
            "# 旧版独有\n", encoding="utf-8")
        # 业务自己的东西 —— 全程一个字节都不许动
        (self.dest / "followup").mkdir()
        (self.dest / "followup" / "ledgers.json").write_text(
            '{"ledgers": []}', encoding="utf-8")
        (self.dest / ".env").write_text("TOKEN=不许碰\n", encoding="utf-8")
        self.before = fingerprint(self.dest)

    def _install(self):
        return bootstrap.install_or_rollback(self.pkg, self.dest, self.runtime)

    def test_setup_failure_restores_the_old_version(self):
        with mock.patch.object(bootstrap, "run_setup",
                               side_effect=bootstrap.BootstrapError("自检不过")):
            with self.assertRaises(bootstrap.BootstrapError):
                self._install()
        self.assertEqual((self.dest / "VERSION").read_text(encoding="utf-8"),
                         "0.0.0-old\n", "🔴 旧版没有被放回来")
        self.assertTrue((self.dest / "scripts" / "only_in_old.py").is_file(),
                        "旧版独有的文件也要回来")

    def test_new_version_files_do_not_linger_after_rollback(self):
        """
        只把旧文件拷回来是不够的 —— 新版多出来的文件会留在原地，
        那还是半升级。它们必须被移出执行路径。
        """
        (self.pkg / "scripts" / "only_in_new.py").write_text(
            "# 新版独有\n", encoding="utf-8")
        with mock.patch.object(bootstrap, "run_setup",
                               side_effect=bootstrap.BootstrapError("自检不过")):
            with self.assertRaises(bootstrap.BootstrapError):
                self._install()
        self.assertFalse((self.dest / "scripts" / "only_in_new.py").exists(),
                         "🔴 新版的文件还在，import 得到、discover 也扫得到")

    def test_nothing_is_deleted_only_moved(self):
        (self.pkg / "scripts" / "only_in_new.py").write_text(
            "# 新版独有\n", encoding="utf-8")
        with mock.patch.object(bootstrap, "run_setup",
                               side_effect=bootstrap.BootstrapError("自检不过")):
            with self.assertRaises(bootstrap.BootstrapError):
                self._install()
        stashed = list(self.dest.glob(".upgrade-rollback-*/replaced/scripts/only_in_new.py"))
        self.assertTrue(stashed, "现场要留得住，不能删")

    def test_business_config_and_env_untouched(self):
        with mock.patch.object(bootstrap, "run_setup",
                               side_effect=bootstrap.BootstrapError("自检不过")):
            with self.assertRaises(bootstrap.BootstrapError):
                self._install()
        self.assertEqual(
            (self.dest / "followup" / "ledgers.json").read_text(encoding="utf-8"),
            '{"ledgers": []}')
        self.assertEqual((self.dest / ".env").read_text(encoding="utf-8"),
                         "TOKEN=不许碰\n")

    def test_managed_tree_is_byte_identical_after_rollback(self):
        with mock.patch.object(bootstrap, "run_setup",
                               side_effect=bootstrap.BootstrapError("自检不过")):
            with self.assertRaises(bootstrap.BootstrapError):
                self._install()
        after = fingerprint(self.dest)

        def keep(d):
            return {k: v for k, v in d.items()
                    if k in _manifest.TOP_FILES or k.startswith(
                        tuple(n + "/" for n in _manifest.TOP_DIRS))}
        self.assertEqual(keep(after), keep(self.before),
                         "清单范围内必须逐字节回到升级前")

    def test_bad_package_never_touches_the_old_version(self):
        """包本身就不合格时，连快照都不用做 —— 旧版全程没被碰过。"""
        (self.pkg / "scripts" / "core.py").write_text("def x(:\n", encoding="utf-8")
        with self.assertRaises(bootstrap.BootstrapError):
            self._install()
        self.assertEqual(fingerprint(self.dest), self.before,
                         "校验不过时不该产生任何改动，连快照目录都不该有")

    def test_successful_install_leaves_the_new_version(self):
        with mock.patch.object(bootstrap, "run_setup", return_value=None), \
             mock.patch.object(bootstrap, "run_migration", return_value=None):
            self._install()
        self.assertNotEqual((self.dest / "VERSION").read_text(encoding="utf-8"),
                            "0.0.0-old\n", "成功时就该是新版")

    def test_first_install_has_nothing_to_roll_back(self):
        fresh = Path(self.tmp.name) / "fresh"
        with mock.patch.object(bootstrap, "run_setup",
                               side_effect=bootstrap.BootstrapError("自检不过")):
            with self.assertRaises(bootstrap.BootstrapError):
                bootstrap.install_or_rollback(self.pkg, fresh, self.runtime)
        # 不崩即可；首次安装没有「原来那一版」可退


# ══════════════════════════════════════════════════════════════════════
# 升级必须迁移旧状态（key_tiebreakers 的 rc14→rc15 主键漂移，见
# migrate_rc15_key_state.py）——不然代码换了、状态没迁，第一次真跑就可能
# 触发一批多余的首次催办。cron 本身不归 bootstrap 管（是装完后人手动注册
# 的一次性动作），能补的窗口只有升级这个动作本身完成之前。
# ══════════════════════════════════════════════════════════════════════

class MigrationDuringUpgradeTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fg-mig-")
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.pkg = base / "pkg"
        self.dest = base / "skill"
        self.runtime = base / "runtime"
        bootstrap.copy_package(ROOT, self.pkg)
        bootstrap.copy_package(ROOT, self.dest)  # 现有的「旧版」
        (self.dest / "followup").mkdir()

    def _install(self, fresh=False):
        dest = (Path(self.tmp.name) / "fresh") if fresh else self.dest
        return bootstrap.install_or_rollback(self.pkg, dest, self.runtime)

    def test_upgrade_calls_migration_after_setup_succeeds(self):
        order = []
        with mock.patch.object(bootstrap, "run_setup",
                               side_effect=lambda *a, **k: order.append("setup")) as setup, \
             mock.patch.object(bootstrap, "run_migration",
                               side_effect=lambda *a, **k: order.append("migrate")) as mig:
            self._install()
        setup.assert_called_once()
        mig.assert_called_once()
        self.assertEqual(order, ["setup", "migrate"],
                         "🔴 迁移必须在自检通过之后才做，不能颠倒")

    def test_first_install_does_not_call_migration(self):
        """首次安装没有旧状态可迁，硬跑一次纯属徒增一次不必要的联网取数。"""
        with mock.patch.object(bootstrap, "run_setup", return_value=None), \
             mock.patch.object(bootstrap, "run_migration", return_value=None) as mig:
            self._install(fresh=True)
        mig.assert_not_called()

    def test_migration_failure_rolls_back_like_setup_failure(self):
        """
        迁移失败必须跟 run_setup 失败走同一条回滚路径——不能让新代码
        留在旧状态上，那正是这个 P0 要防的事。
        """
        (self.dest / "VERSION").write_text("0.0.0-old\n", encoding="utf-8")
        with mock.patch.object(bootstrap, "run_setup", return_value=None), \
             mock.patch.object(bootstrap, "run_migration",
                               side_effect=bootstrap.BootstrapError("迁移失败")):
            with self.assertRaises(bootstrap.BootstrapError):
                self._install()
        self.assertEqual((self.dest / "VERSION").read_text(encoding="utf-8"),
                         "0.0.0-old\n", "🔴 迁移失败时旧版没有被放回来")


class MigrationSkippedOnceMarkedDoneTest(unittest.TestCase):
    """
    🔴 2026-08-21 第四轮复审：迁移一旦真的跑成功过一次，往后每次升级都
    还会重新联网读一遍配了 key_tiebreakers 的台账——多余，且让日后所有
    升级都莫名其妙依赖一次跟那次改动毫无关系的网络请求。改成先看
    migrations_completed.json 里的标记，标记在就直接跳过，不联网、
    不进子进程、不占运行锁。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fg-mig-marker-")
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.skill = base / "skill"
        self.runtime = base / "runtime"
        bootstrap.copy_package(ROOT, self.skill)
        (self.runtime / "followup" / "state").mkdir(parents=True)

    def _write_marker(self, content):
        marker = self.runtime / "followup" / "state" / "migrations_completed.json"
        marker.write_text(json.dumps(content), encoding="utf-8")

    def test_marker_present_skips_the_subprocess_entirely(self):
        self._write_marker({"rc15_key_tiebreakers": "2026-08-21T09:00:00+08:00"})
        with mock.patch.object(bootstrap.subprocess, "run") as run:
            bootstrap.run_migration(self.skill, self.runtime, hermes=True)
        run.assert_not_called()

    def test_marker_absent_still_runs_the_subprocess(self):
        with mock.patch.object(bootstrap.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0)) as run:
            bootstrap.run_migration(self.skill, self.runtime, hermes=True)
        run.assert_called_once()

    def test_marker_present_but_missing_this_migrations_id_still_runs(self):
        """标记文件存在，但里面记的是别的一次性迁移——不能被误当成这次也做过。"""
        self._write_marker({"some_other_future_migration": "2026-09-01T00:00:00+08:00"})
        with mock.patch.object(bootstrap.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0)) as run:
            bootstrap.run_migration(self.skill, self.runtime, hermes=True)
        run.assert_called_once()

    def test_corrupt_marker_file_is_treated_as_not_done(self):
        """标记文件本身读不出来，保守起见当作没做过，不能因为一份坏文件就永久跳过迁移。"""
        marker = self.runtime / "followup" / "state" / "migrations_completed.json"
        marker.write_text("{ 坏 json", encoding="utf-8")
        with mock.patch.object(bootstrap.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0)) as run:
            bootstrap.run_migration(self.skill, self.runtime, hermes=True)
        run.assert_called_once()


class MigrationForceKilledDuringUpgradeTest(unittest.TestCase):
    """
    🔴 2026-08-21 第七轮复审的 P0：迁移子进程被**强杀**（SIGKILL 这类连
    except 都拦不住的信号）时，进程内自己那套"炸了就地复原"完全没机会
    执行——bootstrap.py 原来看到子进程退出码非零就直接回滚**代码**，
    但旧代码完全不认识事务日志这套机制，状态可能停在"改了一部分"的
    中间态，永远没人会去处理那份孤零零的日志。

    这批测试穷举"被杀在第几步"（0..N，N = 这次改写涉及的文件总数），
    每一种都要断言最终只能是以下两种之一，不能是别的：
      · state 全部退回旧内容（老 key），此时必须回滚代码
      · state 全部已经是新内容（新 key），此时绝不能回滚代码

    做法：不真的用信号去杀一个真实进程（慢、脆、跨平台行为不一致），
    而是**直接在磁盘上还原"被杀在第 k 步"那一刻的现场**——这就是
    事务日志机制本来要处理的输入，不管它是被信号杀死还是进程自己
    exit 得到的都一样，"盘上留下了什么"才是恢复逻辑唯一能看到的东西。
    再让 bootstrap.subprocess.run 的**第一次**调用（对应主 --apply
    子进程）返回一个模拟被信号杀死的结果，第二次调用（--recover-only）
    放行、真的跑一遍——这样测的是 bootstrap.py 真实会执行的那条恢复
    路径，只是没有真的启动又杀掉一个进程。
    """

    STATE_NAMES = ("stage_entered.json", "followup_state.json",
                  "stage_history.json", "migrations_completed.json")

    def setUp(self):
        self._new_scene()

    def _new_scene(self):
        """一份全新的 skill/runtime 现场——subTest 循环里每一轮都要用干净的一份。"""
        tmp = tempfile.TemporaryDirectory(prefix="fg-mig-kill-")
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.skill = base / "skill"          # 真实的一份新版代码
        self.runtime = base / "runtime"
        bootstrap.copy_package(ROOT, self.skill)
        self.state = self.runtime / "followup" / "state"
        self.state.mkdir(parents=True)

    def _state_bytes(self, data: dict) -> bytes:
        """跟 core._state_bytes 用同一种格式——事务日志的指纹就是拿这个算的。"""
        return json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")

    def _seed_killed_at_step(self, k: int):
        """
        还原"迁移写完前 k 个文件后被杀"的磁盘现场。

        old：改写前的内容（backup 里的、以及还没轮到写的文件当前的样子）。
        new：改写要写成的样子（前 k 个文件已经是这样，事务日志记的也是这份
             "最终该有的样子"——begin_state_transaction 在动手之前就把全部
             目标内容的指纹写死了，不会随着写的进度变化）。
        """
        old = {
            "stage_entered.json": {"box|3|efficiency_test": "2026-02-09"},
            "followup_state.json": {"box|3|efficiency_test": {"first_overdue": "2026-02-09"}},
            "stage_history.json": {},
        }
        new = {
            "stage_entered.json": {"box|3‖欧洲|efficiency_test": "2026-02-09"},
            "followup_state.json": {"box|3‖欧洲|efficiency_test": {"first_overdue": "2026-02-09"}},
            "stage_history.json": {},
            "migrations_completed.json": {"rc15_key_tiebreakers": "2026-08-21T09:00:00+08:00"},
        }

        backup = self.state / ".migrate-rc15-backup-TEST"
        backup.mkdir(parents=True, exist_ok=True)
        for name, data in old.items():   # migrations_completed.json 改写前不存在，不进备份
            (backup / name).write_bytes(self._state_bytes(data))

        for i, name in enumerate(self.STATE_NAMES):
            target = new[name] if i < k else old.get(name)
            path = self.state / name
            if target is None:           # 还没轮到写、且改写前本来就不存在
                if path.exists():
                    path.unlink()
                continue
            path.write_bytes(self._state_bytes(target))

        expected = {name: hashlib.sha256(self._state_bytes(data)).hexdigest()
                   for name, data in new.items()}
        (self.state / core.STATE_TXN_FILE).write_bytes(self._state_bytes({
            "backup_dir": str(backup),
            "files": list(self.STATE_NAMES),
            "expected": expected,
            "started_at": "2026-08-21T09:00:00+08:00",
            "pid": 999999,   # 肯定不是活着的 pid，"死锁自动夺回"那条逻辑不会绊住这个测试
        }))
        return old, new

    def _run_migration_with_killed_first_attempt(self):
        """
        第一次 subprocess.run（--apply）模拟被 SIGKILL：POSIX 下
        subprocess 对被信号杀死的子进程报出**负的信号号**，不是抛异常。
        第二次调用（--recover-only）放行，真的执行。
        """
        real_run = subprocess.run
        calls = {"n": 0}

        def fake_run(args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                self.assertIn("--apply", args)
                return subprocess.CompletedProcess(args, -9)   # SIGKILL
            self.assertIn("--recover-only", args)
            return real_run(args, **kwargs)

        with mock.patch.object(bootstrap.subprocess, "run", side_effect=fake_run):
            bootstrap.run_migration(self.skill, self.runtime, hermes=True)

    def test_killed_before_any_file_written_rolls_back_and_state_is_all_old(self):
        old, _ = self._seed_killed_at_step(0)
        with self.assertRaises(bootstrap.BootstrapError):
            self._run_migration_with_killed_first_attempt()
        for name, data in old.items():
            self.assertEqual(
                json.loads((self.state / name).read_text(encoding="utf-8")), data,
                f"第 0 步就被杀：{name} 必须是旧内容，才能回滚代码")
        self.assertFalse((self.state / "migrations_completed.json").exists())

    def test_killed_partway_through_rolls_back_and_state_is_all_old(self):
        """
        🔴 核心场景：杀在中间——部分文件已经是新 key，部分还是旧 key。
        这批测试要证明的就是"不能停在这种混搭状态"：要么全退回旧，
        要么全是新，中间态必须被恢复逻辑消灭掉。
        """
        total = len(self.STATE_NAMES)
        for k in range(1, total):   # 1..total-1，不含 0（上面单测）和 total（下面单测）
            with self.subTest(杀在第几个文件之后=k):
                self._new_scene()  # 每个 subTest 用一份干净现场
                old, _ = self._seed_killed_at_step(k)
                with self.assertRaises(bootstrap.BootstrapError):
                    self._run_migration_with_killed_first_attempt()
                for name, data in old.items():
                    self.assertEqual(
                        json.loads((self.state / name).read_text(encoding="utf-8")), data,
                        f"杀在第 {k} 个文件之后：{name} 必须整体退回旧内容，"
                        f"不能停在'部分新部分旧'的中间态")
                self.assertFalse(
                    (self.state / "migrations_completed.json").exists(),
                    f"杀在第 {k} 个文件之后：完成标记不该存在——那意味着还没迁完")

    def test_killed_after_everything_written_keeps_new_code_and_new_state(self):
        """
        全部写完、只差清理日志这一步被杀——状态已经是新的，绝不能回滚代码。
        """
        total = len(self.STATE_NAMES)
        _, new = self._seed_killed_at_step(total)
        self._run_migration_with_killed_first_attempt()   # 不该抛异常
        for name, data in new.items():
            self.assertEqual(
                json.loads((self.state / name).read_text(encoding="utf-8")), data,
                f"全部写完才被杀：{name} 必须保持新内容，回滚就是把成功的迁移退回去了")


class InstallerItselfKilledBeforeMigrationEverRunsTest(unittest.TestCase):
    """
    🔴 2026-08-21 第七轮复审要求覆盖的第四种场景：**安装器自身**在代码
    替换完成之后、真正调用迁移**之前**被强杀。这时新代码已经在磁盘上，
    但迁移从头到尾没被触发过——没有事务日志（begin_state_transaction
    压根没被调用），没有迁移完成标记。往后如果没有人手动重跑一次安装，
    定时任务会直接在"新代码 + 从没迁移过的旧状态"上开始判定，是这一整套
    迁移机制最初要防的那个 P0，只是换了个没被 bootstrap 覆盖到的入口。

    bootstrap.py 自己被杀时，没有任何"bootstrap 进程"能负责报告或恢复——
    唯一能补的窗口是**下一个真正碰状态的进程**，也就是每日任务。
    见 check_followup.py 里对 core.migration_marker_present() 的检查。
    """

    @staticmethod
    def _fixtures():
        """
        一份最小、自洽的台账+规则集：required_columns 与规则节点引用的列
        只涉及这 4 列，不拖 known_values/scope_filters/terminal_states 这些
        跟本测试无关的校验进来。
        """
        led = {
            "id": "trade_qq", "name": "测试台账", "line": "测试",
            "display_name": "测试", "enabled": True,
            "source": "tencent_mcp", "file_id": "FILE", "sheet_id": "S1",
            "ruleset": "trade_qq",
            "key_field": ["企业", "机构", "访客时间"], "name_field": "企业",
            "key_tiebreakers": ["目标国家地区"],
            "required_columns": ["企业", "机构", "访客时间", "目标国家地区"],
        }
        rules = {"rulesets": {"trade_qq": {"nodes": [{
            "id": "fill", "name": "①填表", "stage": "填表", "enabled": True,
            "when": [{"field": "目标国家地区", "op": "empty"}],
            "clock": {"field": "访客时间"},
            "threshold": {"days": 0, "boundary": "on"},
            "repeat": {"days": 1},
        }]}}}
        sheet = FakeSheet(["企业", "机构", "访客时间", "目标国家地区"],
                          [{"企业": "甲", "机构": "杭州分行", "访客时间": "46202",
                            "目标国家地区": ""}])
        return led, rules, sheet

    def test_daily_job_refuses_to_judge_when_migration_was_never_attempted(self):
        led, rules, sheet = self._fixtures()
        # 没有写 migrations_completed.json——模拟"安装器换完代码就被杀，
        # 迁移从没被调用过"。
        with temp_home(ledgers={"ledgers": [led]}, rules=rules):
            r = run_main([], sheet)   # 真实运行（不带 --dry-run）
        self.assertNotEqual(r.code, 0,
                            "🔴 有台账配了 key_tiebreakers 但迁移从没跑过，"
                            "必须拒绝判定，不能假装状态是可信的")
        self.assertFalse(r.posts, "拒绝判定就不该有任何催办被推送出去")
        self.assertIn("状态迁移未确认完成", r.err)

    def test_daily_job_proceeds_once_marker_exists(self):
        """
        对照组：跟上一条**只差标记文件存不存在**，其余 fixtures 逐字节相同——
        证明这条新护栏卡住的确实是"标记缺失"这一件事，不是误伤了别的什么。
        """
        led, rules, sheet = self._fixtures()
        marker = {"migrations_completed.json":
                 {"rc15_key_tiebreakers": "2026-08-21T09:00:00+08:00"}}
        with temp_home(ledgers={"ledgers": [led]}, rules=rules, state=marker):
            r = run_main([], sheet)
        self.assertEqual(r.code, 0, r.err)


if __name__ == "__main__":
    unittest.main()
