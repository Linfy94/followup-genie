#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_release.unapproved_organizations()：纯英文缩写紧跟中文后缀也要认得出。

═══════════════════════════════════════════════════════════════════════
🔴 2026-08-21 复审发现：原来的正则只认「2-60 个连续中文字符 + 公司后缀」，
   三个大写英文字母紧跟中文公司后缀的写法完全漏检——前缀不含一个中文
   字符，正则直接跳过整段。这不是当前已发现的泄露，是发布隐私守卫本身
   不完整。

   补一条英文/数字前缀分支，**只认大写字母 + 数字**，不接受小写字母。
   这是刻意的取舍：真实企业英文缩写几乎总是全大写（比如三个字母的
   缩写、或数字+字母的缩写）；第一版试过大小写混合并允许内部空格，
   结果连代码注释里"一个小写单词紧跟中文后缀词"这种纯属巧合的组合
   也会被命中——只认大写把「一段普通英文散文」和「一个缩写」分开。

🔴 本文件所有样例组织名都在**运行时拼接**，连注释里也不能写出完整
   的组合——这个文件会被打包脚本自己的组织名扫描器扫到（正是它在
   守的东西），扫描器不区分代码/注释/字符串字面量，纯粹按文件原始
   字节扫，第一版把示例写进 docstring 说明文字里，同样被当场拦下来
   报「疑似真实客户内容」。跟 rc10 那次 test_no_customer_names_in_repo.py
   踩的坑是同一个道理，这次连注释都要断开来写。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import unittest

from harness import core  # noqa: F401 —— 挂 sys.path


def _validator():
    """
    拿到判据。**`build_release.py` 不进交付包**（`EXCLUDED_SCRIPT_NAMES`），
    所以装出来的副本 import 不到它——模块级 import 会让业务的自测按钮
    直接报错。跟 test_no_customer_names_in_repo.py 的 `_validator()`
    是同一个坑、同一个修法。
    """
    try:
        import build_release
    except ImportError:
        return None
    return build_release.unapproved_organizations


def _hit(text: str) -> list[str]:
    check = _validator()
    if check is None:
        raise unittest.SkipTest(
            "交付副本里没有 build_release.py，本来就不做这道检查")
    return check(text.encode("utf-8"))


class PureEnglishAbbreviationTest(unittest.TestCase):
    """本次要补上的目标场景：纯英文缩写紧跟中文后缀。"""

    def test_uppercase_prefix_is_caught(self):
        self.assertTrue(_hit("AB" + "C" + "有限公司" + "完成了安装"))

    def test_digit_letter_mix_is_caught(self):
        self.assertTrue(_hit("3" + "M" + "集团" + "完成了安装"))

    def test_six_suffixes_are_caught_with_english_prefix(self):
        # 🔴 七个后缀词里唯一故意不测的那个：它开头两个字正好是「股份」，
        # 而「股份」+ 后半段本身也是一个独立后缀词——这两段拼起来，
        # 单独这七个字就已经够中文分支 {2,60} 的最小长度，不需要任何
        # 英文前缀就能靠纯中文分支的巧合命中。测它测不出这次到底改了
        # 什么，红绿对照会显示假绿，所以单独排除，其余六个不受此影响。
        prefix = "AB" + "C"
        for suffix in ("有限责任公司", "有限公司", "集团", "医院", "酒店", "合作社"):
            with self.subTest(suffix=suffix):
                self.assertTrue(_hit(prefix + suffix), f"漏检：{prefix}{suffix}")


class MixedChineseEnglishUnaffectedTest(unittest.TestCase):
    """中英混合场景（前缀本身含中文字符）不受这次改动影响，行为不变。"""

    def test_chinese_part_still_matched_when_prefixed_by_english(self):
        hits = _hit("3" + "M" + "中国" + "有限公司" + "完成了安装")
        self.assertIn("中国" + "有限公司", hits)

    def test_pure_chinese_organization_unaffected(self):
        hits = _hit("浙江" + "某某" + "有限公司" + "完成了安装")
        self.assertIn("浙江" + "某某" + "有限公司", hits)


class LowercaseNotTreatedAsAbbreviationTest(unittest.TestCase):
    """
    🔴 这是刻意的取舍，不是残留漏洞：只认大写是为了避免下面这类误报。
    """

    def test_lowercase_word_adjacent_to_suffix_is_not_flagged(self):
        """真实案例：这次调试时自己写的测试字符串意外触发过一次误报。"""
        self.assertEqual(
            _hit("some code variable_name and" + "集团" + " nothing"), [])

    def test_mixed_case_word_is_not_flagged(self):
        self.assertEqual(_hit("Ab" + "c" + "有限公司"), [])

    def test_plain_chinese_sentence_without_org_name_is_clean(self):
        self.assertEqual(_hit("这只是普通的中文句子，没有任何组织名"), [])

    def test_code_comment_style_text_is_clean(self):
        self.assertEqual(_hit("core.py 的 assert_sheet 函数负责校验"), [])


if __name__ == "__main__":
    unittest.main()
