#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试脚手架。

═══════════════════════════════════════════════════════════════════════
硬要求：**测试绝不碰真实世界**。

  · HERMES_HOME 指向临时目录 → 不碰真实配置、不碰真实状态
  · qqdoc.read_sheet 打桩     → 不碰腾讯文档
  · wecom_push._post 打桩     → 不碰企微群（那里有真人）
  · check_followup 的告警打桩 → 不碰 telegram
  · reminders.write=false     → 不碰提醒事项 / 不调 osascript

这不是洁癖：这套测试要覆盖「推送失败」「告警失败」这类路径，
真连出去的话，每跑一次测试业务群就被打扰一次。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import core          # noqa: E402
import qqdoc         # noqa: E402
import wecom_push    # noqa: E402
import check_followup  # noqa: E402
import doctor        # noqa: E402
import watchdog      # noqa: E402
import reminders_sync  # noqa: E402,F401  —— 让打桩点可被测试引用

SERIAL_EPOCH = qqdoc.SERIAL_EPOCH

# 台账的列，与真实的盒子登记册一致（只保留判定用得到的）
COLUMNS = ["序号", "地点", "项目名称", "需求上报日期", "最新进展日期",
           "技术确认", "客户意见", "安装调试", "节能测试", "备注"]


# ── 配置模板（台账无关的最小可用集） ──────────────────────────────────

def ledgers_cfg(**over) -> dict:
    led = {
        "id": "box", "name": "测试台账", "line": "盒子",
        "display_name": "AI节能盒子", "enabled": True,
        "source": "tencent_mcp", "file_id": "FILE", "sheet_id": "000001",
        "ruleset": "box",
        "key_field": "序号", "name_field": "项目名称",
        "required_columns": ["序号", "地点", "项目名称", "需求上报日期",
                             "最新进展日期", "技术确认", "安装调试",
                             "节能测试", "备注"],
        "scope_filters": [{"field": "地点", "op": "in", "values": ["杭州", "深圳"]}],
        "known_values": {"地点": ["杭州", "北京", "深圳"]},
        "manual_terminal": [],
        "terminal_states": [
            {"field": "技术确认", "op": "in", "values": ["不可行"]},
            {"field": "客户意见", "op": "in", "values": ["不同意"]},
        ],
        "terminal_column": {"enabled": False, "field": "终止",
                            "truthy_values": ["终止"]},
        "terminal_note_phrases": ["关闭", "放弃", "无法调通"],
        "terminal_note_exceptions": [],
        "paused": [],
        "gray_list_for_review": [],
    }
    led.update(over)
    return {"ledgers": [led]}


def rules_cfg(**node_over) -> dict:
    def node(nid, name, stage, when, clock, days, boundary, repeat):
        n = {"id": nid, "name": name, "stage": stage, "enabled": True,
             "when": when, "clock": clock,
             "threshold": {"days": days, "boundary": boundary},
             "repeat": repeat, "action": "", "backlog_note": ""}
        n.update(node_over.get(nid) or {})
        return n

    return {
        "rulesets": {"box": {"nodes": [
            node("collect", "①收资", "待收资",
                 [{"field": "技术确认", "op": "in", "values": ["待收资"]}],
                 {"field": "需求上报日期"}, 7, "on", {"days": 7}),
            node("expert", "②专家评估", "待专家评估",
                 [{"field": "技术确认", "op": "empty"}],
                 {"field": "需求上报日期"}, 3, "after", {"workdays": 2}),
            node("install", "③预调试/安装", "预调试/安装",
                 [{"field": "技术确认", "op": "in",
                   "values": ["可行", "需预调试", "有条件可行"]},
                  {"field": "安装调试", "op": "empty"}],
                 {"stage_entered": True, "fallback": "最新进展日期"},
                 21, "after", {"days": 7}),
            node("efficiency_test", "④节能测试", "节能测试",
                 [{"field": "安装调试", "op": "equals", "value": "完成"},
                  {"field": "节能测试", "op": "not_equals", "value": "完成"}],
                 {"stage_entered": True, "fallback": "最新进展日期"},
                 21, "after", {"days": 7}),
        ]}},
        "workday": {"exclude_weekends": True, "exclude_holidays": True},
    }


def output_cfg(**over) -> dict:
    cfg = {
        "notify": {"primary": "wecom_webhook"},
        "alert": {"enabled": True, "timeout_seconds": 5},
        "terminal_hint": {"enabled": True, "stages": ["待收资"],
                          "days": 60, "text": "超2个月 可考虑终止"},
        "reminders": {"write": False, "list_name": "测试列表"},
        "stdout": {"enabled": True},
        "wecom_webhook": {"enabled": True, "msgtype": "markdown_v2",
                          "split_bytes": 4000},
    }
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def holidays_cfg() -> dict:
    return {
        "covers_year": 2026, "verified": True,
        # 只放测试要用到的：春节连休 + 一个补班日 + 国庆
        "holidays": ["2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18",
                     "2026-02-19", "2026-02-20", "2026-02-21", "2026-02-22",
                     "2026-02-23",
                     "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
                     "2026-10-05", "2026-10-06", "2026-10-07"],
        "workdays": ["2026-02-14", "2026-02-28", "2026-10-10"],
    }


# ── 假表 ──────────────────────────────────────────────────────────────

def _cell(v):
    """按真实接口的形态造一个单元格，以便走通 cell_text / cell_date 的真实分支。"""
    if isinstance(v, date):
        return {"value_type": "NUMBER", "number_value": (v - SERIAL_EPOCH).days,
                "string_value": ""}
    if isinstance(v, bool):
        return {"value_type": "BOOL", "bool_value": v, "string_value": ""}
    if isinstance(v, int):
        # 序号就是 NUMBER 型：string_value 恒为空。这是 P0-2 那个坑，
        # 假表必须如实复现，否则测试跑的是一个比真实世界宽容的环境。
        return {"value_type": "NUMBER", "number_value": v, "string_value": ""}
    return {"value_type": "STRING", "string_value": str(v)}


def make_sheet(rows: list[dict], columns: list[str] | None = None) -> qqdoc.Sheet:
    """
    rows 是 [{列名: 值}]，值为 None 或 "" 时**不产生单元格** ——
    cells 模式下空单元格根本不返回（P0-4），假表必须一致。
    """
    cols = columns or COLUMNS
    grid: dict[int, dict[int, dict]] = {0: {i: _cell(c) for i, c in enumerate(cols)}}
    for r, row in enumerate(rows, start=1):
        cells = {}
        for i, c in enumerate(cols):
            v = row.get(c)
            if v is None or v == "":
                continue
            cells[i] = _cell(v)
        grid[r] = cells
    return qqdoc.Sheet(list(cols), grid)


def row(key: int, name: str, *, place="杭州", reported=None, progress=None,
        tech="", opinion="", install="", test="", note="") -> dict:
    return {"序号": key, "地点": place, "项目名称": name,
            "需求上报日期": reported, "最新进展日期": progress,
            "技术确认": tech, "客户意见": opinion, "安装调试": install,
            "节能测试": test, "备注": note}


def days_ago(base: date, n: int) -> date:
    return base - timedelta(days=n)


# ── 临时 HERMES_HOME ──────────────────────────────────────────────────

@contextlib.contextmanager
def temp_home(*, ledgers=None, rules=None, output=None, holidays=None,
              state=None, env_lines=None, write_holidays=True):
    """
    搭一个完整的临时 <hermes>：config/ + state/ + .env。

    绝不复制真实 .env —— 那里有腾讯文档 token 和企微 webhook，
    拷进 /tmp 等于把凭证撒出去。测试用假值。
    """
    with tempfile.TemporaryDirectory(prefix="fg-test-") as d:
        home = Path(d)
        (home / "followup" / "config").mkdir(parents=True)
        (home / "followup" / "state").mkdir(parents=True)

        def dump(name, obj):
            (home / "followup" / "config" / name).write_text(
                json.dumps(obj, ensure_ascii=False), encoding="utf-8")

        dump("ledgers.json", ledgers if ledgers is not None else ledgers_cfg())
        dump("rules.json", rules if rules is not None else rules_cfg())
        dump("output.json", output if output is not None else output_cfg())
        if write_holidays:
            hol = holidays if holidays is not None else holidays_cfg()
            if isinstance(hol, str):   # 用来造「表损坏」的场景
                (home / "followup" / "config" / "holidays.json").write_text(
                    hol, encoding="utf-8")
            else:
                dump("holidays.json", hol)

        lines = env_lines if env_lines is not None else [
            "TENCENT_DOCS_TOKEN=fake-token-for-tests",
            "FOLLOWUP_WECOM_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=fake",
            "FOLLOWUP_ALERT_TARGET=telegram:000000",
        ]
        (home / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")

        for name, obj in (state or {}).items():
            (home / "followup" / "state" / name).write_text(
                obj if isinstance(obj, str)
                else json.dumps(obj, ensure_ascii=False), encoding="utf-8")

        old = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(home)
        core.STATE_DAMAGE.clear()
        core.BLOCKED_WRITES.clear()
        core.set_read_only(False)   # 上一个用例可能把它留在只读上
        try:
            yield home
        finally:
            core.STATE_DAMAGE.clear()
            core.BLOCKED_WRITES.clear()
            core.set_read_only(False)
            if old is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old


def read_state(home: Path, name: str):
    p = home / "followup" / "state" / name
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def state_files(home: Path) -> set[str]:
    d = home / "followup" / "state"
    return {p.name for p in d.iterdir()} if d.exists() else set()


# ── 运行一次主脚本 ────────────────────────────────────────────────────

class Run:
    """一次 check_followup.main() 的结果。"""

    def __init__(self, code, out, err, posts, alerts):
        self.code = code
        self.out = out
        self.err = err
        self.posts = posts    # [(url, payload), ...] 实际发出去的企微请求
        self.alerts = alerts  # [(argv, ...)] 实际发出去的告警

    @property
    def alerted(self) -> bool:
        return bool(self.alerts)


def run_main(argv, sheet, *, post_results=None, alert_ok=True,
             hermes_found=True, read_sheet_error=None, push_raises=None,
             fingerprints=None):
    """
    跑一次主脚本。

    post_results：企微每一片的成败，如 [True, False]。缺省全部成功。
                  给 True/False 单值则所有片同此结果。
    alert_ok    ：模拟 hermes send 的成败。
    """
    posts: list = []
    alerts: list = []
    seq = list(post_results) if isinstance(post_results, (list, tuple)) else None
    single = post_results if isinstance(post_results, bool) else True

    def fake_post(url, payload):
        posts.append((url, payload))
        if seq is not None:
            i = len(posts) - 1
            ok = seq[i] if i < len(seq) else seq[-1]
        else:
            ok = single
        return (True, "ok") if ok else (False, "errcode=93000 fake failure")

    def fake_read_sheet(file_id, sheet_id):
        if read_sheet_error:
            raise qqdoc.LedgerError(read_sheet_error)
        return sheet

    def fake_subprocess_run(argv_, **kw):
        alerts.append(argv_)
        class R:
            returncode = 0 if alert_ok else 3
            stdout = ""
            stderr = "" if alert_ok else "fake send failure"
        return R()

    def fake_push(*a, **kw):
        raise push_raises

    def fake_fingerprint(fid):
        """
        只读性核对与需求文档变更探测共用同一个 manage.query_file_info。

        缺省给一个固定值（只读性核对只关心「两次一样」）。需求文档那组要按
        file_id 分别给值，所以 fingerprints 可以是 {file_id: 指纹或异常}
        或一个可调用对象。异常实例会被抛出 —— 「读不到」是必须能造出来的场景。
        """
        if fingerprints is None:
            return {"last_modify_time": 1, "last_modify_name": "x"}
        got = fingerprints(fid) if callable(fingerprints) \
            else fingerprints.get(fid)
        if isinstance(got, Exception):
            raise got
        if got is None:
            raise AssertionError(f"测试没给 file_id={fid!r} 准备指纹")
        return got

    out, err = io.StringIO(), io.StringIO()
    patches = [
        # 真实的群机器人限速是每分钟 20 条，所以每片之间 sleep 1 秒。
        # 测试里没有真的机器人，睡着纯属浪费——拆成 10 片就白等 10 秒。
        mock.patch.object(wecom_push, "RATE_SLEEP", 0),
        mock.patch.object(qqdoc, "read_sheet", fake_read_sheet),
        mock.patch.object(wecom_push, "_post", fake_post),
        mock.patch.object(check_followup, "_hermes_bin",
                          lambda: "/fake/hermes" if hermes_found else None),
        mock.patch.object(check_followup.subprocess, "run", fake_subprocess_run),
        mock.patch.object(qqdoc, "file_fingerprint", fake_fingerprint),
        contextlib.redirect_stdout(out),
        contextlib.redirect_stderr(err),
    ]
    if push_raises is not None:
        patches.insert(1, mock.patch.object(wecom_push, "push", fake_push))

    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        code = check_followup.main(argv)

    return Run(code, out.getvalue(), err.getvalue(), posts, alerts)


def run_doctor(argv: list[str] | None = None) -> tuple[int, str]:
    """
    跑一次自检。**只用于不联网的 --validate-config**，别的分支会去碰腾讯文档。

    存在的意义：诊断工具在被诊断的东西坏掉时**自己崩掉**，等于没有诊断工具。
    配置写坏正是最需要它说人话的时刻。
    """
    out = io.StringIO()
    with contextlib.redirect_stdout(out), \
            mock.patch.object(sys, "argv",
                              ["doctor.py"] + list(argv or ["--validate-config"])):
        code = doctor.main()
    return code, out.getvalue()
