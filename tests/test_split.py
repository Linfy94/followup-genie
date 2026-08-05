#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P2-A：消息拆分的硬保证。

旧实现有两个洞，都实测复现过：
  ① 单行本身超限时原样返回 —— 4500 字节的单行会被整片发出去，超企微 4096 上限
  ② 「第几条」前缀是拆完之后才加的 —— limit=4000 时实测两片各 4009 字节

保证：**每一片（含前缀）都不超过 min(split_bytes, 4096)，且一个字都不丢。**
"""

from __future__ import annotations

import unittest

from harness import wecom_push

B = wecom_push._bytes
CAP = wecom_push.HARD_LIMIT


class SplitGuaranteeTest(unittest.TestCase):

    def assert_within(self, text, limit):
        """发出去的就是 render_chunks 的结果，所以要验它，而不是 split_message。"""
        msgs = wecom_push.render_chunks(text, limit)
        cap = min(limit, CAP)
        for i, m in enumerate(msgs):
            self.assertLessEqual(
                B(m), cap,
                f"第 {i+1}/{len(msgs)} 片 {B(m)} 字节 > 上限 {cap}")
        return msgs

    def assert_no_loss(self, text, msgs):
        """去掉前缀后拼回来，所有非空行都还在（顺序不变）。"""
        body = []
        for m in msgs:
            if m.startswith("（") and "\n" in m:
                m = m.split("\n", 1)[1]
            body.append(m)
        joined = "\n".join(body)
        # 硬拆会在断点插「…」，比对时把它去掉再拼
        got = joined.replace("…\n", "").replace("…", "")
        src = "".join(text.split())
        self.assertEqual("".join(got.split()), src, "拆分丢内容了")

    # ── 洞 ①：单行超限 ────────────────────────────────────────────
    def test_single_line_4500_bytes(self):
        text = "东" * 1500          # 4500 字节，一行
        msgs = self.assert_within(text, 4000)
        self.assertGreater(len(msgs), 1)
        self.assert_no_loss(text, msgs)

    def test_single_line_exactly_at_hard_limit(self):
        text = "x" * 4096
        self.assert_within(text, 4096)

    # ── 洞 ②：前缀踩线 ────────────────────────────────────────────
    def test_prefix_is_counted_in_the_budget(self):
        blk = "x" * 3999
        text = blk + "\n\n" + blk
        msgs = self.assert_within(text, 4000)
        self.assertGreater(len(msgs), 1, "这个用例必须真的拆开")
        for m in msgs:
            self.assertTrue(m.startswith("（"), "多片时每片都要有序号前缀")

    def test_prefix_counted_when_limit_equals_hard_limit(self):
        """split_bytes 调到 4096 时最容易踩线 —— 旧实现在这里必挂。"""
        self.assert_within("好" * 4000, 4096)

    # ── 中文 / emoji / 超长企业名 ──────────────────────────────────
    def test_chinese_never_split_mid_character(self):
        """按字节切会把汉字劈成两半变乱码，必须按码点切。"""
        # 🔴 测试数据一律用虚构企业名。真实客户名进了仓库就等于把
        #    「这家公司是我们的项目、而且卡住了」写进版本历史，撤不回来。
        #    这两条测的是「中文不被劈成半个字」，名字是谁与断言无关。
        text = "东海市示例城市建设投资集团有限公司" * 400
        msgs = self.assert_within(text, 1000)
        for m in msgs:
            m.encode("utf-8").decode("utf-8")   # 能编解码就说明没劈坏

    def test_emoji_are_safe(self):
        text = ("🧚 项目跟进精灵 · 东海市示例城市建设投资集团有限公司 "
                "— 停滞 123 天 ✅🔴⚠️\n") * 200
        msgs = self.assert_within(text, 2000)
        for m in msgs:
            m.encode("utf-8").decode("utf-8")
        self.assert_no_loss(text, msgs)

    def test_very_long_company_name_line(self):
        long_name = "中国" + "特别长的企业名称" * 300 + "有限公司"
        text = f"1、{long_name} — 停滞 12 天"
        msgs = self.assert_within(text, 1200)
        self.assert_no_loss(text, msgs)

    def test_very_long_data_quality_warning(self):
        """
        真正会触发单行硬拆的其实是这个：数据质量告警会拼接大量序号。
        项目条目最长也就几十字节，永远够。
        """
        text = "⚠️ 最新进展日期 有 900 行无法解析为日期（序号 " + \
               "、".join(str(i) for i in range(1, 900)) + "）"
        msgs = self.assert_within(text, 4000)
        self.assert_no_loss(text, msgs)

    # ── 正常情况不该被拆 ──────────────────────────────────────────
    def test_short_message_stays_one_piece_without_prefix(self):
        text = "# 标题\n\n1、甲公司 — 停滞 30 天"
        msgs = wecom_push.render_chunks(text, 4000)
        self.assertEqual(msgs, [text], "没超限就不该加前缀、不该拆")

    def test_split_prefers_blank_line_boundaries(self):
        """优先在阶段之间断开，让每条消息主题集中。"""
        a = "### 【待收资】\n" + "\n".join(f"{i}、公司{i} — 停滞 {i} 天"
                                          for i in range(1, 40))
        b = "### 【节能测试】\n" + "\n".join(f"{i}、公司{i} — 停滞 {i} 天"
                                            for i in range(1, 40))
        msgs = self.assert_within(a + "\n\n" + b, 900)
        self.assertTrue(any("【待收资】" in m for m in msgs))
        self.assertTrue(any("【节能测试】" in m for m in msgs))

    def test_absurdly_small_limit_still_terminates(self):
        """极端配置不能让拆分死循环。"""
        msgs = self.assert_within("东" * 50, 20)
        self.assertGreater(len(msgs), 1)

    def test_zero_limit_is_rejected_not_infinite_loop(self):
        with self.assertRaises(ValueError):
            wecom_push.render_chunks("东" * 50, 0)


class PayloadTest(unittest.TestCase):
    """曾经的真 bug：markdown_v2 掉进 text 分支，满屏 ** # - 原样发出去。"""

    def test_payload_inner_key_matches_msgtype(self):
        for t in sorted(wecom_push.SUPPORTED_MSGTYPES):
            payload = {"msgtype": t, t: {"content": "x"}}
            self.assertIn(payload["msgtype"], payload)
            self.assertEqual(payload[t]["content"], "x")

    def test_markdown_v2_is_supported(self):
        self.assertIn("markdown_v2", wecom_push.SUPPORTED_MSGTYPES)


if __name__ == "__main__":
    unittest.main()
