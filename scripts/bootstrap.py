#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目跟进精灵的统一安装入口（Python 标准库，无第三方依赖）。"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
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


def package_root() -> Path:
    return Path(__file__).resolve().parent.parent


def should_ignore(_directory: str, names: Iterable[str]) -> set[str]:
    """shutil.copytree 的 ignore 回调。判断逻辑与打包共用 _manifest.should_skip。"""
    return {name for name in names if _manifest.should_skip(name)}


def copy_package(source: Path, destination: Path) -> None:
    """只覆盖包内代码，不删除目标目录中的任何文件。"""
    if source.resolve() == destination.resolve():
        return

    try:
        destination.mkdir(parents=True, exist_ok=True)
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


def install_workbuddy(source: Path, workspace: Path) -> tuple[Path, Path]:
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BootstrapError(f"当前工作空间不可写：{workspace}：{exc}") from exc

    skill_dir = workspace / ".followup-genie"
    runtime_home = workspace / "runtime"
    if skill_dir.is_symlink() or runtime_home.is_symlink():
        raise BootstrapError("程序目录和运行目录不能是符号链接")
    copy_package(source, skill_dir)
    run_setup(skill_dir, runtime_home)
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
    copy_package(source, skill_dir)
    run_setup(skill_dir, home, hermes=True)
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
