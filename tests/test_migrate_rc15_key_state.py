#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
migrate_rc15_key_state.py：rc14 旧 key → rc15 新 key 的一次性状态迁移。

═══════════════════════════════════════════════════════════════════════
🔴 2026-08-21 P0 复现场景①：一个原本 singleton 的项目，消歧字段本来就
有值（rc14 无条件拼进 key），升级到 rc15（只有真撞车才拼）后，同一行
算出的基础 key 不再带后缀——零数据变化，历史记录却在新 key 下查不到，
表现为升级本身触发一次多余的首次催办。

这份脚本要把状态文件里"仍按 rc14 规则命名"的记录，原地改名到 rc15 会
算出的新 key。下面几条测试直接对 `plan_ledger()` / `apply_plan()` /
`main()` 断言，不跑真实网络。

🔴 2026-08-21 第三轮复审又补了两类：`snapshot_last_<台账>.json` 的
`nodes` 字典漏迁移（`SnapshotMigrationTest`），以及迁移必须跟每日任务
共用运行锁、任何一步失败都不能显示成功（`LockingTest` / `FailureReportingTest`）。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime
from unittest import mock

from harness import core, ledgers_cfg, temp_home, read_state

import migrate_rc15_key_state as migrate

from test_sentinel_rules import FakeSheet


def _sheet(rows):
    return FakeSheet(["企业", "机构", "访客时间", "目标国家地区"], rows)


def _ledger(**over):
    over.setdefault("id", "trade_qq")
    over.setdefault("key_field", ["企业", "机构", "访客时间"])
    over.setdefault("name_field", "企业")
    over.setdefault("key_tiebreakers", ["目标国家地区"])
    return ledgers_cfg(**over)["ledgers"][0]


class PlanLedgerTest(unittest.TestCase):

    def test_singleton_with_stale_suffixed_history_is_planned_for_rename(self):
        """核心场景：消歧字段有值但从没撞过车，rc14 会拼后缀，rc15 不会。"""
        s = _sheet([{"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
                    "目标国家地区": "欧洲"}])
        with mock.patch.object(core, "read_ledger_sheet", return_value=s):
            plans = migrate.plan_ledger(_ledger())
        self.assertEqual(plans, [("甲公司|杭州分行|46202‖欧洲", "甲公司|杭州分行|46202")])

    def test_no_tiebreaker_field_ever_filled_means_nothing_to_migrate(self):
        s = _sheet([{"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
                    "目标国家地区": ""}])
        with mock.patch.object(core, "read_ledger_sheet", return_value=s):
            plans = migrate.plan_ledger(_ledger())
        self.assertEqual(plans, [])

    def test_ledger_without_key_tiebreakers_configured_is_skipped_without_reading_sheet(self):
        led = _ledger(key_tiebreakers=None)
        with mock.patch.object(core, "read_ledger_sheet") as m:
            plans = migrate.plan_ledger(led)
        self.assertEqual(plans, [])
        m.assert_not_called()

    def test_genuine_current_collision_is_left_to_the_runtime_guard(self):
        """
        本轮就撞车的行，resolve_row_keys 在没有 existing_state_keys 时会
        正常消歧（不是 None）——rc14/rc15 两边算出来的后缀 key 一致，
        没有漂移，plan_ledger 不需要管它。
        """
        s = _sheet([
            {"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
             "目标国家地区": "欧洲"},
            {"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
             "目标国家地区": "日本"},
        ])
        with mock.patch.object(core, "read_ledger_sheet", return_value=s):
            plans = migrate.plan_ledger(_ledger())
        self.assertEqual(plans, [])


class ApplyPlanTest(unittest.TestCase):

    def test_renames_matching_entries_across_all_three_state_dicts(self):
        old_key = "甲公司|杭州分行|46202‖欧洲"
        new_key = "甲公司|杭州分行|46202"
        state = {
            "stage_entered.json": {f"trade_qq|{old_key}|fillin": "2026-08-01"},
            "followup_state.json": {f"trade_qq|{old_key}|fillin": {"first_overdue": "2026-08-05"}},
            "stage_history.json": {},
        }
        log = migrate.apply_plan("trade_qq", [(old_key, new_key)], state)
        self.assertEqual(
            state["stage_entered.json"], {f"trade_qq|{new_key}|fillin": "2026-08-01"})
        self.assertEqual(
            state["followup_state.json"],
            {f"trade_qq|{new_key}|fillin": {"first_overdue": "2026-08-05"}})
        self.assertTrue(any("stage_entered.json" in line for line in log))

    def test_does_not_clobber_an_existing_new_key_entry(self):
        """目标 key 已经有记录了（比如撞车安全网已经处理过一次）——不覆盖，两条都留着。"""
        old_key, new_key = "甲|杭州分行|1‖欧洲", "甲|杭州分行|1"
        state = {
            "stage_entered.json": {
                f"trade_qq|{old_key}|fillin": "2026-08-01",
                f"trade_qq|{new_key}|fillin": "2026-08-10",
            },
        }
        log = migrate.apply_plan("trade_qq", [(old_key, new_key)], state)
        self.assertEqual(state["stage_entered.json"], {
            f"trade_qq|{old_key}|fillin": "2026-08-01",
            f"trade_qq|{new_key}|fillin": "2026-08-10",
        }, "冲突时两条历史记录都不能丢")
        self.assertTrue(any("已存在" in line for line in log))

    def test_unrelated_ledger_ids_are_untouched(self):
        old_key, new_key = "甲|杭州分行|1‖欧洲", "甲|杭州分行|1"
        state = {
            "stage_entered.json": {
                f"trade_qq|{old_key}|fillin": "2026-08-01",
                f"另一条台账|{old_key}|fillin": "2026-08-02",
            },
        }
        migrate.apply_plan("trade_qq", [(old_key, new_key)], state)
        self.assertIn(f"另一条台账|{old_key}|fillin", state["stage_entered.json"])


class MainDryRunTest(unittest.TestCase):
    """main() 的默认行为（不加 --apply）绝不写盘——迁移这么敏感，默认必须只读。"""

    def test_dry_run_never_touches_state_files(self):
        s = _sheet([{"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
                    "目标国家地区": "欧洲"}])
        old_key = "甲公司|杭州分行|46202‖欧洲"
        state_seed = {"stage_entered.json": {f"trade_qq|{old_key}|fillin": "2026-08-01"}}
        with temp_home(ledgers={"ledgers": [_ledger()]}, state=state_seed) as home:
            with mock.patch.object(core, "read_ledger_sheet", return_value=s), \
                 mock.patch.object(sys, "argv", ["migrate_rc15_key_state.py"]):
                code = migrate.main()
            self.assertEqual(code, 0)
            self.assertEqual(
                read_state(home, "stage_entered.json"),
                {f"trade_qq|{old_key}|fillin": "2026-08-01"},
                "dry-run 不该改写任何文件")
            self.assertEqual(
                list((home / "followup" / "state").glob(".migrate-rc15-backup-*")), [],
                "dry-run 不该留下备份目录——没写就不需要备份")

    def test_apply_migrates_and_leaves_a_backup(self):
        s = _sheet([{"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
                    "目标国家地区": "欧洲"}])
        old_key = "甲公司|杭州分行|46202‖欧洲"
        new_key = "甲公司|杭州分行|46202"
        state_seed = {"stage_entered.json": {f"trade_qq|{old_key}|fillin": "2026-08-01"}}
        with temp_home(ledgers={"ledgers": [_ledger()]}, state=state_seed) as home:
            with mock.patch.object(core, "read_ledger_sheet", return_value=s), \
                 mock.patch.object(sys, "argv", ["migrate_rc15_key_state.py", "--apply"]):
                code = migrate.main()
            self.assertEqual(code, 0)
            self.assertEqual(
                read_state(home, "stage_entered.json"),
                {f"trade_qq|{new_key}|fillin": "2026-08-01"})
            backups = list((home / "followup" / "state").glob(".migrate-rc15-backup-*"))
            self.assertEqual(len(backups), 1, "必须留一份改写前的备份")
            backup_content = json.loads(
                (backups[0] / "stage_entered.json").read_text(encoding="utf-8"))
            self.assertEqual(backup_content, {f"trade_qq|{old_key}|fillin": "2026-08-01"},
                             "备份必须是改写前的内容，不是改写后的")


class SnapshotMigrationTest(unittest.TestCase):
    """
    snapshot_last_<台账>.json 的 `nodes` 字典也按 key 存，跟另外三份状态
    文件一样会漂移——漏了不改，节点变更检测会失灵，见模块 docstring。
    """

    def test_apply_snapshot_plan_renames_matching_entry(self):
        old_key, new_key = "甲公司|杭州分行|46202‖欧洲", "甲公司|杭州分行|46202"
        nodes = {old_key: "fill_contact"}
        log = migrate.apply_snapshot_plan([(old_key, new_key)], nodes)
        self.assertEqual(nodes, {new_key: "fill_contact"})
        self.assertTrue(any("snapshot_last" in line for line in log))

    def test_apply_snapshot_plan_does_not_clobber_existing_target(self):
        old_key, new_key = "甲|杭州分行|1‖欧洲", "甲|杭州分行|1"
        nodes = {old_key: "fill_contact", new_key: "confirm_result"}
        log = migrate.apply_snapshot_plan([(old_key, new_key)], nodes)
        self.assertEqual(nodes, {old_key: "fill_contact", new_key: "confirm_result"},
                         "冲突时两条都不能丢")
        self.assertTrue(any("已存在" in line for line in log))

    def test_untouched_keys_are_left_alone(self):
        nodes = {"另一个项目|杭州分行|1": "fill_contact"}
        migrate.apply_snapshot_plan(
            [("甲|杭州分行|1‖欧洲", "甲|杭州分行|1")], nodes)
        self.assertEqual(nodes, {"另一个项目|杭州分行|1": "fill_contact"})

    def test_main_apply_migrates_the_snapshot_last_file_too(self):
        """
        端到端：main() --apply 除了三份共享状态文件，也要把这条台账自己的
        snapshot_last_<台账>.json 一起改名——这是本次复审要补的核心场景。
        """
        s = _sheet([{"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
                    "目标国家地区": "欧洲"}])
        old_key = "甲公司|杭州分行|46202‖欧洲"
        new_key = "甲公司|杭州分行|46202"
        state_seed = {
            "stage_entered.json": {f"trade_qq|{old_key}|fill_contact": "2026-08-01"},
            "snapshot_last_trade_qq.json": {
                "date": "2026-08-19",
                "nodes": {old_key: "fill_contact"},
            },
        }
        with temp_home(ledgers={"ledgers": [_ledger()]}, state=state_seed) as home:
            with mock.patch.object(core, "read_ledger_sheet", return_value=s), \
                 mock.patch.object(sys, "argv", ["migrate_rc15_key_state.py", "--apply"]):
                code = migrate.main()
            self.assertEqual(code, 0)
            snap = read_state(home, "snapshot_last_trade_qq.json")
            self.assertEqual(snap["nodes"], {new_key: "fill_contact"},
                             "snapshot_last 的 nodes 也必须跟着改名")
            self.assertEqual(snap["date"], "2026-08-19", "日期字段不该被动")
            backups = list((home / "followup" / "state").glob(".migrate-rc15-backup-*"))
            backed_up = json.loads(
                (backups[0] / "snapshot_last_trade_qq.json").read_text(encoding="utf-8"))
            self.assertEqual(backed_up["nodes"], {old_key: "fill_contact"},
                             "snapshot_last 也要在改写前备份")


class LockingTest(unittest.TestCase):
    """
    迁移必须跟每日任务共用同一把运行锁——不然赶上 9 点任务在跑时执行迁移，
    会覆盖对方刚写完的状态。锁只在真正写盘（--apply）时才取。
    """

    def _hold_lock(self, home):
        payload = json.dumps({
            "pid": os.getpid(),  # 用测试自己的 pid——保证判定成"活着"
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "token": "someone-elses-token",
        })
        lock_path = home / "followup" / "state" / ".run.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(payload, encoding="utf-8")
        return lock_path

    def test_dry_run_does_not_need_the_lock(self):
        """只看计划不写盘，不该跟正在跑的每日任务抢锁。"""
        s = _sheet([{"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
                    "目标国家地区": "欧洲"}])
        with temp_home(ledgers={"ledgers": [_ledger()]}) as home:
            self._hold_lock(home)
            with mock.patch.object(core, "read_ledger_sheet", return_value=s), \
                 mock.patch.object(sys, "argv", ["migrate_rc15_key_state.py"]):
                code = migrate.main()
            self.assertEqual(code, 0, "锁被占用不该影响纯只读的计划打印")

    def test_apply_fails_cleanly_when_lock_is_busy(self):
        """
        真赶上每日任务在跑：--apply 必须老老实实失败退出，不写任何东西，
        不能因为抢不到锁就假装什么都没发生。
        """
        s = _sheet([{"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
                    "目标国家地区": "欧洲"}])
        old_key = "甲公司|杭州分行|46202‖欧洲"
        state_seed = {"stage_entered.json": {f"trade_qq|{old_key}|fill_contact": "2026-08-01"}}
        with temp_home(ledgers={"ledgers": [_ledger()]}, state=state_seed) as home:
            self._hold_lock(home)
            with mock.patch.object(core, "read_ledger_sheet", return_value=s), \
                 mock.patch.object(sys, "argv", ["migrate_rc15_key_state.py", "--apply"]):
                code = migrate.main()
            self.assertNotEqual(code, 0, "抢不到锁必须非零退出，不能显示成功")
            self.assertEqual(
                read_state(home, "stage_entered.json"),
                {f"trade_qq|{old_key}|fill_contact": "2026-08-01"},
                "抢不到锁就不该动任何状态文件")
            self.assertEqual(
                list((home / "followup" / "state").glob(".migrate-rc15-backup-*")), [],
                "没写就不该留下备份")


class FailureReportingTest(unittest.TestCase):
    """任一步失败（台账读取失败、改名冲突）都必须让整个脚本非零退出。"""

    def test_apply_writes_nothing_if_any_ledger_fails_to_read(self):
        """
        两条台账，一条能正常算出迁移计划、一条取数失败——即使前者本来
        能成功，也不该只迁移一半、留下不一致的状态。整体必须失败、
        整体不写。
        """
        good_ledger = _ledger(id="trade_qq")
        bad_ledger = _ledger(id="other_qq")
        s = _sheet([{"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
                    "目标国家地区": "欧洲"}])
        old_key = "甲公司|杭州分行|46202‖欧洲"
        state_seed = {"stage_entered.json": {f"trade_qq|{old_key}|fill_contact": "2026-08-01"}}

        def fake_read(ledger):
            if ledger["id"] == "other_qq":
                raise RuntimeError("网络挂了")
            return s

        with temp_home(ledgers={"ledgers": [good_ledger, bad_ledger]},
                       state=state_seed) as home:
            with mock.patch.object(core, "read_ledger_sheet", side_effect=fake_read), \
                 mock.patch.object(sys, "argv", ["migrate_rc15_key_state.py", "--apply"]):
                code = migrate.main()
            self.assertNotEqual(code, 0)
            self.assertEqual(
                read_state(home, "stage_entered.json"),
                {f"trade_qq|{old_key}|fill_contact": "2026-08-01"},
                "有台账读取失败时，连本该能成功的那条台账也不该被单独写入")
            self.assertEqual(
                list((home / "followup" / "state").glob(".migrate-rc15-backup-*")), [],
                "整体失败就不该留下备份，避免看起来像是做过什么")

    def test_apply_writes_nothing_when_a_rename_conflicts(self):
        """
        冲突时不该有任何改动——连冲突本身涉及的那两条记录都不动。

        🔴 2026-08-21 第四轮复审指出：这条本来的行为是"冲突的那条跳过、
        其余照常写入"。单独看没问题，但组合上 bootstrap.py 的回滚语义就
        出事——bootstrap 看到迁移非零退出会整体回滚**代码**，不会回滚
        已经写下去的**状态**。"冲突→部分写入→非零退出→代码回滚"走完，
        state 停在新 key、代码退回旧版，旧代码按旧规则找不到这批刚改名
        的历史记录，当成新项目重新催办——跟这个脚本要防的 P0 一模一样，
        只是换了个触发路径。修法是把冲突纳入跟"台账读取失败"一样的
        全须全尾路径：只要有冲突，这一轮一个字节都不写。
        """
        s = _sheet([{"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
                    "目标国家地区": "欧洲"}])
        old_key, new_key = "甲公司|杭州分行|46202‖欧洲", "甲公司|杭州分行|46202"
        state_seed = {"stage_entered.json": {
            f"trade_qq|{old_key}|fill_contact": "2026-08-01",
            f"trade_qq|{new_key}|fill_contact": "2026-08-10",  # 目标 key 已经有记录，会冲突
        }}
        with temp_home(ledgers={"ledgers": [_ledger()]}, state=state_seed) as home:
            with mock.patch.object(core, "read_ledger_sheet", return_value=s), \
                 mock.patch.object(sys, "argv", ["migrate_rc15_key_state.py", "--apply"]):
                code = migrate.main()
            self.assertNotEqual(code, 0, "有冲突就不该显示成功")
            self.assertEqual(
                read_state(home, "stage_entered.json"),
                {f"trade_qq|{old_key}|fill_contact": "2026-08-01",
                 f"trade_qq|{new_key}|fill_contact": "2026-08-10"},
                "有冲突时这两条记录也一个字节都不该动")
            self.assertEqual(
                list((home / "followup" / "state").glob(".migrate-rc15-backup-*")), [],
                "没写就不该留下备份")

    def test_conflict_on_one_ledger_blocks_the_clean_rename_on_another(self):
        """
        端到端复现第四轮复审报的具体场景：两条台账，一条能干净改名、
        一条撞了冲突——干净的那条也不该被单独写入，必须整体失败。
        """
        s_a = _sheet([{"企业": "甲", "机构": "杭州分行", "访客时间": "46202",
                      "目标国家地区": "欧洲"}])
        s_b = _sheet([{"企业": "乙", "机构": "杭州分行", "访客时间": "1",
                      "目标国家地区": "欧洲"}])

        def fake_read(ledger):
            return s_a if ledger["id"] == "a" else s_b

        old_key_a = "甲|杭州分行|46202‖欧洲"
        old_key_b, new_key_b = "乙|杭州分行|1‖欧洲", "乙|杭州分行|1"
        state_seed = {"stage_entered.json": {
            f"a|{old_key_a}|fill_contact": "2026-08-01",
            f"b|{old_key_b}|fill_contact": "2026-08-01",
            f"b|{new_key_b}|fill_contact": "2026-08-10",  # b 会冲突
        }}
        with temp_home(ledgers={"ledgers": [_ledger(id="a"), _ledger(id="b")]},
                       state=state_seed) as home:
            with mock.patch.object(core, "read_ledger_sheet", side_effect=fake_read), \
                 mock.patch.object(sys, "argv", ["migrate_rc15_key_state.py", "--apply"]):
                code = migrate.main()
            self.assertNotEqual(code, 0)
            self.assertEqual(
                read_state(home, "stage_entered.json"),
                {f"a|{old_key_a}|fill_contact": "2026-08-01",
                 f"b|{old_key_b}|fill_contact": "2026-08-01",
                 f"b|{new_key_b}|fill_contact": "2026-08-10"},
                "🔴 台账 a 明明能干净改名，但 b 有冲突时 a 也不该被单独写入——"
                "这正是第四轮复审复现的组合问题")

    def test_write_failure_partway_restores_everything_from_backup(self):
        """
        写到一半失败（比如磁盘写满）：单份文件的写入本身是原子的，但一批
        文件加起来不是一次事务。已经写完的那几份必须用备份复原回去，
        不能留下"部分文件是新 key、部分还是旧 key"的半写状态。
        """
        s = _sheet([{"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
                    "目标国家地区": "欧洲"}])
        old_key = "甲公司|杭州分行|46202‖欧洲"
        state_seed = {"stage_entered.json": {f"trade_qq|{old_key}|fill_contact": "2026-08-01"},
                      "followup_state.json": {f"trade_qq|{old_key}|fill_contact":
                                              {"first_overdue": "2026-08-05"}}}
        with temp_home(ledgers={"ledgers": [_ledger()]}, state=state_seed) as home:
            calls = {"n": 0}
            real_write_state = core.write_state

            def flaky_write_state(name, data):
                # 第一次真正的迁移写入（stage_entered.json）放行，
                # 第二个文件（followup_state.json）模拟写坏；之后的调用
                # （restore 阶段）一律放行，好验证 restore 本身能不能成功。
                calls["n"] += 1
                if calls["n"] == 2:
                    raise OSError("模拟磁盘写满")
                return real_write_state(name, data)

            with mock.patch.object(core, "read_ledger_sheet", return_value=s), \
                 mock.patch.object(core, "write_state", side_effect=flaky_write_state), \
                 mock.patch.object(sys, "argv", ["migrate_rc15_key_state.py", "--apply"]):
                code = migrate.main()
            self.assertNotEqual(code, 0)
            self.assertEqual(
                read_state(home, "stage_entered.json"),
                {f"trade_qq|{old_key}|fill_contact": "2026-08-01"},
                "🔴 写到一半失败后，已经写成新 key 的那份必须被复原回旧 key")
            self.assertEqual(
                read_state(home, "followup_state.json"),
                {f"trade_qq|{old_key}|fill_contact": {"first_overdue": "2026-08-05"}})


class MigrationMarkerTest(unittest.TestCase):
    """
    迁移一旦干净跑完就该留个标记，往后每次升级都不用再联网重跑一遍。

    🔴 2026-08-21 第四轮复审：原来每次升级都会重新读一遍配了
    key_tiebreakers 的台账，即使早就迁移完成——多余的联网依赖，网络或
    权限一抖就把一次跟这个迁移毫无关系的升级挡住。
    """

    def test_clean_run_with_nothing_to_migrate_leaves_a_marker(self):
        s = _sheet([{"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
                    "目标国家地区": ""}])  # 消歧字段没填，没有漂移
        with temp_home(ledgers={"ledgers": [_ledger()]}) as home:
            with mock.patch.object(core, "read_ledger_sheet", return_value=s), \
                 mock.patch.object(sys, "argv", ["migrate_rc15_key_state.py", "--apply"]):
                code = migrate.main()
            self.assertEqual(code, 0)
            marker = read_state(home, migrate.MIGRATION_MARKER_FILE)
            self.assertIn(migrate.MIGRATION_ID, marker)

    def test_clean_run_that_actually_renames_leaves_a_marker(self):
        s = _sheet([{"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
                    "目标国家地区": "欧洲"}])
        old_key = "甲公司|杭州分行|46202‖欧洲"
        state_seed = {"stage_entered.json": {f"trade_qq|{old_key}|fill_contact": "2026-08-01"}}
        with temp_home(ledgers={"ledgers": [_ledger()]}, state=state_seed) as home:
            with mock.patch.object(core, "read_ledger_sheet", return_value=s), \
                 mock.patch.object(sys, "argv", ["migrate_rc15_key_state.py", "--apply"]):
                code = migrate.main()
            self.assertEqual(code, 0)
            self.assertIn(migrate.MIGRATION_ID, read_state(home, migrate.MIGRATION_MARKER_FILE))

    def test_dry_run_never_leaves_a_marker(self):
        """--dry-run 是永远只读的硬约束，哪怕"什么都不用做"这个结论也不该落盘。"""
        s = _sheet([{"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
                    "目标国家地区": ""}])
        with temp_home(ledgers={"ledgers": [_ledger()]}) as home:
            with mock.patch.object(core, "read_ledger_sheet", return_value=s), \
                 mock.patch.object(sys, "argv", ["migrate_rc15_key_state.py"]):
                migrate.main()
            self.assertIsNone(read_state(home, migrate.MIGRATION_MARKER_FILE))

    def test_conflict_does_not_leave_a_marker(self):
        s = _sheet([{"企业": "甲公司", "机构": "杭州分行", "访客时间": "46202",
                    "目标国家地区": "欧洲"}])
        old_key, new_key = "甲公司|杭州分行|46202‖欧洲", "甲公司|杭州分行|46202"
        state_seed = {"stage_entered.json": {
            f"trade_qq|{old_key}|fill_contact": "2026-08-01",
            f"trade_qq|{new_key}|fill_contact": "2026-08-10",
        }}
        with temp_home(ledgers={"ledgers": [_ledger()]}, state=state_seed) as home:
            with mock.patch.object(core, "read_ledger_sheet", return_value=s), \
                 mock.patch.object(sys, "argv", ["migrate_rc15_key_state.py", "--apply"]):
                migrate.main()
            self.assertIsNone(read_state(home, migrate.MIGRATION_MARKER_FILE),
                             "没有真正干净地完成，不该留下标记")


if __name__ == "__main__":
    unittest.main()
