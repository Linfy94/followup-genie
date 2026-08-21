#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_release.unapproved_organizations()：各种大小写的英文缩写紧跟中文后缀都要认得出。

═══════════════════════════════════════════════════════════════════════
🔴 2026-08-21 复审两轮：

第一轮发现：原来的正则只认「2-60 个连续中文字符 + 公司后缀」，纯英文
缩写紧跟中文公司后缀完全漏检。补了英文前缀分支，但只认全大写——
目的是排除代码注释里巧合的英文单词紧跟中文后缀词这类误报。

第二轮指出：大小写混合、全小写的真实品牌缩写写法（驼峰式品牌名、
纯小写缩写），同样被"只认大写"漏掉——业务的态度很明确：**宁可构建
失败后人工确认，也不能放过真实客户名**，这条优先级排在"避免误报"
之上。这次改成不限制大小写。

代价：英文常用单词紧跟中文后缀词这类误报会重新出现——常用单词与
真实品牌缩写在字符特征上完全一样，正则分不出哪个更像公司名，这是
纯文本匹配的理论极限。真触发了，处理成本很低（加白名单/改措辞），
比漏掉真实客户名的代价小得多。下面 `KnownFalsePositiveTradeoffTest`
就是在如实记录这个接受了的代价，不是在验证"不会误报"。

🔴 本文件所有样例组织名都在**运行时拼接**，连注释/文档字符串里也
   不能写出完整的组合——这个文件会被打包脚本自己的组织名扫描器扫到
   （正是它在守的东西），扫描器读的是源码字节，不区分代码与文字说明。
   跟 rc10 那次 test_no_customer_names_in_repo.py 踩的坑是同一个道理。
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


class EnglishAbbreviationAnyCaseTest(unittest.TestCase):
    """本次要补上的目标场景：任意大小写的英文缩写紧跟中文后缀。"""

    def test_uppercase_prefix_is_caught(self):
        self.assertTrue(_hit("AB" + "C" + "有限公司" + "完成了安装"))

    def test_lowercase_prefix_is_caught(self):
        """🔴 这是第二轮复审补上的：全小写缩写此前会漏检。"""
        self.assertTrue(_hit("ab" + "c" + "有限公司" + "完成了安装"))

    def test_mixed_case_prefix_is_caught(self):
        """🔴 这是第二轮复审补上的：驼峰式品牌名（iRobot 这类）此前会漏检。"""
        self.assertTrue(_hit("i" + "Robot" + "有限公司" + "完成了安装"))

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


class KnownFalsePositiveTradeoffTest(unittest.TestCase):
    """
    🔴 这次改动**接受**这几类误报，不是漏改了：为了不漏掉真实客户名
    （abc/iRobot 这类），代价是这几个巧合场景会被命中，业务的优先级
    是"宁可误报"，处理路径是加白名单/改措辞，不是继续收紧正则。
    """

    def test_lowercase_word_adjacent_to_suffix_is_now_flagged(self):
        """
        这行字符串本身之前是"不该被命中"的对照组，这次改动之后就该被
        命中——用它来确认这次的取舍是刻意的、可预期的，不是意外回归。
        """
        self.assertTrue(
            _hit("some code variable_name and" + "集团" + " nothing"))


class StillCleanCasesTest(unittest.TestCase):
    """这几类跟这次改动无关，验证仍然干净——不是"缩写紧跟后缀"这个形状。"""

    def test_plain_chinese_sentence_without_org_name_is_clean(self):
        self.assertEqual(_hit("这只是普通的中文句子，没有任何组织名"), [])

    def test_code_comment_style_text_is_clean(self):
        self.assertEqual(_hit("core.py 的 assert_sheet 函数负责校验"), [])


if __name__ == "__main__":
    unittest.main()
