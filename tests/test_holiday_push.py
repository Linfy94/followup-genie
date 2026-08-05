#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
法定节假日不推送。

═══════════════════════════════════════════════════════════════════════
🔴 0.3.0-rc3 及之前：cron 的星期字段写死成周一至周五，只排得掉周末，
   排不掉国庆春节。（那个写法后来又暴露了补班日不触发的问题，
   0.4.0-rc1 改回每天跑、由本闸门统一判断，见 test_schedule.py。）

    check_followup 里**根本没有「今天是不是工作日」这个判断** ——
    节假日表只喂给②的复提醒计算，从不参与「今天推不推」。

    结果：国庆 10/1–10/7 里有五天照推、春节九天连休里有五天照推。
    业务在放假，每天早上 9 点收催办。

🔴 而修法有一个不显眼的陷阱，本文件的 HealthMustStillBeWrittenTest 就是
   为它存在的：watchdog 的 missed_runs() 只跳周末、不认识节假日
   （watchdog.py 刻意不 import core，拿不到节假日表）。

   如果节假日让脚本静默退出、不写 last_full_success，watchdog 会把国庆
   七天里的五个工作日班次数成「错过 5 次」，而告警阈值是 2 ——
   **国庆第二天就误报「任务根本没跑」。**

   所以节假日当天必须照常运行、照常写健康记录，只是不投递。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import unittest
from datetime import date

from harness import (make_sheet, row, days_ago, temp_home, run_main,
                     read_state, output_cfg, rules_cfg)

# 用春节而不是国庆：`--today` 是未来日期时会被参数护栏挡下
# （不允许与 --force-push 同用，那会写出未来日期的快照），
# 而这套测试必须走真实运行才能验到投递与健康记录。
#
# 2026-02-16 是春节连休（2/15–2/23）里的周一 —— 既是周一又是法定假日，
# 正是 cron `1-5` 拦不住、而本次修复要拦住的那一天。
HOLIDAY = date(2026, 2, 16)
WORKDAY = date(2026, 2, 25)      # 春节后的正常周三
MAKEUP = date(2026, 2, 28)       # 周六补班日：上班，要推
# 普通周六，不在 holidays 也不在 workdays 里。cron 改成每天之后，
# 「周末不推」这件事完全落在 is_workday() 身上，得单独测住。
PLAIN_SATURDAY = date(2026, 3, 7)


def overdue_sheet(today):
    """两条稳定超期的项目，与 test_notify_commit 同构。"""
    return make_sheet([
        row(1, "甲公司", tech="待收资",
            reported=days_ago(today, 60), progress=days_ago(today, 60)),
        row(2, "乙公司", tech="",
            reported=days_ago(today, 30), progress=days_ago(today, 30)),
    ])


def run_on(day, **kw):
    return run_main([f"--today={day}", "--force-push"], overdue_sheet(day), **kw)


class HolidaySkipsPushTest(unittest.TestCase):
    """法定节假日不推企微，但这不是故障。"""

    def test_holiday_does_not_push(self):
        with temp_home():
            r = run_on(HOLIDAY)
            self.assertEqual(r.posts, [],
                             "国庆假期里不该往业务群推催办")

    def test_holiday_exits_zero(self):
        """不推不是失败 —— 退出码非零会让 Hermes 报错、并触发故障告警。"""
        with temp_home():
            r = run_on(HOLIDAY)
            self.assertEqual(r.code, 0, r.err)
            self.assertFalse(r.alerted, "节假日不推是正常行为，不该告警")

    def test_holiday_does_not_commit_last_notified(self):
        """
        没真发出去就不能记「已通知」，否则假期这几天的项目会进静默期，
        节后业务再也收不到 —— 这正是整套两级状态提交要防的静默漏催。
        """
        with temp_home() as home:
            run_on(HOLIDAY)
            fs = read_state(home, "followup_state.json")
            notified = [k for k, v in fs.items() if v.get("last_notified")]
            self.assertEqual(notified, [],
                             f"节假日没投递，不该有投递凭证，实际 {fs}")

    def test_holiday_still_says_why_locally(self):
        """
        本地必须留痕说明原因。业务手动点一下看到「毫无反应」，
        和「今天压根没跑起来」长得一模一样。
        """
        with temp_home():
            r = run_on(HOLIDAY)
            self.assertIn("法定节假日", r.out,
                          f"本地回执要说清为什么没推，实际输出：{r.out!r}")


class HealthMustStillBeWrittenTest(unittest.TestCase):
    """
    🔴 本文件最重要的一条：节假日当天**必须**照常写 last_full_success。

    watchdog 只会数「错过了几个本该执行的 9:00」，而它不认识节假日。
    这里一旦不写，国庆七天里的五个工作日班次会被数成错过 5 次，
    阈值 2 —— 国庆第二天就误报「任务根本没跑」。

    也就是说：修好了「假期骚扰」，却换来一次「假期误报故障」。
    """

    def test_holiday_run_still_records_full_success(self):
        with temp_home() as home:
            r = run_on(HOLIDAY)
            self.assertEqual(r.code, 0, r.err)
            h = read_state(home, "health.json")
            self.assertTrue(
                h.get("last_full_success"),
                "🔴 节假日没写 last_full_success —— watchdog 会把假期当成"
                "「任务根本没跑」，国庆第二天就误报故障告警")

    def test_holiday_run_is_not_a_warning_run(self):
        """
        「今天是节假日」绝不能进 run_warnings。check_followup 里
        `exit_code == 0 and not run_warnings` 才写 last_full_success，
        塞进去等于亲手造出上面那个误报。
        """
        with temp_home() as home:
            run_on(HOLIDAY)
            h = read_state(home, "health.json")
            summary = h.get("last_run_summary") or {}
            self.assertIn("节假日", str(summary.get("delivery")),
                          f"投递摘要要说明原因，实际 {summary}")
            self.assertEqual(h.get("consecutive_failures"), 0)


class NormalDaysUnaffectedTest(unittest.TestCase):
    """判定与投递解耦：本次只动「推不推」，不动「催谁」。"""

    def test_ordinary_workday_still_pushes(self):
        with temp_home() as home:
            r = run_on(WORKDAY)
            self.assertTrue(r.posts, "普通工作日必须照常推")
            self.assertEqual(r.code, 0, r.err)
            fs = read_state(home, "followup_state.json")
            notified = [k for k, v in fs.items() if v.get("last_notified")]
            self.assertEqual(len(notified), 2, "正常推送后两条都该记已通知")

    def test_makeup_workday_pushes(self):
        """
        🔴 调休补班日是周六但要上班，必须推。

        0.4.0-rc1 之前 cron 的星期字段写死成周一至周五，这一天**根本不触发** ——
        脚本层判断得再对也没用，业务在上班却收不到催办，
        而它看起来和「今天没有要催的」一模一样。现在 cron 每天叫醒，
        由这里判定，这条测试才真正对应线上行为。
        """
        self.assertEqual(MAKEUP.weekday(), 5, "确认 2/28 确实是周六")
        with temp_home():
            r = run_on(MAKEUP)
            self.assertTrue(r.posts, "补班日上班，应该推")

    def test_ordinary_weekend_does_not_push(self):
        """
        🔴 普通周末不推 —— 这条以前由 cron 的 `1-5` 兜着，现在没人兜了。

        cron 改成每天之后，「周六不推」完全依赖 is_workday()。
        这条要是坏了，业务每个周末都会收到催办，
        而这正是催办类产品被屏蔽的最快途径。
        """
        self.assertEqual(PLAIN_SATURDAY.weekday(), 5, "确认确实是周六")
        with temp_home() as home:
            r = run_on(PLAIN_SATURDAY)
            self.assertEqual(r.posts, [], "普通周末不该推")
            self.assertEqual(r.code, 0, "不推不是故障")
            fs = read_state(home, "followup_state.json")
            notified = [k for k, v in (fs or {}).items() if v.get("last_notified")]
            self.assertEqual(notified, [], "没推就不该记已通知")

    def test_weekend_says_weekend_not_holiday(self):
        """
        措辞要说对是哪一种非工作日。业务在普通周六看到「今天是法定节假日」，
        会以为节假日表配错了，白跑一趟排查。
        """
        with temp_home():
            r = run_on(PLAIN_SATURDAY)
        self.assertIn("周末", r.out)
        self.assertNotIn("法定节假日", r.out)

    def test_due_list_is_identical_on_holiday_and_workday(self):
        """节假日只拦投递，判定一个字都不该变 —— 停滞天数照常累计。"""
        with temp_home():
            hol = run_on(HOLIDAY)
        with temp_home():
            wd = run_on(WORKDAY)
        for name in ("甲公司", "乙公司"):
            self.assertIn(name, hol.out, "节假日仍要算出待催清单，只是不推")
            self.assertIn(name, wd.out)


class SwitchesTest(unittest.TestCase):
    """两个关掉它的途径都要有效 —— 否则业务想改口径只能改代码。"""

    def test_skip_can_be_turned_off(self):
        cfg = output_cfg(notify={"primary": "wecom_webhook",
                                 "skip_non_workdays": False})
        with temp_home(output=cfg):
            r = run_on(HOLIDAY)
            self.assertTrue(r.posts,
                            "显式关掉开关后，节假日应恢复照推（维持旧行为）")

    def test_no_effect_when_holidays_not_enabled(self):
        """
        业务电脑的真实形态：节假日表还没拷过去、exclude_holidays 是 false。
        那时 is_workday() 只排周末，而 cron 本来就排了周末 —— 等于维持现状。

        这是**安全的降级**：不会因为漏拷一个文件就静默改变推送行为。
        （漏拷本身由 doctor 那条「工作日口径与规则对不上」负责喊。）
        """
        rules = rules_cfg()
        rules["workday"] = {"exclude_weekends": True, "exclude_holidays": False}
        with temp_home(rules=rules, write_holidays=False):
            r = run_on(HOLIDAY)
            self.assertTrue(
                r.posts,
                "没启用节假日表时不该跳过 —— 否则漏拷文件会静默改变行为")


class DiagnosticModesUnaffectedTest(unittest.TestCase):
    """诊断模式本来就不发不写，这条改动不该动到它们。"""

    def test_dry_run_on_holiday_writes_nothing(self):
        with temp_home() as home:
            r = run_main(["--dry-run"], overdue_sheet(date.today()))
            self.assertEqual(r.posts, [])
            self.assertEqual(r.code, 0, r.err)
            # 文件压根没被创建 —— 这比「内容为空」更强
            self.assertIsNone(read_state(home, "health.json"),
                              "诊断模式不该写健康记录")

    def test_today_without_force_push_writes_nothing(self):
        with temp_home() as home:
            r = run_main([f"--today={HOLIDAY}"], overdue_sheet(HOLIDAY))
            self.assertEqual(r.posts, [])
            self.assertEqual(r.code, 0, r.err)
            self.assertIsNone(read_state(home, "followup_state.json"))


if __name__ == "__main__":
    unittest.main()
