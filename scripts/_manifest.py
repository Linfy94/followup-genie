#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交付包清单 —— **单一来源**。

═══════════════════════════════════════════════════════════════════════
为什么单独一个文件：这份清单原本在两处各写了一份，
`scripts/bootstrap.py`（装到目标位置）和 `scripts/build_release.py`（打 zip），
然后它们**漂移了**：

    bootstrap 装的      比 release zip 多 .gitignore / CHANGELOG.md / run_tests.sh
    release zip 解压的  两者都没有 tests/

同一个产品，两条安装路径装出不同的文件集。最难受的是
**bootstrap 给了 run_tests.sh 却没给 tests/** —— 业务点一下必然报错，
一个坏掉的自测按钮比没有按钮更糟。

所以清单提到这里，两边 import 同一份，并有 tests/test_manifest.py 守着。
═══════════════════════════════════════════════════════════════════════

收录标准（用户 2026-08-03 定）：**最小能让业务接入其他台账并自测的范围。**

  接入台账 → templates/（配置模板）+ docs/（怎么改）
  自测     → run_tests.sh **和 tests/**，两者缺一不可
  跑起来   → scripts/
  必读     → README / SKILL / KNOWN_ISSUES / SECURITY / VERSION

按这条标准落选的：
  · .gitignore —— 业务不用 git

🔴 **CHANGELOG.md 曾经落选，2026-08-17 改回收录 —— 别再按旧理由删掉它。**

  旧理由是「不服务上面任何一条，完整记录在 GitHub 仓库里」，看着成立，
  但它默认了「不收录 == 生产目录里没有」。实际不是：

    生产目录里躺着一份 2026-08-12 的旧副本，首条写着 `## 0.6.0-rc1`，
    而同目录的 VERSION 已经是 0.6.0-rc9。bootstrap 按清单同步，
    清单里没有它 → **永远不会被刷新**，于是它一直声称生产是 rc1。

  2026-08-17 复查时就照着它误判了一次生产版本。
  **一份会说谎的文件，比没有这份文件糟。** 收录它是最省事的堵法：
  每次部署自动刷新，生产目录从此自己说得清自己是哪一版。
"""

from __future__ import annotations  # 兼容 Python 3.9（macOS 自带版本）

# 顶层文件。顺序无关，但保持字母序方便比对。
TOP_FILES = (
    "CHANGELOG.md",
    "KNOWN_ISSUES.md",
    "README.md",
    "SECURITY.md",
    "SKILL.md",
    "VERSION",
    "run_tests.sh",
)

# 整目录收录。
# 🔴 tests 必须在这里：run_tests.sh 的内容是 `unittest discover -s tests`，
#    只给脚本不给目录等于给了一个必然报错的按钮。
TOP_DIRS = ("docs", "scripts", "templates", "tests")

# 打包与安装时一律跳过的名字（目录或文件）。
# followup / runtime / state 是**运行时数据**，绝不能进包 ——
# 它们含业务的真实配置、催办状态，以及 .env 里的凭证。
EXCLUDED_NAMES = frozenset({
    ".DS_Store",
    ".env",
    ".git",
    "__pycache__",
    "followup",
    "notes",        # 开发过程记录，含开发机路径，不交付
    "runtime",
    "state",
})

# 后缀命中即跳过。
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".log")

# 开发工具，装到业务那边没有意义。
# （它仍留在仓库里，只是不进交付包。）
EXCLUDED_SCRIPT_NAMES = frozenset({"build_release.py"})


# 会写「当前版本是 x.y.z」的交付文件。打包时逐个核对必须与 VERSION 一致。
#
# 🔴 为什么要有这份清单：0.3.0-rc1 发布后，这两份 docs 里仍写着 `0.2.0-rc2`，
#    而当时的守卫只查 README 与 SKILL.md，一路放行。业务打开手册看到的
#    是上一版的版本号 —— 版本号写错不会让程序崩，只会让人对不上账。
#
VERSION_STAMPED_FILES = (
    "KNOWN_ISSUES.md",
    "README.md",
    "SKILL.md",
    "docs/WorkBuddy代理指令.md",
    "docs/业务操作手册-零基础版.md",
)

# 已知问题按版本记录历史修复，因此必须包含当前版本，
# 但也允许出现旧版本作为历史引用。
VERSION_HISTORY_FILES = frozenset({"KNOWN_ISSUES.md"})


# 交付文件中允许出现的虚构组织全称。
# 新增带「公司/集团/医院/酒店/合作社」后缀的测试名时，必须显式登记；
# 未登记的名称会被 build_release.py 拦下。
ALLOWED_EXAMPLE_ORGANIZATIONS = frozenset({
    "东海市示例城市建设投资集团有限公司",
    "同名企业有限公司",
    "星河示例科技有限公司",
    "云帆示例能源有限公司",
    "某某生物技术股份有限公司",
    "某某科技有限公司",
    "某某酒店管理有限公司",
})


def should_skip(name: str) -> bool:
    """一个文件/目录名要不要跳过。打包与安装共用同一判断。"""
    return (
        name in EXCLUDED_NAMES
        or name.endswith(EXCLUDED_SUFFIXES)
        or ".corrupt." in name
    )
