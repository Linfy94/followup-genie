#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
真实客户名不许进任何**会被推上去**的文件。

═══════════════════════════════════════════════════════════════════════
🔴 2026-08-17 发现：`Linfy94/followup-genie` 是 **PUBLIC** 仓库，
   而三家真实客户名从 0.6.0-rc5 起就随 `CHANGELOG.md` 推了上去。

   为什么没被拦住：`build_release.py` 的「未登记组织名」检查**只扫发布包**
   （`validate_contents` 走的是打包清单），而当时 CHANGELOG.md 不在清单里 ——
   于是它既不进包、也不被检查，却**照样进了公开仓**。
   守错了边界：真正外发的是 git push，不是 zip。

   这条测试把同一个判据（复用 `build_release.unapproved_organizations`，
   不另写一份 —— 坑 #12）扩到**所有被 git 跟踪的文本文件**。
   以后写客户名进任何要推的文件，跑测试时当场红。

⚠️ 已经进了 git 历史的那三个名字，本次只清理当前内容、**不改写历史**
   （用户 2026-08-17 决定：不做强推这类不可逆操作）。
   所以这条测试守的是「从今往后不再新增」，不是「历史已干净」。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from harness import core  # noqa: F401 —— 挂 sys.path

ROOT = Path(__file__).resolve().parent.parent

# 只查文本。二进制（图片等）由打包环节的其他敏感规则管。
TEXT_SUFFIXES = {".md", ".py", ".json", ".jsonc", ".sh", ".txt", ".yml", ".yaml", ""}

# 🔴 检查器会扫到自己：`build_release.py` 里写的就是那条正则本身
#    （几个公司后缀的或分支），于是它永远自我命中。
#    这里也刻意不逐字抄那串后缀 —— 抄一遍，这个文件也会自我命中。
#
#    2026-08-18：生产树自己的历史里躺着好几份 `.upgrade-rollback-*/`
#    回滚备份（铁律④留下的，不许删），每一份都带一份当时的
#    `scripts/build_release.py`，同样会自我命中。按精确路径排除只挡得住
#    当前这一份，历史备份份份都得手动加——这不叫「只排除一个文件」，
#    是「排除清单会无限变长」。改成按「路径以 scripts/build_release.py
#    结尾」匹配：判据定义处只会以这个相对路径出现，不管它是当前版本
#    还是某次回滚备份里的旧版本，内容性质完全一样（都不是客户数据流经的地方）。
def is_validator_definition(rel: str) -> bool:
    return rel == "scripts/build_release.py" or rel.endswith("/scripts/build_release.py")


def tracked_text_files() -> list[Path] | None:
    """
    被 git 跟踪的文本文件；**不在 git 仓里时返回 None**。

    🔴 业务装出来的副本没有 .git（打包时 `.git` 就在排除名单里），
       在那里 `git ls-files` 必然失败。若让它抛异常，
       业务点「自测」就会看到一条红 —— 而 `_manifest.py` 的头注写得很清楚：
       **一个坏掉的自测按钮比没有按钮更糟**。

       这道检查守的是「从开发仓推出去的东西」，装完的副本本来就没有推的动作，
       跳过是对的，不是放水。
    """
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    files = []
    for rel in out.split("\0"):
        if not rel:
            continue
        p = ROOT / rel
        if is_validator_definition(rel):
            continue
        if p.suffix.lower() in TEXT_SUFFIXES and p.is_file():
            files.append(p)
    return files


def _validator():
    """
    拿到判据。**`build_release.py` 不进交付包**（`EXCLUDED_SCRIPT_NAMES`），
    所以装出来的副本 import 不到它 —— 模块级 import 会让业务的自测按钮直接报错。
    """
    try:
        import build_release
    except ImportError:
        return None
    return build_release.unapproved_organizations


class NoCustomerNamesTest(unittest.TestCase):

    def test_the_check_itself_still_works(self):
        """
        🔴 先证明判据没坏。否则「全仓干净」可能只是因为检查恒返回空 ——
           那是这个项目栽过的「假绿」（坑 #11）。
        """
        check = _validator()
        if check is None:
            self.skipTest("交付副本里没有 build_release.py，本来就不做这道检查")
        # 🔴 样本**运行时拼**，源码里不出现完整名字：
        #    否则这个文件自己就会被下面那条扫描命中，
        #    而把它登记进白名单又会让判据把它抹掉 —— 证明就废了。
        #    （第一版正是这么写的，白名单一加，这条当场变成恒假。）
        sample = "某年某月，示范样板" + "有限公司" + "完成了安装"
        self.assertTrue(check(sample.encode("utf-8")),
                        "判据认不出组织名了，这条测试就什么都证明不了")

    def test_only_the_validator_itself_is_exempt(self):
        """🔴 白名单式排除最容易越长越大。钉住豁免规则只认「判据定义处」。"""
        import test_no_customer_names_in_repo as mod
        self.assertTrue(mod.is_validator_definition("scripts/build_release.py"))
        self.assertTrue(mod.is_validator_definition(
            ".upgrade-rollback-20260817-102210/scripts/build_release.py"))
        self.assertFalse(mod.is_validator_definition("scripts/core.py"))
        self.assertFalse(mod.is_validator_definition("notes/build_release.py"))

    def test_no_tracked_file_carries_a_real_customer_name(self):
        files = tracked_text_files()
        if files is None:
            self.skipTest("不在 git 仓里（业务装出来的副本），没有推送动作要守")
        check = _validator()
        if check is None:
            self.skipTest("交付副本里没有 build_release.py，本来就不做这道检查")
        offenders = {}
        for path in files:
            try:
                names = check(path.read_bytes())
            except OSError:
                continue
            if names:
                offenders[path.relative_to(ROOT).as_posix()] = names
        self.assertEqual(
            offenders, {},
            "这些被 git 跟踪的文件里有疑似真实客户名 —— 仓库是公开的，"
            "推上去就收不回了。改成脱敏描述，或把示例名加进 "
            "_manifest.ALLOWED_EXAMPLE_ORGANIZATIONS：\n"
            + "\n".join(f"  {f}：{'、'.join(n)}" for f, n in offenders.items()))


if __name__ == "__main__":
    unittest.main()
