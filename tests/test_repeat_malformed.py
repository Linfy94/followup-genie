#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
repeat 写坏了必须在**离线校验**就拦住，不能留到定时任务里炸。

rc8 放开了四种节律写法，就是为了让业务自己改提醒时间。那么「改错了」
从此是常态而不是意外，而 rc8 的校验只挡住了「写法认得出但值不对」这一类：

  · `"repeat": "每周一"`      —— 整个不是对象，离线放行，运行时 AttributeError
  · `{"days": "七"}`          —— 离线放行，运行时 int() 炸
  · `{"days": -3}`            —— 离线放行，且**不炸**：`(today-last).days >= -3`
                                 恒真，节律静默变成「每天提醒」。最坏的一种
  · `{"weekday": [], "days": 7}` —— 两种节律同时在场却放行（旧判断看「值真不真」，
                                 空数组是假的，于是当成只配了 days）

前三条是「配置错 → 静默或崩溃」，第四条是「节律看着改了、实际没改」——
都是这个项目一路在消灭的那个形状，只是这次出现在业务自己动手的那一层。
"""

from __future__ import annotations

import unittest

from harness import core, temp_home, rules_cfg


def offline(repeat) -> list[str]:
    """走 doctor --validate-config 那条路：不碰台账、不联网。"""
    rules = {"rulesets": {"box": {"nodes": [
        {"id": "x", "name": "①测试", "enabled": True,
         "threshold": {"days": 7}, "repeat": repeat}]}}, "workday": {}}
    return [e for e in core.validate_configs({"ledgers": []}, rules, {})
            if "repeat" in e]


class MalformedRepeatTest(unittest.TestCase):

    def test_repeat_must_be_an_object(self):
        """业务照着中文说明写成 "每周一" 是最自然的手滑。"""
        for bad in ("每周一", 7, ["Mon"]):
            with self.subTest(bad=bad):
                self.assertTrue(offline(bad), f"repeat={bad!r} 必须离线报错")

    def test_repeat_object_does_not_crash_validators(self):
        """🔴 校验函数本身不许裸崩 —— 它是用来报错的，不能变成错误本身。"""
        for bad in ("每周一", 7, ["Mon"], None):
            with self.subTest(bad=bad):
                core.repeat_errors(bad, "X")     # 不抛异常即可
                core.cadence_text(bad)

    def test_days_must_be_a_positive_integer(self):
        """
        🔴 `{"days": -3}` 是这一组里最坏的：它不炸。
           判定是 `(today - last).days >= int(days)`，负数恒真，
           于是节律静默变成「每天提醒」，而配置看起来是配过的。
        """
        for bad in (-3, 0, "七", 7.9, True, None):
            with self.subTest(bad=bad):
                self.assertTrue(offline({"days": bad}),
                                f"days={bad!r} 必须离线报错")
        for bad in (0, -1, "二"):
            with self.subTest(workdays=bad):
                self.assertTrue(offline({"workdays": bad}),
                                f"workdays={bad!r} 必须离线报错")

    def test_exclusivity_is_by_key_presence_not_truthiness(self):
        """
        🔴 「改到一半」的形状：把 weekday 的值删空了、键忘了删，又加了 days。
           按值真不真判会看成「只配了 days」而放行 —— 业务以为按 weekday 走。
        """
        for combo in ({"weekday": [], "days": 7},
                      {"monthday": [], "workdays": 2},
                      {"weekday": None, "days": 7}):
            with self.subTest(combo=combo):
                self.assertTrue(any("只能配一种" in e for e in offline(combo)),
                                f"{combo!r} 必须报「只能配一种」")

    def test_good_configs_still_pass(self):
        """🔴 收紧之后现有生产配置必须一条都不受影响。"""
        for good in ({"days": 7}, {"days": 1}, {"workdays": 2},
                     {"weekday": "Wed"}, {"weekday": ["Mon", "Thu"]},
                     {"monthday": [1, 15]}):
            with self.subTest(good=good):
                self.assertEqual(offline(good), [])


class DoctorReusesCoreTest(unittest.TestCase):
    """
    🔴 doctor 的口径表曾自带一份 repeat 解读，于是走样了两处：
       ① 数组直接打印成 `只在每周['Mon', 'Thu']提醒`，业务读不懂；
       ② 它的「缺 repeat」检查漏了 monthday，配 monthday 会被**误报缺失**。
       两处都是复制实现必然的下场 —— 现在共用 core 的那一份。
    """

    def _rows(self, repeat) -> str:
        import doctor
        rules = rules_cfg(collect={"repeat": repeat})
        with temp_home(rules=rules):
            doc = doctor.Doc()
            doctor.check_configs(doc)
        return "\n".join(f"{lv} {t} {d}" for lv, t, d in doc.rows)

    def test_weekday_list_reads_like_chinese(self):
        out = self._rows({"weekday": ["Mon", "Thu"]})
        self.assertIn("周一/周四提醒", out)
        self.assertNotIn("['Mon'", out)

    def test_monthday_is_not_reported_as_missing(self):
        out = self._rows({"monthday": [1, 15]})
        self.assertNotIn("缺 repeat", out)
        self.assertIn("每月 1/15 号提醒", out)


if __name__ == "__main__":
    unittest.main()
