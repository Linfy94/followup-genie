#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目跟进精灵的统一安装入口（Python 标准库，无第三方依赖）。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable

import _manifest


# 🔴 清单来自 _manifest.py —— **与 build_release.py 用同一份**。
#    这两处原本各写一份然后漂移了：装出来的文件集不一样，
#    而且这里给了 run_tests.sh 却没给 tests/，自测按钮是坏的。
PACKAGE_FILES = _manifest.TOP_FILES
PACKAGE_DIRS = _manifest.TOP_DIRS


class BootstrapError(RuntimeError):
    """可直接向业务说明的安装故障。"""


class StateRecoveryUnsafe(BootstrapError):
    """
    迁移子进程异常退出（含被强杀）之后，连"状态改写有没有走完"都判断
    不清楚——见 run_migration() 里对 --recover-only 退出码 1 的处理。

    🔴 2026-08-21 第七轮复审：**这个异常绝不能走普通 BootstrapError 的
    自动回滚路径**。install_or_rollback() 收到普通 BootstrapError 时会
    把代码退回旧版、然后放心地说"现有的定时任务仍在跑原来那一版"——
    但这里我们判不清状态是新是旧、完不完整，回滚代码在信息不足的情况下
    又是一次赌注：旧代码遇上一个它完全没见过的中间态状态目录，行为
    没有任何保证。原样留在现场，让人来看，比再赌一把安全。
    """


def package_root() -> Path:
    return Path(__file__).resolve().parent.parent


def should_ignore(_directory: str, names: Iterable[str]) -> set[str]:
    """shutil.copytree 的 ignore 回调。判断逻辑与打包共用 _manifest.should_skip。"""
    return {name for name in names if _manifest.should_skip(name)}


def _validate_managed_paths(source: Path, destination: Path) -> None:
    """复制前拒绝会越界写入的链接和会造成半升级的类型冲突。"""
    if source.is_symlink() or destination.is_symlink():
        raise BootstrapError("安装包目录和目标程序目录不能是符号链接")

    for name in PACKAGE_FILES:
        src = source / name
        if src.is_symlink() or not src.is_file():
            raise BootstrapError(f"安装包中的 {name} 必须是普通文件")
        dst = destination / name
        if dst.is_symlink():
            raise BootstrapError(f"目标程序中的 {name} 是符号链接，已停止以免越界写入")
        if dst.exists() and not dst.is_file():
            raise BootstrapError(f"目标程序中的 {name} 应该是文件，实际类型不一致")
        if dst.exists() and dst.stat().st_nlink > 1:
            raise BootstrapError(f"目标程序中的 {name} 是硬链接，已停止以免覆盖外部文件")

    for name in PACKAGE_DIRS:
        src = source / name
        if src.is_symlink() or not src.is_dir():
            raise BootstrapError(f"安装包中的 {name}/ 必须是普通目录")
        dst = destination / name
        if dst.is_symlink():
            raise BootstrapError(f"目标程序中的 {name}/ 是符号链接，已停止以免越界写入")
        if dst.exists() and not dst.is_dir():
            raise BootstrapError(f"目标程序中的 {name}/ 应该是目录，实际类型不一致")

        for base, dirs, files in os.walk(src, followlinks=False):
            for child in dirs + files:
                p = Path(base) / child
                if p.is_symlink():
                    rel = p.relative_to(source)
                    raise BootstrapError(f"安装包中不接受符号链接：{rel}")

        if not dst.exists():
            continue
        for base, dirs, files in os.walk(dst, followlinks=False):
            for child in dirs + files:
                p = Path(base) / child
                rel = p.relative_to(destination)
                if p.is_symlink():
                    raise BootstrapError(
                        f"目标程序中的 {rel} 是符号链接，已停止以免越界写入")
                if p.is_file() and p.stat().st_nlink > 1:
                    raise BootstrapError(
                        f"目标程序中的 {rel} 是硬链接，已停止以免覆盖外部文件")


def retire_stale_code(source: Path, destination: Path) -> list[str]:
    """
    把上一版有、这一版没有的文件挪出执行路径，返回被挪走的相对路径。

    ═══════════════════════════════════════════════════════════════════
    🔴 0.3.0-rc1 的 copy_package 是**纯合并**（docstring 原文：
       「不删除目标目录中的任何文件」）。于是升级后：

         上一版的 scripts/old_module.py 还躺在 scripts/ 里
           → 仍然 import 得到、仍然跑得起来
             → `unittest discover -s tests` 还会跑上一版的测试

       两个版本的代码混在同一个目录里执行，出了问题连是哪一版都说不清。

    ⚠️ 挪走而不是删除：
       ① 用户的一贯要求是「不删除文件，只新增」；
       ② 升级出问题时还能翻回来看上一版到底是什么样。

    去处是 `.upgrade-backup-<时间戳>/`：以 `.` 开头、且不在 _manifest 清单里，
    import 和 unittest discover 都够不着 —— **失效但没销毁**。

    🔴 范围严格限定在清单内（docs/ scripts/ templates/ tests/ 与那几个顶层
       文件）。运行时数据（followup/ state/ runtime/ .env）、以及清单之外的
       任何东西（生产目录里的 notes/、CHANGELOG.md）**一律不碰**。
    ═══════════════════════════════════════════════════════════════════
    """
    retired: list[str] = []
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = destination / f".upgrade-backup-{stamp}"

    def move(item: Path, rel: str) -> None:
        target = backup / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        # 备份与程序目录在同一文件系统；os.replace 是单次原子改名，失败时
        # 源文件仍在原位，不会出现「源已删、备份又没写成」。
        os.replace(str(item), str(target))
        retired.append(rel)

    for name in PACKAGE_DIRS:
        dst_dir = destination / name
        src_dir = source / name
        if not dst_dir.is_dir():
            continue
        for item in sorted(dst_dir.rglob("*"), key=lambda p: -len(p.parts)):
            rel_in_dir = item.relative_to(dst_dir)
            rel = f"{name}/{rel_in_dir.as_posix()}"

            # __pycache__ 一律挪走：里面的 .pyc 对应的是上一版的源码。
            if item.is_dir() and item.name == "__pycache__":
                move(item, rel)
                continue
            if item.is_dir():
                continue
            # 运行时数据与本来就不该进包的东西，不属于我们管的范围
            if any(_manifest.should_skip(part) for part in rel_in_dir.parts):
                continue
            if not (src_dir / rel_in_dir).exists():
                move(item, rel)

    return retired


def verify_package(source: Path) -> None:
    """
    替换旧版**之前**，先确认新版自己是完整的、至少语法上跑得起来。

    🔴 顺序很重要。以前是「先覆盖，再让 setup 去发现问题」——
    包缺文件或某个 .py 有语法错时，旧版已经被覆盖掉了，
    而定时任务照样会在第二天早上 9:00 触发**半升级的代码**。
    催办类工具最怕的就是这种：它不会崩得很响，只会「今天没有要催的」。

    这里只做便宜且确定的检查（清单齐全 + 能编译），不 import 新版代码 ——
    import 会执行模块顶层语句，等于在校验阶段就把新版跑起来了。
    """
    missing = [n for n in PACKAGE_FILES if not (source / n).is_file()]
    missing += [f"{n}/" for n in PACKAGE_DIRS if not (source / n).is_dir()]
    if missing:
        raise BootstrapError(
            "安装包不完整，缺少：" + "、".join(missing) + "。已停止，未改动现有程序。")

    for entry in ("scripts/check_followup.py", "scripts/core.py",
                  "scripts/doctor.py", "scripts/setup.sh"):
        if not (source / entry).is_file():
            raise BootstrapError(
                f"安装包缺少关键文件 {entry}。已停止，未改动现有程序。")

    version = (source / "VERSION").read_text(encoding="utf-8").strip()
    if not version:
        raise BootstrapError("安装包里的 VERSION 是空的。已停止，未改动现有程序。")

    bad = []
    for py in sorted((source / "scripts").rglob("*.py")):
        if _manifest.should_skip(py.name):
            continue
        try:
            compile(py.read_text(encoding="utf-8"), str(py), "exec")
        except (SyntaxError, ValueError, UnicodeDecodeError) as exc:
            bad.append(f"{py.relative_to(source)}：{exc}")
    if bad:
        raise BootstrapError(
            "安装包里有无法编译的脚本，已停止，未改动现有程序：\n  "
            + "\n  ".join(bad))


def snapshot_code(destination: Path) -> "Path | None":
    """
    把现有版本的代码原样存一份，供升级失败时恢复。返回快照目录（首次安装返回 None）。

    只存清单范围内的东西。业务配置、状态、凭证本来就不在清单里，
    不进快照，也就绝不可能被恢复动作覆盖掉。
    """
    if not destination.is_dir():
        return None
    if not any((destination / n).exists() for n in PACKAGE_FILES + PACKAGE_DIRS):
        return None      # 空目录，算首次安装
    snap = destination / f".upgrade-rollback-{datetime.now():%Y%m%d-%H%M%S}"
    snap.mkdir(parents=True, exist_ok=True)
    for name in PACKAGE_FILES:
        src = destination / name
        if src.is_file():
            shutil.copy2(src, snap / name)
    for name in PACKAGE_DIRS:
        src = destination / name
        if src.is_dir():
            shutil.copytree(src, snap / name, dirs_exist_ok=True,
                            copy_function=shutil.copy2, ignore=should_ignore)
    return snap


def restore_snapshot(snapshot: Path, destination: Path) -> None:
    """
    把 destination 的清单范围恢复成 snapshot 里的样子。

    **不删除任何东西**：现场那一份先挪进 `<快照>/replaced/`，再把旧版拷回来。
    这样既不会留下新版多出来的文件（半升级），也留得住现场供事后查。

    清单之外的一切（followup/、runtime/、state/、.env、notes/、
    以及 retire_stale_code 留下的 .upgrade-backup-*）一概不碰。
    """
    replaced = snapshot / "replaced"
    replaced.mkdir(parents=True, exist_ok=True)
    for name in PACKAGE_FILES + PACKAGE_DIRS:
        cur = destination / name
        if cur.exists():
            target = replaced / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                shutil.rmtree(target) if target.is_dir() else target.unlink()
            shutil.move(str(cur), str(target))
        old = snapshot / name
        if old.is_file():
            shutil.copy2(old, destination / name)
        elif old.is_dir():
            shutil.copytree(old, destination / name, dirs_exist_ok=True,
                            copy_function=shutil.copy2)


def install_or_rollback(source: Path, skill_dir: Path, runtime_home: Path,
                        *, hermes: bool = False) -> list[str]:
    """
    先校验新版 → 存一份旧版 → 复制 → 跑 setup 自检 → （升级时）迁移旧状态。
    任何一步失败都把旧版原样放回去，绝不让定时任务在半升级的代码或
    未迁移的状态上跑。
    """
    verify_package(source)
    snap = snapshot_code(skill_dir)
    try:
        retired = copy_package(source, skill_dir)
        _report_retired(retired)
        run_setup(skill_dir, runtime_home, hermes=hermes)
        if snap is not None:  # 升级才需要迁移；首次安装没有旧状态
            run_migration(skill_dir, runtime_home, hermes=hermes)
        return retired
    except StateRecoveryUnsafe:
        # 🔴 刻意不回滚代码、不碰快照——见 StateRecoveryUnsafe 的说明。
        #    状态已经判不清了，这里再去动代码只是在信息不足时又赌一把。
        raise
    except BootstrapError:
        if snap is not None:
            try:
                restore_snapshot(snap, skill_dir)
                print(f"↩️ 升级失败，已把原来那一版原样放回去（现场留在 {snap.name}/replaced/，"
                      f"没有删除任何东西）。现有的定时任务仍在跑原来那一版。",
                      file=sys.stderr)
            except OSError as exc:
                # 恢复都失败了，绝不能让人以为「没事」。
                raise BootstrapError(
                    f"升级失败，且自动恢复也失败了：{exc}\n"
                    f"🔴 程序目录现在可能是半升级状态。"
                    f"旧版完整地留在 {snap}，请把它手工拷回 {skill_dir}。"
                ) from exc
        raise


def copy_package(source: Path, destination: Path) -> list[str]:
    """
    把包内代码覆盖过去，并把上一版残留的代码挪出执行路径。

    返回被挪走的相对路径列表（正常首次安装为空）。
    """
    if source.resolve() == destination.resolve():
        return []

    try:
        destination.mkdir(parents=True, exist_ok=True)
        _validate_managed_paths(source, destination)
        for name in PACKAGE_FILES:
            src = source / name
            if src.is_file():
                shutil.copy2(src, destination / name)
        for name in PACKAGE_DIRS:
            src = source / name
            if src.is_dir():
                shutil.copytree(
                    src,
                    destination / name,
                    dirs_exist_ok=True,
                    copy_function=shutil.copy2,
                    ignore=should_ignore,
                )
        # 🔴 必须在复制**之后**：先复制新版，再拿新版当基准清点残留。
        return retire_stale_code(source, destination)
    except OSError as exc:
        raise BootstrapError(
            f"无法把程序安装到 {destination}：{exc}"
        ) from exc


def run_setup(skill_dir: Path, runtime_home: Path, hermes: bool = False) -> None:
    script = skill_dir / "scripts" / ("install.sh" if hermes else "setup.sh")
    if not script.is_file():
        raise BootstrapError(f"安装包不完整，缺少：{script}")

    env = os.environ.copy()
    env.pop("FOLLOWUP_HOME", None)
    if hermes:
        env["HERMES_HOME"] = str(runtime_home)
    else:
        env.pop("HERMES_HOME", None)
        env["FOLLOWUP_HOME"] = str(runtime_home)

    try:
        result = subprocess.run(
            ["bash", str(script)],
            cwd=str(skill_dir),
            env=env,
            check=False,
        )
    except OSError as exc:
        raise BootstrapError(f"无法启动安装程序：{exc}") from exc
    if result.returncode != 0:
        raise BootstrapError(
            f"安装程序未通过检查（退出码 {result.returncode}），已停止，未启用自动化。"
        )


# 🔴 跟 migrate_rc15_key_state.py 里的同名常量必须一致——这里特意不去
# import 那个脚本（它是子进程跑的，bootstrap.py 一贯不依赖 scripts/ 下
# 除 _manifest 之外的任何模块，保持"就算 core.py 坏了，装机本身还能跑"
# 这条隔离性），所以两边各自定义，靠这条注释互相看得见对方。
_MIGRATION_MARKER_FILE = "migrations_completed.json"
_MIGRATION_ID = "rc15_key_tiebreakers"


def _migration_already_done(runtime_home: Path) -> bool:
    """
    迁移干净跑完一次就不用再跑——见 migrate_rc15_key_state.py 里
    `_mark_migration_done()` 的说明。跳过判断本身只读本地文件，
    不联网、不需要凭证、不用取运行锁。
    """
    marker = runtime_home / "followup" / "state" / _MIGRATION_MARKER_FILE
    if not marker.is_file():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False  # 标记文件本身读不出来，保守起见当作没做过，照常跑一次
    return isinstance(data, dict) and _MIGRATION_ID in data


def run_migration(skill_dir: Path, runtime_home: Path, hermes: bool = False) -> None:
    """
    升级时把 rc14 旧 key 迁到 rc15 新 key（见 migrate_rc15_key_state.py）。

    ═══════════════════════════════════════════════════════════════════
    🔴 2026-08-21 复审发现：这一步原来压根没接进安装流程——迁移脚本写好了，
    但没人会在升级时真的调它。已装 rc14 的业务升级后，代码换了但状态没迁，
    第一次真跑就可能触发一批多余的首次催办。定时任务本身不归 bootstrap
    管（cron 是装完之后人手动注册的一次性动作），能补的窗口只有：**升级
    这个动作本身完成之前**，把迁移做完、做不完就跟 run_setup 一样整体
    失败回滚——不让新代码在旧状态上开始跑。

    只在**升级**（`snap is not None`，见 install_or_rollback）时调用；
    首次安装没有旧状态可迁，硬跑一次纯属徒增一次不必要的联网取数。

    🔴 2026-08-21 第四轮复审又指出：迁移一旦真的跑成功过一次，往后
    **每一次**升级都还会重新联网读一遍配了 key_tiebreakers 的台账——
    多余，而且让日后所有升级都莫名其妙依赖一次跟那次改动毫无关系的
    网络请求，网络或权限一抖就会把整次升级挡下来。现在先看
    `migrations_completed.json` 这个标记，标记在就直接跳过，
    不联网、不进子进程、不占运行锁。
    ═══════════════════════════════════════════════════════════════════
    """
    if _migration_already_done(runtime_home):
        print("ℹ️ rc15 主键迁移此前已完成，跳过（不重新联网核对）。")
        return

    script = skill_dir / "scripts" / "migrate_rc15_key_state.py"
    if not script.is_file():
        raise BootstrapError(f"安装包不完整，缺少：{script}")

    env = os.environ.copy()
    env.pop("FOLLOWUP_HOME", None)
    if hermes:
        env["HERMES_HOME"] = str(runtime_home)
    else:
        env.pop("HERMES_HOME", None)
        env["FOLLOWUP_HOME"] = str(runtime_home)

    try:
        result = subprocess.run(
            [sys.executable, str(script), "--apply"],
            cwd=str(skill_dir),
            env=env,
            check=False,
        )
    except OSError as exc:
        raise BootstrapError(f"无法启动状态迁移：{exc}") from exc
    if result.returncode == 0:
        return

    # ═══════════════════════════════════════════════════════════════════
    # 🔴 2026-08-21 第七轮复审指出的 P0：迁移子进程失败——**包括被强杀**
    # （SIGKILL 这类连 except 都拦不住的信号，subprocess.run 会报出负的
    # 信号号当退出码）。原来这里直接 raise BootstrapError，外层
    # install_or_rollback() 收到就把代码退回旧版、并且放心地打印"现有的
    # 定时任务仍在跑原来那一版"。但旧代码完全不认识 rc19 才有的这套
    # 状态事务日志机制——它不会去处理日志、不会去复原，日志会孤零零地
    # 留在盘上，而状态可能已经被改了一部分。旧代码在这种"部分新、部分旧"
    # 的状态上直接开始跑，正是这一整套机制最初要防的那个 P0，只是换了
    # 个没被覆盖到的触发路径。
    #
    # 补法：趁新版代码**还没被换掉**，用它自己的恢复程序（--recover-only，
    # 见 migrate_rc15_key_state.py 里的说明）先把状态判个清楚，再决定
    # 「能不能回滚代码」——而不是想当然地直接回滚。
    # ═══════════════════════════════════════════════════════════════════
    try:
        recovery = subprocess.run(
            [sys.executable, str(script), "--recover-only"],
            cwd=str(skill_dir), env=env, check=False,
        )
    except OSError as exc:
        # 连恢复程序都启动不了——比"判不清"更糟，同样不能自称安全。
        raise StateRecoveryUnsafe(
            f"升级后的状态迁移异常退出（退出码 {result.returncode}），"
            f"且恢复程序本身都无法启动（{exc}）。当前状态可能不完整，"
            f"已停止升级，**未回滚代码**（回滚代码但不确定状态是否配套，"
            f"同样不安全）。请人工核对 {runtime_home / 'followup' / 'state'} "
            f"下的状态文件与备份目录，确认后再继续。"
        ) from exc

    if recovery.returncode in (0, 3):
        # 0：状态从头到尾没被碰过（多半是败在联网取数那一步，走到不了
        #    写状态那里）；3：确认没写完，已经照备份整体复原。两种都是
        #    "状态回到了旧代码认得的样子"，可以放心让下面的异常触发回滚。
        raise BootstrapError(
            f"升级后的状态迁移未完成（退出码 {result.returncode}），"
            f"状态已确认回到升级前的样子，正在回滚代码。"
        )
    if recovery.returncode == 2:
        # 其实已经完整写完，只是清理日志那一步没走完——状态是新的，
        # 回滚代码反而会制造"状态新、代码旧"。不算失败，正常返回，
        # 让 install_or_rollback() 继续走下去、保留新代码。
        print("ℹ️ 迁移子进程虽然异常退出，但状态经核对其实已经完整写完"
              "（多半是清理日志这最后一步被打断）。保留新代码，不回滚。")
        return

    # recovery.returncode 既不是 0/2/3——判不清，绝不能自称"安全"。
    raise StateRecoveryUnsafe(
        f"升级后的状态迁移异常退出（退出码 {result.returncode}），且自动恢复"
        f"也没能确认状态是否完整（恢复程序退出码 {recovery.returncode}）。\n"
        f"🔴 当前状态可能处于不完整的中间态，无法判断是新是旧。"
        f"已停止升级，**未回滚代码**（回滚代码但不确定状态是否配套，"
        f"同样不安全）。请人工核对 {runtime_home / 'followup' / 'state'} "
        f"下的状态文件、.state-transaction.json 与 .migrate-rc15-backup-* "
        f"备份目录，确认后再继续。在此之前，不应该让定时任务基于当前状态运行。"
    )


def _report_retired(retired: list[str]) -> None:
    """升级挪走了旧代码就说一声。悄悄搬走和悄悄留下同样不该。"""
    if not retired:
        return
    print(f"ℹ️ 升级：{len(retired)} 个上一版残留的文件已移出执行路径"
          f"（挪到 .upgrade-backup-*/，未删除）：")
    for rel in retired[:10]:
        print(f"   · {rel}")
    if len(retired) > 10:
        print(f"   · …另有 {len(retired) - 10} 个")


def install_workbuddy(source: Path, workspace: Path) -> tuple[Path, Path]:
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BootstrapError(f"当前工作空间不可写：{workspace}：{exc}") from exc

    skill_dir = workspace / ".followup-genie"
    runtime_home = workspace / "runtime"
    if skill_dir.is_symlink() or runtime_home.is_symlink():
        raise BootstrapError("程序目录和运行目录不能是符号链接")
    install_or_rollback(source, skill_dir, runtime_home)
    return skill_dir, runtime_home


def locate_hermes_install(explicit: str | None) -> Path:
    """
    定位一个**已经存在**的 Hermes 安装。找不到就报错。

    🔴 这**不是** core.hermes_home() 那个「解析运行时目录」。两者容易混：

      core.hermes_home()      运行时用。目录不存在也照样返回路径 ——
                              因为引导阶段本来就要去创建它
      locate_hermes_install() 安装时用。目录必须已存在 ——
                              往一个不存在的 Hermes 里装 skill 没有意义，
                              静默创建 ~/.hermes 只会让人以为装好了

    名字分开是为了让下次读代码的人不必再推一遍这个区别。
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    configured = os.environ.get("HERMES_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    default = Path.home() / ".hermes"
    if default.is_dir():
        return default.resolve()
    raise BootstrapError(
        "找不到 Hermes 目录。请由 Agent 确认 Hermes 已安装，"
        "并使用 --hermes-home 指定其目录。"
    )


def install_hermes(source: Path, home: Path) -> tuple[Path, Path]:
    skill_dir = home / "skills" / "work" / "followup-genie"
    if skill_dir.is_symlink() or home.is_symlink():
        raise BootstrapError("Hermes 目录和 Skill 目录不能是符号链接")
    install_or_rollback(source, skill_dir, home, hermes=True)
    return skill_dir, home


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把项目跟进精灵安装到 WorkBuddy 当前工作空间或 Hermes。"
    )
    parser.add_argument("--host", choices=("workbuddy", "hermes"), required=True)
    parser.add_argument(
        "--workspace",
        help="WorkBuddy 当前工作空间；省略时使用当前目录。",
    )
    parser.add_argument(
        "--hermes-home",
        help="Hermes 运行目录；默认读取 HERMES_HOME 或 ~/.hermes。",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source = package_root()
    try:
        if args.host == "workbuddy":
            workspace = Path(args.workspace or os.getcwd()).expanduser().resolve()
            skill_dir, runtime_home = install_workbuddy(source, workspace)
        else:
            home = locate_hermes_install(args.hermes_home)
            skill_dir, runtime_home = install_hermes(source, home)
    except BootstrapError as exc:
        print(f"❌ 项目跟进精灵安装失败：{exc}", file=sys.stderr)
        return 2

    version_path = skill_dir / "VERSION"
    version = (
        version_path.read_text(encoding="utf-8").strip()
        if version_path.is_file()
        else "未知"
    )
    print()
    print("✅ 项目跟进精灵安装完成")
    print(f"   宿主：{args.host}")
    print(f"   版本：{version}")
    print(f"   程序目录：{skill_dir}")
    print(f"   运行目录：{runtime_home}")
    print("   已完成离线配置检查；尚未读取正式台账、发送消息或创建自动化。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
