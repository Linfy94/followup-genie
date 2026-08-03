#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成项目跟进精灵的 GitHub Release 安装包（仅 Python 标准库）。"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
import zipfile
from pathlib import Path

import _manifest


ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_ROOT = "followup-genie"

# 🔴 清单来自 _manifest.py —— **打包与安装必须用同一份**。
#    这两处原本各写一份，然后漂移了：装出来的文件集不一样，
#    而且 bootstrap 给了 run_tests.sh 却没给 tests/，自测按钮是坏的。
TOP_FILES = _manifest.TOP_FILES
TOP_DIRS = _manifest.TOP_DIRS
EXCLUDED_SCRIPT_NAMES = set(_manifest.EXCLUDED_SCRIPT_NAMES)
# 路径里出现这些片段就拒绝打包。注意它**不再包含 tests** ——
# tests/ 现在是交付内容（没它 run_tests.sh 跑不了），
# 但 followup/runtime/state/.env 这些运行时数据仍然一律禁止。
FORBIDDEN_PARTS = {
    ".git",
    ".env",
    "__pycache__",
    "followup",
    "notes",
    "runtime",
    "state",
}
SENSITIVE_CONTENT = (
    re.compile(rb"/Users/"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=[A-Za-z0-9_-]{16,}"),
)


class BuildError(RuntimeError):
    pass


def version() -> str:
    value = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[a-z0-9.]+)?", value):
        raise BuildError(f"VERSION 格式不合法：{value!r}")
    return value


def validate_skill_frontmatter() -> None:
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        raise BuildError("SKILL.md 缺少合法的 YAML frontmatter")

    keys = []
    for line in match.group(1).splitlines():
        if not line.strip() or line.startswith((" ", "\t")):
            raise BuildError("SKILL.md frontmatter 必须保持为简单的单行字段")
        key, separator, _value = line.partition(":")
        if not separator:
            raise BuildError(f"SKILL.md frontmatter 行无法解析：{line!r}")
        keys.append(key.strip())
    if keys != ["name", "description"]:
        raise BuildError(
            "SKILL.md frontmatter 只能依次包含 name、description；"
            f"当前是：{keys}"
        )


def package_files() -> list[Path]:
    files = []
    for name in TOP_FILES:
        path = ROOT / name
        if not path.is_file():
            raise BuildError(f"发布必需文件不存在：{path}")
        files.append(path)
    for directory in TOP_DIRS:
        base = ROOT / directory
        if not base.is_dir():
            raise BuildError(f"发布必需目录不存在：{base}")
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT)
            if any(part in FORBIDDEN_PARTS for part in relative.parts):
                continue
            if path.name.endswith((".pyc", ".pyo", ".log")):
                continue
            if ".corrupt." in path.name:
                continue
            if directory == "scripts" and path.name in EXCLUDED_SCRIPT_NAMES:
                continue
            files.append(path)
    return sorted(set(files), key=lambda item: item.relative_to(ROOT).as_posix())


def validate_contents(files: list[Path], release_version: str) -> None:
    for path in files:
        relative = path.relative_to(ROOT)
        if path.is_symlink():
            raise BuildError(f"发布包不接受符号链接：{relative}")
        if any(part in FORBIDDEN_PARTS for part in relative.parts):
            raise BuildError(f"禁止进入发布包的路径：{relative}")
        data = path.read_bytes()
        for pattern in SENSITIVE_CONTENT:
            if pattern.search(data):
                raise BuildError(f"疑似敏感内容，停止打包：{relative}")

    stale = []
    for name in _manifest.VERSION_STAMPED_FILES:
        text = (ROOT / name).read_text(encoding="utf-8")
        if release_version not in text:
            stale.append(name)
    if stale:
        raise BuildError(
            f"以下文件写的版本号与 VERSION（{release_version}）不一致："
            + "、".join(stale))


def zip_mode(path: Path) -> int:
    return 0o755 if path.suffix in {".py", ".sh"} else 0o644


def build_archive(target: Path, files: list[Path]) -> None:
    with zipfile.ZipFile(
        target, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for path in files:
            relative = path.relative_to(ROOT).as_posix()
            info = zipfile.ZipInfo(
                f"{ARCHIVE_ROOT}/{relative}",
                date_time=(2026, 1, 1, 0, 0, 0),
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (zip_mode(path) & 0xFFFF) << 16
            archive.writestr(info, path.read_bytes())


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_archive(path: Path, expected_files: list[Path]) -> None:
    expected = {
        f"{ARCHIVE_ROOT}/{item.relative_to(ROOT).as_posix()}"
        for item in expected_files
    }
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        bad = [
            name
            for name in names
            if name.startswith("/")
            or ".." in Path(name).parts
            or any(part in FORBIDDEN_PARTS for part in Path(name).parts)
        ]
        if bad:
            raise BuildError(f"压缩包出现禁止路径：{bad}")
        if names != expected:
            missing = sorted(expected - names)
            extra = sorted(names - expected)
            raise BuildError(f"压缩包内容不一致：缺少={missing}，多出={extra}")
        for name in names:
            data = archive.read(name)
            for pattern in SENSITIVE_CONTENT:
                if pattern.search(data):
                    raise BuildError(f"压缩包内疑似敏感内容：{name}")


def build(output_dir: Path) -> list[Path]:
    release_version = version()
    validate_skill_frontmatter()
    files = package_files()
    validate_contents(files, release_version)

    output_dir.mkdir(parents=True, exist_ok=True)
    names = (
        "followup-genie-agent.zip",
        f"followup-genie-agent-{release_version}.zip",
        "followup-genie-workbuddy.skill",
        f"followup-genie-workbuddy-{release_version}.skill",
        "SHA256SUMS.txt",
    )
    targets = [output_dir / name for name in names]
    existing = [path for path in targets if path.exists()]
    if existing:
        raise BuildError(
            "为避免覆盖已有发布物，输出文件已存在："
            + "、".join(str(path) for path in existing)
        )

    stable_zip, versioned_zip, stable_skill, versioned_skill, checksums = targets
    build_archive(stable_zip, files)
    shutil.copyfile(stable_zip, versioned_zip)
    shutil.copyfile(stable_zip, stable_skill)
    shutil.copyfile(stable_zip, versioned_skill)
    for archive in targets[:4]:
        verify_archive(archive, files)

    checksum_lines = [
        f"{sha256(path)}  {path.name}"
        for path in targets[:4]
    ]
    checksums.write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")
    return targets


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT.parent / f"dist-{version()}",
        help="发布物输出目录；默认在源码目录旁创建版本化目录。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        outputs = build(args.output_dir.expanduser().resolve())
    except (BuildError, OSError, zipfile.BadZipFile) as exc:
        print(f"❌ 发布包生成失败：{exc}", file=sys.stderr)
        return 2

    print("✅ 发布包生成完成")
    for path in outputs:
        print(f"   {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
