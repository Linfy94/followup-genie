#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
提醒事项的身份：一个项目一条，不多不少。

═══════════════════════════════════════════════════════════════════════
🔴 0.4.0-rc1 之前，`_upsert()` 靠**标题精确匹配**认回旧提醒：

    if name of r is "⭕️AI节能盒子「节能测试」某某公司"

而标题是「业务线＋阶段＋企业名」。同一家企业在同一条业务线上完全可以有
**两个项目**（台账里是不同序号的两行），它们的标题一模一样 ——
于是两条催办被合并成一条提醒：业务只看到一个、漏掉另一个，
而且没有任何迹象。列表看起来是干净的，条数也说得通。

修法：身份用 `item.state_key`（`台账|序号|节点`，与 followup_state /
stage_entered 同一把钥匙），它与提醒事项自身 id 的对应关系存在
`state/reminder_map.json`。**业务看到的标题一个字都不用改**，
不塞技术编号、不塞天数。

本文件测的是编排逻辑（认领、映射、去重），AppleScript 文本本身
另有断言守着（不许再出现按标题匹配）。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import re
import unittest
from datetime import date

from harness import core, temp_home, read_state  # noqa: F401 —— 也为挂 sys.path

import reminders_sync  # noqa: E402

TODAY = date(2026, 8, 4)


def item(*, key, name="同名企业有限公司", stage="节能测试",
         line_label="AI节能盒子", node_id="efficiency_test",
         ledger_id="box", action="催节能测试出报告"):
    return core.Item(
        ledger_id=ledger_id, line="盒子", key=key, name=name,
        node_id=node_id, node_name="④节能测试",
        stalled_days=100, clock_from=date(2026, 4, 1),
        clock_source="stage_entered", action=action,
        line_label=line_label, stage=stage, allowance=21,
    )


class FakeReminders:
    """
    够用的假「提醒事项」。只认本模块真实会发出的三种脚本，
    靠关键字辨认、正则取参 —— 不解释 AppleScript。
    """

    RID = re.compile(r'\(id of r as string\) is "([^"]*)"')
    NAME = re.compile(r'name:"([^"]*)"')
    BODY = re.compile(r'body:"([^"]*)"')

    def __init__(self, existing=()):
        self.items = {}          # id -> {"name","body","completed"}
        self.n = 0
        self.created = []
        for name in existing:    # 升级前就存在的旧提醒
            self._new(name, "旧备注")

    def _new(self, name, body):
        self.n += 1
        rid = f"x-apple://REM/{self.n}"
        self.items[rid] = {"name": name, "body": body, "completed": False}
        return rid

    # ── 只读 ──
    def osa(self, script):
        if "set found to false" in script:        # _list_exists
            return "true"
        if 'set out to ""' in script:             # _open_reminders
            return "\n".join(
                f"{rid}\t{v['name']}"
                for rid, v in self.items.items() if not v["completed"])
        raise AssertionError(f"没料到的只读脚本：{script[:80]}")

    # ── 写入 ──
    def osa_write(self, script, *, write_enabled):
        if not write_enabled:
            raise reminders_sync.WriteBlocked
        assert "make new reminder" in script, script[:80]
        name = self.NAME.search(script).group(1)
        body = self.BODY.search(script).group(1)
        m = self.RID.search(script)
        rid = m.group(1) if m else ""
        tgt = self.items.get(rid)
        if rid and tgt and not tgt["completed"]:
            tgt["body"] = body
            tgt["name"] = name
            return f"updated\t{rid}"
        new = self._new(name, body)
        self.created.append(name)
        return f"created\t{new}"

    # 便于断言
    def open_titles(self):
        return sorted(v["name"] for v in self.items.values() if not v["completed"])


def run_sync(items, fake, **cfg_over):
    cfg = {"reminders": {"write": True, "list_name": "项目跟进精灵"}}
    cfg["reminders"].update(cfg_over)
    orig_osa, orig_write = reminders_sync._osa, reminders_sync._osa_write
    reminders_sync._osa = fake.osa
    reminders_sync._osa_write = fake.osa_write
    try:
        import io
        buf = io.StringIO()
        reminders_sync.sync(items, cfg, TODAY, stream=buf)
        return buf.getvalue()
    finally:
        reminders_sync._osa, reminders_sync._osa_write = orig_osa, orig_write


class SameCompanySameStageTest(unittest.TestCase):
    """本次修复的正题。"""

    def two_projects(self):
        # 同企业、同业务线、同阶段，只有台账序号不同 —— 标题会完全一样
        return [item(key="3"), item(key="77")]

    def test_titles_really_are_identical(self):
        a, b = self.two_projects()
        self.assertEqual(reminders_sync._title(a), reminders_sync._title(b),
                         "前提：这两个项目的标题本来就一样，所以标题不能当身份")
        self.assertNotEqual(a.state_key, b.state_key, "但身份必须不同")

    def test_two_projects_get_two_reminders(self):
        fake = FakeReminders()
        with temp_home():
            run_sync(self.two_projects(), fake)
        self.assertEqual(
            len(fake.items), 2,
            "🔴 同企业同阶段的两个项目被合并成一条提醒 —— "
            "业务会漏掉其中一个，且没有任何迹象")

    def test_mapping_records_both(self):
        fake = FakeReminders()
        with temp_home() as home:
            run_sync(self.two_projects(), fake)
            m = read_state(home, reminders_sync.MAP_FILE)
        self.assertEqual(sorted(m), ["box|3|efficiency_test",
                                     "box|77|efficiency_test"])
        self.assertEqual(len(set(m.values())), 2, "两个键不能指向同一条提醒")

    def test_second_run_updates_both_and_creates_nothing(self):
        fake = FakeReminders()
        with temp_home():
            run_sync(self.two_projects(), fake)
            fake.created.clear()
            out = run_sync(self.two_projects(), fake)
        self.assertEqual(fake.created, [], "第二天不该再建")
        self.assertEqual(len(fake.items), 2)
        self.assertIn("更新 2", out)


class AdoptExistingTest(unittest.TestCase):
    """升级当天不许整批重建 —— 那等于把业务的列表翻倍。"""

    def test_old_reminder_is_adopted_not_duplicated(self):
        title = reminders_sync._title(item(key="3"))
        fake = FakeReminders(existing=[title])
        with temp_home() as home:
            out = run_sync([item(key="3")], fake)
            m = read_state(home, reminders_sync.MAP_FILE)
        self.assertEqual(len(fake.items), 1, "旧提醒该被认领，不是再建一条")
        self.assertEqual(fake.created, [])
        self.assertIn("认领旧提醒 1", out)
        self.assertEqual(list(m.values()), ["x-apple://REM/1"])

    def test_adopted_reminder_gets_fresh_body(self):
        title = reminders_sync._title(item(key="3"))
        fake = FakeReminders(existing=[title])
        with temp_home():
            run_sync([item(key="3")], fake)
        body = list(fake.items.values())[0]["body"]
        self.assertIn("超期 79 天", body, "认领之后备注要刷新成今天的天数")

    def test_one_old_reminder_two_projects_splits(self):
        """
        🔴 历史遗留的「合并」状态要能自动拆开。

        升级前两个项目共用一条提醒。升级后第一个认领它，
        第二个认不到（一个 id 只能被认领一次）于是新建 ——
        列表从此正确。少了「只能认领一次」这条，两个键会指向同一条提醒，
        每天互相覆盖备注，病没治反而更隐蔽。
        """
        title = reminders_sync._title(item(key="3"))
        fake = FakeReminders(existing=[title])
        with temp_home() as home:
            run_sync([item(key="3"), item(key="77")], fake)
            m = read_state(home, reminders_sync.MAP_FILE)
        self.assertEqual(len(fake.items), 2)
        self.assertEqual(len(set(m.values())), 2)


class DeletedReminderTest(unittest.TestCase):

    def test_deleted_reminder_is_recreated(self):
        """提醒只做通知、不承担状态。业务删了，下次照常重建。"""
        fake = FakeReminders()
        with temp_home() as home:
            run_sync([item(key="3")], fake)
            fake.items.clear()          # 业务把它删了
            run_sync([item(key="3")], fake)
            m = read_state(home, reminders_sync.MAP_FILE)
        self.assertEqual(len(fake.items), 1)
        self.assertEqual(list(m.values()), [list(fake.items)[0]],
                         "映射要指向重建后的那条，不能停在死 id 上")

    def test_completed_reminder_is_recreated(self):
        fake = FakeReminders()
        with temp_home():
            run_sync([item(key="3")], fake)
            for v in fake.items.values():
                v["completed"] = True   # 业务勾了完成
            run_sync([item(key="3")], fake)
        self.assertEqual(len(fake.items), 2, "勾完成后应新建一条，与升级前行为一致")


class ScriptShapeTest(unittest.TestCase):
    """AppleScript 文本本身的守卫 —— 上面的假件测不到这一层。"""

    def capture(self, rid):
        """直接调 _upsert，把它发出的 AppleScript 原文抓出来。"""
        seen = []
        orig = reminders_sync._osa_write
        reminders_sync._osa_write = lambda s, *, write_enabled: (
            seen.append(s) or "created\tx-apple://REM/9")
        try:
            reminders_sync._upsert("列表", "标题", "备注", TODAY, rid,
                                   write_enabled=True)
        finally:
            reminders_sync._osa_write = orig
        return seen[0]

    def test_never_matches_by_title(self):
        for rid in ("", "x-apple://REM/1"):
            with self.subTest(rid=rid):
                self.assertNotIn(
                    "name of r is", self.capture(rid),
                    "🔴 又按标题匹配了 —— 同企业同阶段的两个项目会被合并成一条")

    def test_matches_by_id_when_given_one(self):
        self.assertIn("(id of r as string) is \"x-apple://REM/1\"",
                      self.capture("x-apple://REM/1"))

    def test_no_lookup_at_all_without_an_id(self):
        """没有 id 就是没建过，不该去翻列表 —— 翻了就有翻错的可能。"""
        self.assertNotIn("id of r as string", self.capture(""))

    def test_returns_the_new_id(self):
        orig = reminders_sync._osa_write
        reminders_sync._osa_write = lambda s, *, write_enabled: "created\tREM-42"
        try:
            r, rid = reminders_sync._upsert("列表", "标题", "备注", TODAY, "",
                                            write_enabled=True)
        finally:
            reminders_sync._osa_write = orig
        self.assertEqual((r, rid), ("created", "REM-42"))

    def test_map_file_name_is_stable(self):
        """改名会让升级后所有映射失效、整批重建。"""
        self.assertEqual(reminders_sync.MAP_FILE, "reminder_map.json")


class DryRunTest(unittest.TestCase):

    def test_write_disabled_touches_nothing(self):
        fake = FakeReminders()
        with temp_home() as home:
            out = run_sync([item(key="3")], fake, write=False)
            m = read_state(home, reminders_sync.MAP_FILE)
        self.assertEqual(fake.items, {}, "演练模式不许碰提醒事项")
        self.assertIsNone(m, "演练模式不该写映射文件")
        self.assertIn("演练模式", out)


if __name__ == "__main__":
    unittest.main()
