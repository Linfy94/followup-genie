#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
启动阶段故障必须走统一出口。

这一组守的是同一条铁律：**故障绝不能长得像「今天没有超时单」**。
凭证过期、字段改名、配置写坏、目录不可写 —— 全都会表现成「今天很安静」，
那是本方案从头到尾最提防的失败模式。

每一条都必须：非零退出（让 Hermes 显示失败）+ 告警 + 记进 health。
"""

from __future__ import annotations

import os
import stat
import unittest
from datetime import date

from harness import (make_sheet, row, temp_home, run_main, read_state,
                     ledgers_cfg, rules_cfg, state_files)

TODAY = date(2026, 7, 20)


def sheet():
    return make_sheet([row(1, "甲公司", tech="待收资",
                           reported=date(2026, 6, 1), progress=date(2026, 6, 1))])


class StartupFaultTest(unittest.TestCase):

    def test_broken_config_json(self):
        with temp_home() as home:
            (home / "followup" / "config" / "ledgers.json").write_text(
                "{ 坏掉的 JSON ", encoding="utf-8")
            r = run_main([f"--today={TODAY}", "--force-push"], sheet())
            self.assertEqual(r.code, 2)
            self.assertIn("配置读取失败", r.err)
            self.assertTrue(r.alerted, "配置坏了也要有人知道")

    def test_broken_output_json_is_not_silently_ignored(self):
        """
        output.json 缺失可以按默认走，但**存在却是坏的**必须报错 ——
        静默当成空配置会让企微通道、主通道声明、告警开关一起消失。
        """
        with temp_home() as home:
            (home / "followup" / "config" / "output.json").write_text(
                "]not json[", encoding="utf-8")
            r = run_main([f"--today={TODAY}", "--force-push"], sheet())
            self.assertEqual(r.code, 2)

    def test_no_enabled_ledger(self):
        cfg = ledgers_cfg(enabled=False)
        with temp_home(ledgers=cfg):
            r = run_main([f"--today={TODAY}", "--force-push"], sheet())
            self.assertEqual(r.code, 2)
            self.assertIn("没有启用的台账", r.err)
            self.assertTrue(r.alerted)

    def test_missing_ruleset_reference(self):
        cfg = ledgers_cfg(ruleset="不存在的规则集")
        with temp_home(ledgers=cfg):
            r = run_main([f"--today={TODAY}", "--force-push"], sheet())
            self.assertEqual(r.code, 1)
            self.assertIn("找不到规则集", r.err)
            self.assertTrue(r.alerted)

    def test_unwritable_state_dir_fails_before_sending(self):
        """
        🔴 目录不可写必须在**推送之前**失败。
           否则消息已经发出去了、状态却没落地，下次会整批重推。
        """
        with temp_home() as home:
            d = home / "followup" / "state"
            mode = d.stat().st_mode
            os.chmod(d, stat.S_IRUSR | stat.S_IXUSR)   # 只读
            try:
                r = run_main([f"--today={TODAY}", "--force-push"], sheet())
                self.assertEqual(r.code, 2)
                self.assertIn("状态目录不可写", r.err)
                self.assertEqual(r.posts, [],
                                 "🔴 状态写不了就绝不能先把消息发出去")
            finally:
                os.chmod(d, mode)

    def test_fetch_failure_is_not_an_empty_list(self):
        with temp_home():
            r = run_main([f"--today={TODAY}", "--force-push"], sheet(),
                         read_sheet_error="腾讯文档接口连续 3 次请求失败")
            self.assertEqual(r.code, 1)
            self.assertIn("这不是「今天没有超时单」", r.err)
            self.assertEqual(r.posts, [])
            self.assertTrue(r.alerted)

    def test_credential_failure_is_distinguishable(self):
        """凭证过期是最隐蔽的失败模式：它会表现成「今天没有超时单」。"""
        with temp_home() as home:
            r = run_main([f"--today={TODAY}", "--force-push"], sheet(),
                         read_sheet_error="凭证失效或无权限（HTTP 401）")
            self.assertEqual(r.code, 1)
            h = read_state(home, "health.json")
            self.assertIn("401", h["last_failure"]["reason"])

    def test_missing_required_column_is_fatal(self):
        """业务改了列名 / 读取起点错位，都会在这里被拦住。"""
        bad = make_sheet(
            [{"序号": 1, "项目名称": "甲公司"}],
            columns=["序号", "项目名称"],   # 缺一大堆必需列
        )
        with temp_home():
            r = run_main([f"--today={TODAY}", "--force-push"], bad)
            self.assertEqual(r.code, 1)
            self.assertIn("表头缺少必需列", r.err)
            self.assertTrue(r.alerted)

    def test_duplicate_key_is_fatal(self):
        """重复主键会让两个项目共用一条催办状态。"""
        dup = make_sheet([
            row(1, "甲公司", tech="待收资", reported=TODAY, progress=TODAY),
            row(1, "乙公司", tech="待收资", reported=TODAY, progress=TODAY),
        ])
        with temp_home():
            r = run_main([f"--today={TODAY}", "--force-push"], dup)
            self.assertEqual(r.code, 1)
            self.assertIn("重复值", r.err)

    def test_missing_repeat_interval_is_fatal(self):
        """
        缺复提醒间隔不许默认成「只催一次」——
        新设计的退出机制只有「推进」和「终止」，不该有自动静默这第三种。

        退出码 2 而不是 1：0.4.0-rc8 起 repeat 的检查同时进了离线校验
        （validate_configs），于是在**碰台账之前**就拦下了。按 README 的
        退出码表，「配置错」本就归 2、「入口断言不过」才归 1，这一步是往
        契约上靠。两者对宿主都是「报失败」，且告警照发（下面钉住）。
        """
        rules = rules_cfg(collect={"repeat": {}})
        with temp_home(rules=rules):
            r = run_main([f"--today={TODAY}", "--force-push"], sheet())
            self.assertEqual(r.code, 2)
            self.assertIn("复提醒间隔", r.err)
            self.assertTrue(r.alerts, "配置错也必须告警，不能只写日志")

    def test_config_referencing_nonexistent_column_is_fatal(self):
        """
        终止判据引用的列被业务删掉了 —— 静默跳过会让终止项重新开始被催。
        """
        cfg = ledgers_cfg(terminal_states=[
            {"field": "这一列不存在", "op": "in", "values": ["x"]}])
        with temp_home(ledgers=cfg):
            r = run_main([f"--today={TODAY}", "--force-push"], sheet())
            self.assertEqual(r.code, 1)
            self.assertIn("不存在的列", r.err)

    def test_bad_today_argument(self):
        with temp_home():
            r = run_main(["--today=不是日期"], sheet())
            self.assertEqual(r.code, 2)


class DataQualityVisibleTest(unittest.TestCase):
    """数据质量问题不阻断运行，但必须始终显示 —— 不许藏进 --verbose。"""

    def test_unknown_scope_value_is_shown_by_default(self):
        """「杭州市」这种写法会被范围过滤静默丢掉，是最难发现的坑。"""
        s = make_sheet([
            row(1, "甲公司", place="杭州市", tech="待收资",
                reported=date(2026, 6, 1), progress=date(2026, 6, 1)),
            row(2, "乙公司", tech="待收资",
                reported=date(2026, 6, 1), progress=date(2026, 6, 1)),
        ])
        with temp_home():
            r = run_main([f"--today={TODAY}", "--dry-run"], s)   # 没有 --verbose
            self.assertIn("未知取值", r.out)
            self.assertIn("杭州市", r.out)

    def test_disabled_node_is_always_announced(self):
        """一个悄悄不跑的规则，比一个跑错的规则更难发现。"""
        rules = rules_cfg(efficiency_test={"enabled": False})
        with temp_home(rules=rules):
            r = run_main([f"--today={TODAY}", "--dry-run"], sheet())
            self.assertIn("未启用", r.out)


if __name__ == "__main__":
    unittest.main()
