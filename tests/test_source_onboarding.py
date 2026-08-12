#!/usr/bin/env python3
"""三种数据源的接入说明、入口和错误分流不能再各写各的。"""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SourceOnboardingGuideTest(unittest.TestCase):

    def setUp(self):
        self.tencent = (ROOT / "docs" / "腾讯文档接入.md").read_text(encoding="utf-8")
        self.wecom = (ROOT / "docs" / "企业微信文档接入.md").read_text(encoding="utf-8")

    def test_tencent_guide_separates_token_account_and_link_fields(self):
        for term in ("OpenClaw 专用入口", "不是浏览器登录态", "能打开目标台账", "file_id", "sheet_id"):
            with self.subTest(term=term):
                self.assertIn(term, self.tencent)

    def test_tencent_guide_has_one_path_per_common_failure(self):
        for term in ("HTTP 401 / 403", "缺 `TENCENT_DOCS_TOKEN`", "找不到 `sheet_id`", "不要把“重新扫码”"):
            with self.subTest(term=term):
                self.assertIn(term, self.tencent)

    def test_wecom_guide_separates_smart_bot_from_webhook(self):
        for term in ("智能机器人", "群机器人 Webhook", "Node.js 18+", "npm install -g @wecom/cli", "wecom-cli init"):
            with self.subTest(term=term):
                self.assertIn(term, self.wecom)

    def test_wecom_guide_covers_permission_models_and_readonly_probe(self):
        for term in ("10 人以上企业", "851008", "851003", "851002", "sheet_get_info", "get_doc_content"):
            with self.subTest(term=term):
                self.assertIn(term, self.wecom)

    def test_main_entry_points_link_every_guide(self):
        required = ("腾讯文档接入.md", "企业微信文档接入.md", "飞书多维表格接入.md")
        for rel in ("README.md", "SKILL.md", "docs/接一条新业务线.md"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            for guide in required:
                with self.subTest(file=rel, guide=guide):
                    self.assertIn(guide, text)

    def test_business_entry_points_make_every_ledger_readonly(self):
        for rel in ("README.md", "docs/WorkBuddy代理指令.md",
                    "docs/WorkBuddy安装测试.md", "docs/业务操作手册-零基础版.md"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            with self.subTest(file=rel):
                self.assertIn("所有台账源只读", text)
                self.assertNotIn("腾讯文档台账永远只读", text)
                self.assertNotIn("腾讯文档只读；", text)

    def test_workbuddy_branches_credentials_by_source(self):
        text = (ROOT / "docs" / "WorkBuddy代理指令.md").read_text(encoding="utf-8")
        for term in ("source=tencent_mcp", "source=wecom_doc", "source=lark_cli",
                     "企微或飞书来源不得索取它", "不把其凭证复制到 `.env`"):
            with self.subTest(term=term):
                self.assertIn(term, text)
        self.assertNotIn("腾讯文档本地授权凭证", text)

    def test_readme_lists_wecom_network_and_credential_storage(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("doc.weixin.qq.com", text)
        self.assertIn("纯飞书或纯企业微信文档用户不需要腾讯文档凭证", text)
        self.assertIn("wecom-cli`、`lark-cli` 的身份保留在各自凭证存储中", text)

    def test_template_lists_three_sources_and_wecom_pair(self):
        text = (ROOT / "templates" / "ledgers.example.json").read_text(encoding="utf-8")
        for term in ("tencent_mcp", "lark_cli", "wecom_doc", "url", "sheet_id", "企业微信文档接入.md"):
            with self.subTest(term=term):
                self.assertIn(term, text)


if __name__ == "__main__":
    unittest.main()
