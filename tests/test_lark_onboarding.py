#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞书从零接入说明不能再把 profile、登录和协作者混成一句话。"""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LarkOnboardingGuideTest(unittest.TestCase):

    def setUp(self):
        self.guide = (ROOT / "docs" / "飞书多维表格接入.md").read_text(
            encoding="utf-8")

    def test_guide_covers_all_four_gates(self):
        for term in ("应用配置", "API 权限", "用户登录", "文档权限"):
            with self.subTest(term=term):
                self.assertIn(term, self.guide)

    def test_every_auth_example_names_the_profile(self):
        auth_lines = [line.strip() for line in self.guide.splitlines()
                      if line.strip().startswith("lark-cli auth ")]
        self.assertTrue(auth_lines)
        for line in auth_lines:
            with self.subTest(line=line):
                self.assertIn("--profile sentinel", line)

    def test_scheduled_login_requests_readonly_and_refresh_scopes(self):
        self.assertIn('bitable:app:readonly offline_access', self.guide)
        self.assertIn("不会把\n多维表格权限升级为可写", self.guide)

    def test_share_view_is_explicitly_rejected_as_base_token(self):
        self.assertIn("/share/base/view/shr", self.guide)
        self.assertIn("不是 `base_token`", self.guide)

    def test_user_not_bot_is_the_collaborator(self):
        self.assertIn("固定使用 `--as user`", self.guide)
        self.assertIn("不是机器人或应用", self.guide)

    def test_readonly_probe_is_documented(self):
        self.assertIn("base +table-list", self.guide)
        self.assertIn("--as user", self.guide)

    def test_entry_documents_link_to_the_guide(self):
        for rel in ("README.md", "SKILL.md", "templates/ledgers.example.json"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            with self.subTest(file=rel):
                self.assertIn("飞书多维表格接入.md", text)


if __name__ == "__main__":
    unittest.main()
