#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
「你现在这个解释器不是定时任务跑的那个」必须被自动指出来。

═══════════════════════════════════════════════════════════════════════
🔴 2026-08-14 排查一天三个故障，最大的一块时间浪费在这上面：
   这台机器有三个 Python、三套 SSL 后端

       /usr/bin/python3                        3.9.6   LibreSSL 2.8.3  ← 不支持 TLS 1.3
       /opt/homebrew/bin/python3               3.14.6  OpenSSL 3.6.3
       ~/.hermes/hermes-agent/venv/bin/python  3.11.15 OpenSSL 3.5.7   ← cron 跑的

   我敲了裸 `python3` 落到 Homebrew 那个，于是「我自己用错解释器」
   和「真故障」混在一起，白花近一小时。而 `doctor` 当时关于运行环境
   **一个字都不报**（实测 grep sys.version 零命中）。

   🔴 修法刻意**不写死路径**：`~/.hermes/...` 只在这台 Hermes 上成立，
      同一个包也装到 WorkBuddy、也装到业务电脑。靠比对，处处成立。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import unittest
from unittest import mock

from harness import core  # noqa: F401 —— 挂 sys.path

import core as core_mod
import doctor


class FingerprintTest(unittest.TestCase):

    def test_reports_interpreter_and_ssl_backend(self):
        fp = core_mod.runtime_fingerprint()
        for k in ("executable", "python", "ssl"):
            with self.subTest(k=k):
                self.assertTrue(fp.get(k), f"{k} 是空的，这条自检就没意义")

    def test_survives_a_python_without_ssl(self):
        """自检代码自己不许裸崩 —— 诊断工具在被诊断的东西坏掉时崩溃等于没有。"""
        with mock.patch.object(core_mod, "ssl", object()):
            self.assertIn("未知", core_mod.runtime_fingerprint()["ssl"])


class MismatchTest(unittest.TestCase):

    def _run(self, recorded):
        doc = doctor.Doc()
        with mock.patch.object(doctor.core, "read_health",
                               return_value={"runtime": recorded} if recorded else {}):
            doctor.check_runtime(doc)
        return doc, "\n".join(f"{lv} {t} {d}" for lv, t, d in doc.rows)

    def test_warns_when_interpreter_differs(self):
        """🔴 这条不出现，就等于回到 2026-08-14 那一小时。"""
        doc, text = self._run({"executable": "/somewhere/else/python",
                               "python": "3.11.15", "ssl": "OpenSSL 3.5.7"})
        self.assertEqual(doc.warn, 1)
        self.assertIn("/somewhere/else/python", text)
        self.assertIn("不是定时任务跑的那个", text)

    def test_quiet_when_interpreter_matches(self):
        """一致时不许报警 —— 天天一条黄字，人就不看了。"""
        me = core_mod.runtime_fingerprint()
        doc, _ = self._run(me)
        self.assertEqual(doc.warn, 0)
        self.assertEqual(doc.bad, 0)

    def test_no_record_yet_is_not_a_warning(self):
        """还没真跑过时不该报警：那是新装机的正常状态，不是故障。"""
        doc, text = self._run(None)
        self.assertEqual(doc.warn, 0)
        self.assertIn("还没有记录", text)

    def test_unreadable_health_does_not_crash(self):
        doc = doctor.Doc()
        with mock.patch.object(doctor.core, "read_health",
                               side_effect=OSError("坏了")):
            doctor.check_runtime(doc)   # 不抛就算过
        self.assertEqual(doc.bad, 0)


class RecordedByRealRunTest(unittest.TestCase):
    """
    🔴 指纹必须由**真实运行**写入。若 --dry-run 也写，比对的就变成
       「谁最后随手试跑了一下」，这条自检立刻失去意义。
    """

    def test_dry_run_is_blocked_by_the_readonly_gate(self):
        core_mod.set_read_only(True)
        self.addCleanup(core_mod.set_read_only, False)
        with mock.patch.object(core_mod, "write_state") as w:
            core_mod.update_health(runtime=core_mod.runtime_fingerprint())
        w.assert_not_called()

    def test_main_flow_records_it_on_real_runs(self):
        import pathlib
        src = (pathlib.Path(core_mod.__file__).parent
               / "check_followup.py").read_text(encoding="utf-8")
        self.assertIn("runtime=core.runtime_fingerprint()", src,
                      "主流程没有记录解释器指纹，doctor 永远无从比对")


if __name__ == "__main__":
    unittest.main()
