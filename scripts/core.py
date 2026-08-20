#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
催办判定引擎（零第三方依赖，仅标准库）。

被 check_followup.py（每日跑）和 doctor.py（自检）共用。

设计原则：
  1. 台账特定内容一律进配置，代码只认「节点 → 字段 → 阈值」这套抽象。
     接新业务线时只加配置文件，不改这里。
  2. 任何"静默"都是 bug。过滤掉多少行、禁用了哪些节点、哪些断言没过，
     全部要出现在报告里。一个悄悄不跑的规则比一个跑错的规则更难发现。
  3. 唯一的持久化写入是 <运行时目录>/followup/state/ 下的本地文件。台账只读。
"""

from __future__ import annotations  # 兼容 Python 3.9（macOS 自带版本）

import calendar
import json
import os
import ssl
import sys
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover —— Windows 尚未列入本轮支持范围
    fcntl = None

import qqdoc
import lark_base
import wecom_doc
from qqdoc import LedgerError


# ── 路径 ──────────────────────────────────────────────────────────────

def hermes_home() -> Path:
    """
    运行时目录：`.env` 与 `followup/{config,state}` 都在它下面。

    优先级：`FOLLOWUP_HOME` > `HERMES_HOME` > 平台默认。

    为什么加 FOLLOWUP_HOME：这个包要能装在 WorkBuddy、launchd、裸 crontab 上，
    而在那些地方让人设一个叫 `HERMES_HOME` 的变量是明确的误导 ——
    他们没装 Hermes，也不该为了跑这个工具去理解 Hermes 是什么。
    `HERMES_HOME` 保留为兼容别名，现有的 Hermes 安装与 cron 一行都不用改。
    """
    env = os.environ.get("FOLLOWUP_HOME") or os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "hermes"
    return Path.home() / ".hermes"


def followup_home() -> Path:
    """包外目录：配置与状态都在这里，hermes skills update 不会碰。"""
    return hermes_home() / "followup"


def config_dir() -> Path:
    return followup_home() / "config"


def state_dir() -> Path:
    return followup_home() / "state"


# 🔴 这两个曾是 import 时求值的模块常量。改成函数是为了让测试能把 HERMES_HOME
#    指向临时目录 —— 常量在 import 那一刻就定死了，测试没法切，只能去碰真实状态目录。
#    「测试不许碰真实状态」是硬要求，所以这里改成惰性求值。


def read_env(key: str) -> str | None:
    """
    从 <运行时目录>/.env 读一个键。**绝不打印值。**

    qqdoc.load_token() 里有一份近似实现且刻意不合并 —— qqdoc 是最底层的取数模块，
    core 依赖它，它不能反过来依赖 core（循环导入）。十几行的重复换掉循环依赖，值。
    """
    env_path = hermes_home() / ".env"
    if not env_path.exists():
        return None
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if line.startswith(f"{key}="):
            val = line.split("=", 1)[1].strip().strip("'\"")
            return val or None
    return None


def load_json(path: Path, what: str) -> dict:
    """
    读一份配置。**四类坏法都要变成人话，不许抛裸 traceback。**

    🔴 最后那条顶层类型检查是本轮补的。合法 JSON 不等于能用：
       顶层写成 `[]` 或 `null` 时语法完全正确，于是这里放行，
       然后下游每一个 `.get()` 都会炸成
       `AttributeError: 'list' object has no attribute 'get'` ——
       错误信息指向的是崩溃现场，不是病根，排查的人得反推半天。
    """
    if not path.exists():
        raise LedgerError(f"{what}不存在：{path}")
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise LedgerError(
            f"{what}不是 UTF-8 编码（{path}）：{e}。"
            f"常见于用记事本按 GBK 另存 —— 请以 UTF-8 重新保存。"
        )
    except OSError as e:
        raise LedgerError(f"{what}读取失败（{path}）：{type(e).__name__}: {e}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise LedgerError(f"{what}不是合法 JSON（{path}）：{e}")
    if not isinstance(data, dict):
        raise LedgerError(
            f"{what}的顶层应该是一个 JSON 对象 {{...}}，"
            f"实际是 {type(data).__name__}（{path}）。"
            f"写成 [] 或 null 时每个字段都读不到，程序会一路跑到崩在别处。"
        )
    return data


def load_configs() -> tuple[dict, dict, dict]:
    ledgers = load_json(config_dir() / "ledgers.json", "台账配置")
    rules = load_json(config_dir() / "rules.json", "规则配置")
    # output.json 缺失时用最保守的默认值：不写提醒。
    # 但**存在却是坏 JSON 时必须报错** —— 静默当成空配置会让企微通道、
    # 主通道声明、告警开关一起消失，表现成「今天很安静」。
    out_path = config_dir() / "output.json"
    output = load_json(out_path, "输出配置") if out_path.exists() else {}
    return ledgers, rules, output


def _type_name(v) -> str:
    return {dict: "对象", list: "数组", str: "字符串", int: "数字",
            float: "数字", bool: "布尔", type(None): "null"}.get(type(v),
                                                                type(v).__name__)


def _validate_source_fields(l: dict, pos: int) -> list[str]:
    """
    每种数据源各自的必填字段。

    🔴 缺字段的下场不是「报错退出」而是**裸 traceback**：
    `lark_base.read_sheet(l["base_token"], ...)` 会抛 KeyError，
    业务看到的是一屏英文堆栈，完全不知道该改哪里。
    """
    errs: list[str] = []
    where = f"台账「{l.get('name') or l.get('id') or pos}」"
    source = l.get("source", "tencent_mcp")

    if source == "tencent_mcp":
        for f in ("file_id", "sheet_id"):
            if not l.get(f):
                errs.append(f"{where}的 source 是 tencent_mcp，缺少 {f}")
    elif source == "lark_cli":
        for f, hint in (("base_token", "飞书多维表格的 app/base token"),
                        ("table_id", "数据表 id，形如 tblXXXXXXXX"),
                        ("profile", "lark-cli 的身份配置名，决定用谁的授权去读")):
            if not l.get(f):
                errs.append(f"{where}的 source 是 lark_cli，缺少 {f}（{hint}）")
        errs.extend(_validate_link_date_fields(l, where))
    elif source == "wecom_doc":
        # 🔴 url 与 sheet_id 必须成对：实测两份不同的企微文档里都有
        #    sheet_id `BB08J2` 的子表 —— sheet_id 只在文档内唯一。
        for f, hint in (("url", "企微在线表格的访问链接"),
                        ("sheet_id", "子表 id；它只在文档内唯一，必须和 url 成对")):
            if not l.get(f):
                errs.append(f"{where}的 source 是 wecom_doc，缺少 {f}（{hint}）")
    else:
        errs.append(
            f"{where}的 source={source!r} 不支持。"
            f"目前实现了 tencent_mcp（腾讯文档）、lark_cli（飞书多维表格）"
            f"和 wecom_doc（企业微信在线表格）。")
    errs.extend(stage_seed_errors(l, where))
    return errs


def _validate_link_date_fields(l: dict, where: str) -> list[str]:
    """
    link_date_fields 把主表上的关联字段换成关联记录里的某个日期。

    结构写错时 `read_sheet` 里 `spec["link_field"]` 直接 KeyError；
    更坏的情形是整段写成对象而不是数组，`for spec in ...` 会去遍历它的键，
    于是 spec 成了字符串、`spec["link_field"]` 报 TypeError —— 两种都是裸堆栈。
    """
    errs: list[str] = []
    ldf = l.get("link_date_fields")
    if ldf is None:
        return errs
    if not isinstance(ldf, list):
        return [f"{where}的 link_date_fields 应该是数组，实际是{_type_name(ldf)}"]
    for i, spec in enumerate(ldf, 1):
        if not isinstance(spec, dict):
            errs.append(f"{where}的 link_date_fields 第 {i} 项应该是对象，"
                        f"实际是{_type_name(spec)}")
            continue
        for f in ("link_field", "child_table_id", "child_date_field"):
            if not spec.get(f):
                errs.append(f"{where}的 link_date_fields 第 {i} 项缺少 {f}")
    return errs


def cross_match_fields(cfg: dict, default_local: str = "项目名称") -> list[dict]:
    """规整 cross_ledger 新旧两种字段配置；无效结构抛出人话 ValueError。"""
    raw = cfg.get("match_fields")
    if raw is None:
        local = cfg.get("match_field") or default_local
        target = cfg.get("target_field") or cfg.get("match_field")
        if not target:
            raise ValueError("既没有 match_fields，也没有 target_field/match_field")
        return [{"local_field": local, "target_field": target,
                 "normalize_map": {}}]
    if not isinstance(raw, list) or not raw:
        raise ValueError("match_fields 必须是非空数组")
    result = []
    for index, spec in enumerate(raw, 1):
        if not isinstance(spec, dict):
            raise ValueError(f"match_fields 第 {index} 项必须是对象")
        local = spec.get("local_field")
        target = spec.get("target_field")
        if not isinstance(local, str) or not local.strip():
            raise ValueError(f"match_fields 第 {index} 项缺少 local_field")
        if not isinstance(target, str) or not target.strip():
            raise ValueError(f"match_fields 第 {index} 项缺少 target_field")
        normalizer = spec.get("normalize_map") or {}
        if not isinstance(normalizer, dict) or any(
                not isinstance(key, str) or not key
                or not isinstance(value, str) or not value
                for key, value in normalizer.items()):
            raise ValueError(
                f"match_fields 第 {index} 项的 normalize_map 必须是非空字符串到非空字符串的对象")
        result.append({"local_field": local.strip(),
                       "target_field": target.strip(),
                       "normalize_map": dict(normalizer)})
    return result


def cross_not_before(cfg: dict) -> dict | None:
    """
    跨台账核对的「时间不可能倒流」约束。没配就返回 None（保持原行为）。

    ═══════════════════════════════════════════════════════════════════
    🔴 业务 2026-08-14 报的真事：某个项目被催「待登记飞书」，
       但它在主台账里明明已经有了 —— **同名两条**，
       一条立项 2026-06-17、一条 2026-08-06，
       而前期台账里这个需求 2026-07-24 才提出来。
       06-17 那条比需求本身还早 37 天，**不可能是它**。

       原来的跨表核对只数命中条数：2 条 → 无法确认 → 继续催。
       逻辑没错，是信息不够。

    这条约束的依据：飞书里的项目是由前期台账推进过去的，
    **它的立项时间不可能早于这个需求被提出的时间**。
    实测当时处于「待登记飞书」的 10 个项目：9 条单命中全部满足该不等式
    （最小富余 +6 天），错误那条是 −37 天 —— 分离得很开，
    所以**不设「容差天数」参数**：少一个旋钮，少一处配错的地方。

    🔴 它只做**排除**，不做挑选，因此只会让判定更保守：
       剩 0 条 → 继续催；剩 >1 条 → 仍算无法确认，继续催。
       万一真记录被误排除（台账日期填错），结果是继续催 —— 吵但安全。
       **它不会造成「静默不催」**，那才是这个项目最怕的失败模式。
    ═══════════════════════════════════════════════════════════════════
    """
    raw = cfg.get("not_before")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("not_before 必须是对象")
    local = raw.get("local_field")
    target = raw.get("target_field")
    for name, value in (("local_field", local), ("target_field", target)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"not_before 缺少 {name}")
    return {"local_field": local.strip(), "target_field": target.strip()}


def scope_after(ledger: dict) -> dict | None:
    """
    责任范围的「日期不早于」约束。没配就返回 None（保持原行为）。

    ═══════════════════════════════════════════════════════════════════
    🔴 业务 2026-08-20 接 AI外贸拓客台账时提出：排除 2026-06 之前登记的
       历史数据 —— 不是随口一说，实测过：不排除的话，2026-06 前、且在
       杭州/深圳范围内的老数据里，39 条会被①客户填表误判成「没填联系方式」、
       11 条会被②确认回报误判成「没立项」，全是早该收尾但台账没清的旧线索。

    与 `not_before`（比较两个字段）不同，这条约束比较的是**一个字段 vs 一个
    写死的日期常量**——语义更像 scope_filters 的「日期版」，所以判定结果
    走同一个 out_of_scope 计数，不是终止、不是暂缓。

    🔴 同样「不能证伪就不排除」：日期取不到（空/解析不出）时**不排除**，
       让它按原有逻辑继续走 —— 宁可多催一条被人发现，也不能让数据质量问题
       悄悄躲进「范围外」，那样连数据质量警告都不会显示。
    ═══════════════════════════════════════════════════════════════════
    """
    raw = ledger.get("scope_after")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("scope_after 必须是对象")
    field = raw.get("field")
    if not isinstance(field, str) or not field.strip():
        raise ValueError("scope_after 缺少 field")
    date_str = raw.get("date")
    if not isinstance(date_str, str) or not date_str.strip():
        raise ValueError("scope_after 缺少 date")
    try:
        cutoff = date.fromisoformat(date_str.strip())
    except ValueError:
        raise ValueError(
            f"scope_after.date 不是合法日期（要 YYYY-MM-DD）：{date_str!r}")
    return {"field": field.strip(), "date": cutoff}


def _validate_cross_refs(l: dict, rules: dict, known_ids: set) -> list[str]:
    """cross_ledger 指向的台账必须真实存在（字段是否存在要等读到表才知道）。"""
    errs: list[str] = []
    rset = (rules.get("rulesets") or {}).get(l.get("ruleset"))
    if not isinstance(rset, dict):
        return errs
    for n in rset.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        cross = n.get("cross_ledger")
        if not cross:
            continue
        node = n.get("name") or n.get("id") or "?"
        if not isinstance(cross, dict):
            errs.append(f"节点「{node}」的 cross_ledger 应该是对象，"
                        f"实际是{_type_name(cross)}")
            continue
        target = cross.get("ledger_id")
        if not target:
            errs.append(f"节点「{node}」的 cross_ledger 缺少 ledger_id")
        elif target not in known_ids:
            errs.append(
                f"节点「{node}」的 cross_ledger 指向 ledger_id={target!r}，"
                f"但 ledgers.json 里没有这个 id（现有：{sorted(known_ids)}）。"
                f"跨台账核对查不到目标台账时这个节点会永远催下去。")
        try:
            cross_match_fields(cross, l.get("name_field", "项目名称"))
            # not_before 配错（比如只写了一半）不能等到次日 9:00 才发现 ——
            # 它会让整条约束静默失效，而表现只是「怎么还在催」。
            cross_not_before(cross)
        except ValueError as exc:
            errs.append(f"节点「{node}」的 cross_ledger 配置无效：{exc}")
    return errs


def validate_configs(ledgers: dict, rules: dict, output: dict) -> list[str]:
    """
    顶层对了不代表能跑 —— 关键字段的**类型**也得对。返回人话错误列表（空 = 通过）。

    load_json 只保证「顶层是对象」。但 `"ledgers": {}`（该是数组）、
    `"nodes": "收资"`（该是数组）、`"threshold": 7`（该是对象）这些同样
    会在下游炸成 AttributeError / TypeError，而且照样是「合法 JSON」。

    check_followup 与 doctor **共用这一份**，不各写一遍 —— 否则迟早只有一边挡得住。
    """
    errs: list[str] = []

    # ── ledgers.json ──
    lst = ledgers.get("ledgers")
    if lst is None:
        errs.append("ledgers.json 缺少顶层字段 ledgers")
    elif not isinstance(lst, list):
        errs.append(f"ledgers.json 的 ledgers 应该是数组，实际是{_type_name(lst)}")
    else:
        seen_ids: dict = {}
        for i, l in enumerate(lst):
            if not isinstance(l, dict):
                errs.append(f"ledgers.json 的第 {i + 1} 个台账应该是对象，"
                            f"实际是{_type_name(l)}")
                continue
            lid = l.get("id")
            if not lid:
                errs.append(f"ledgers.json 的第 {i + 1} 个台账缺少 id"
                            f"（状态文件名要用它，不能为空）")
            elif lid in seen_ids:
                # 🔴 重复的 id 不会报错，只会让两份台账共用同一套状态文件：
                # stage_entered / followup_state / 快照全部串台，
                # 一份台账的「已通知」把另一份静音掉，而总数照样对得上。
                errs.append(
                    f"ledgers.json 里有两份台账都叫 id={lid!r}"
                    f"（第 {seen_ids[lid]} 个和第 {i + 1} 个）。"
                    f"id 是状态文件的键，重复会让两份台账共用催办状态、互相静音。")
            else:
                seen_ids[lid] = i + 1
            errs.extend(_validate_source_fields(l, i + 1))
            # scope_filters 走的是同一个 match_condition，因此同一批写法在这里
            # 也会静默失效 —— 而且后果更大：`in` 缺 values 恒假会把**整张台账**
            # 过滤光，表现只是「今天没有要催的」。运行时虽有「整表被过滤光」的
            # 告警兜底，但那要等到次日 9:00；能离线查出来就不该留到线上。
            errs.extend(condition_errors(
                l.get("scope_filters"),
                f"台账「{l.get('name') or l.get('id') or i + 1}」的 scope_filters"))
            # scope_after 配错（比如日期格式不对）不能等到次日 9:00 才发现 ——
            # 它跟 scope_filters 一样，一旦失效就静默变成「今天没有要催的」。
            try:
                scope_after(l)
            except ValueError as exc:
                errs.append(
                    f"台账「{l.get('name') or l.get('id') or i + 1}」的 scope_after 配置无效：{exc}")
            # key_tiebreakers 配错（数字、对象、混合数组这类非法值）原来会被
            # 静默过滤成空列表，消歧功能悄悄不生效，等真撞车了才暴露成
            # 主键重复的致命错，那时完全看不出根因在这里。
            try:
                key_tiebreakers(l)
            except ValueError as exc:
                errs.append(
                    f"台账「{l.get('name') or l.get('id') or i + 1}」的 "
                    f"key_tiebreakers 配置无效：{exc}")

        # cross_ledger 的引用必须指向真实存在的台账。指错了只有跑到那个节点
        # 才炸，而那可能是几天后的事；能离线查出来就不该留到线上。
        known = set(seen_ids)
        for l in lst:
            if not isinstance(l, dict):
                continue
            errs.extend(_validate_cross_refs(l, rules, known))

    # ── rules.json ──
    errs.extend(spec_watch_errors(rules))
    rs = rules.get("rulesets")
    if rs is None:
        errs.append("rules.json 缺少顶层字段 rulesets")
    elif not isinstance(rs, dict):
        errs.append(f"rules.json 的 rulesets 应该是对象，实际是{_type_name(rs)}")
    else:
        for rname, rset in rs.items():
            if not isinstance(rset, dict):
                errs.append(f"规则集 {rname!r} 应该是对象，实际是{_type_name(rset)}")
                continue
            nodes = rset.get("nodes")
            if not isinstance(nodes, list):
                errs.append(f"规则集 {rname!r} 的 nodes 应该是数组，"
                            f"实际是{_type_name(nodes)}")
                continue
            for j, n in enumerate(nodes):
                where = f"规则集 {rname!r} 的第 {j + 1} 个节点"
                if not isinstance(n, dict):
                    errs.append(f"{where}应该是对象，实际是{_type_name(n)}")
                    continue
                where = f"节点「{n.get('name') or n.get('id') or j + 1}」"
                clock = n.get("clock") or {}
                fallback = clock.get("fallback") if isinstance(clock, dict) else None
                if fallback is not None:
                    valid = (
                        isinstance(fallback, str) and bool(fallback.strip())
                    ) or (
                        isinstance(fallback, list) and bool(fallback)
                        and all(isinstance(value, str) and bool(value.strip())
                                for value in fallback)
                    )
                    if not valid:
                        errs.append(
                            f"{where}的 clock.fallback 必须是非空字符串或非空字符串数组，"
                            f"实际是 {fallback!r}")
                # 复提醒节律：只对启用的节点查（未启用的节点允许字段不全）。
                # 🔴 这一道是**离线**的 —— 2026-08-10 之前 repeat 只在
                #    assert_sheet 里查，而那要真读到台账才跑，于是
                #    `doctor --validate-config` 对着一个会炸的配置报「全部通过」。
                #    repeat 不是对象时也要走进去 —— 它自己会报「必须是对象」，
                #    在这里先按类型筛掉等于把最常见的手滑放行到运行时。
                # 🔴 `enabled` 必须是真布尔。字符串 "false" 是真值 ——
                #    想停用却写成 "false" 会把节点静默开着，
                #    而配置读起来完全像是已经停用了。
                en = n.get("enabled")
                if en is not None and not isinstance(en, bool):
                    errs.append(
                        f"{where}的 enabled 必须是 true 或 false（不带引号），"
                        f"实际是 {en!r}。"
                        f"（字符串 \"false\" 在程序里是**真**，会把这个节点静默开着）")
                if n.get("enabled") is True:
                    errs.extend(repeat_errors(n.get("repeat"), where))
                    # 启用的节点必须有非空 when：判定用的是
                    # `conds and all(...)`，空数组会让这一条恒假、节点永不命中。
                    # 「不设条件就匹配所有行」听起来合理，但实现不是那样，
                    # 而两者的区别不会报错，只表现为这个节点从不产生催办。
                    conds = n.get("when")
                    if not conds:
                        errs.append(
                            f"{where}已启用但没有 when 条件。"
                            f"空条件不等于「匹配所有行」——它恒假，"
                            f"这个节点永远不会命中，也不会有任何报错。"
                            f"若本意是停用，请写 enabled: false")
                    errs.extend(condition_errors(conds, where))
                thr = n.get("threshold")
                if thr is None:
                    continue          # 未启用的节点可以没有阈值，doctor 另有检查
                if not isinstance(thr, dict):
                    errs.append(f"{where}的 threshold 应该是对象，"
                                f"实际是{_type_name(thr)}")
                    continue
                unit_key = "workdays" if "workdays" in thr else "days"
                try:
                    int(thr.get(unit_key))
                except (TypeError, ValueError):
                    errs.append(f"{where}的 threshold.{unit_key} 不是数字："
                                f"{thr.get(unit_key)!r}")
                b = thr.get("boundary", "after")
                if b not in ("on", "after"):
                    errs.append(f"{where}的 threshold.boundary＝{b!r} 不合法，"
                                f"只能是 on（满 N 天当天提醒）或 after（第 N+1 天）")

    # ── output.json ──
    # 这几段是「若存在则必须是对象」。写成字符串/数组时下游的 .get() 同样会炸。
    for seg in ("notify", "alert", "wecom_webhook", "reminders",
                "terminal_hint", "stdout", "watchdog"):
        v = output.get(seg)
        if v is not None and not isinstance(v, dict):
            errs.append(f"output.json 的 {seg} 应该是对象，实际是{_type_name(v)}")

    # 告警超时。写错的后果比一般配置更重 —— 它只在**已经出事**时被读到，
    # 那一刻裸崩等于「有故障」升级成「连故障是什么都说不出来」。
    # check_followup._alert_timeout() 还有一道运行时兜底，两道都要有。
    alert_seg = output.get("alert")
    if isinstance(alert_seg, dict):
        raw = alert_seg.get("timeout_seconds")
        if raw is not None and raw != "":
            try:
                if int(raw) <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                errs.append(
                    f"output.json 的 alert.timeout_seconds＝{raw!r} 不是正整数。"
                    "填秒数，比如 30。"
                )

    return errs


def reminders_write_enabled(output: dict) -> bool:
    """
    写入开关的唯一读取入口。

    默认 false 是硬约束：配置缺失、键缺失、值不是 True，全部按 false 走。
    绝不"默认开启"。
    """
    return output.get("reminders", {}).get("write") is True


# ── 状态文件 ──────────────────────────────────────────────────────────

def _state_path(name: str) -> Path:
    return state_dir() / name


# ── 只读闸门 ──────────────────────────────────────────────────────────
# 🔴 这是**第二道防线**，不是唯一那道。
#
#    第一道在 check_followup.main()：参数一解析完就算出 real_run，
#    每条成功与失败路径都显式带着它走。那道让读代码的人一眼看懂什么时候会写。
#
#    这一道是兜底：将来谁在 core 里加了一个新的写入点、忘了判断 real_run，
#    它自动被挡住。两道都要 —— 只有第一道，靠的是每个人都记得判断；
#    只有第二道，读代码的人看不出调用点的意图。

_READ_ONLY = False

# 只读模式下被挡下来的写入。测试靠它证明「是被闸门挡住的」，
# 而不是「恰好没走到那一步」—— 后者随时会因为一次重构变回去。
BLOCKED_WRITES: list[str] = []


def set_read_only(on: bool) -> None:
    """由 check_followup 在参数解析完之后调用一次，之后不再改。"""
    global _READ_ONLY
    _READ_ONLY = bool(on)


def is_read_only() -> bool:
    return _READ_ONLY


def _block(what: str) -> bool:
    """只读模式下拦下一次写入并登记。返回 True 表示已拦下。"""
    if not _READ_ONLY:
        return False
    BLOCKED_WRITES.append(what)
    return True


# 本次运行中发现的状态文件损坏。调用方（check_followup / doctor）要把它
# 报进清单、记进 health、并触发告警 —— 只打一行 stderr 是不够的。
#
# 元素形如 {"name", "path", "message"}。用 dict 而不是纯字符串，
# 是为了让 quarantine_damaged() 拿得到路径。
STATE_DAMAGE: list[dict] = []


def read_state(name: str) -> dict:
    """
    读状态文件。**纯读 —— 损坏时只登记，不改名。**

    🔴 改名是「修复」，属于写入。旧实现把它塞在这里，于是
       `--dry-run` 碰上一个坏掉的 stage_entered.json 就会把它改名 ——
       一个用来「看看情况」的开关，动了现场。排查的人下次再跑，
       文件已经不在原处了。

    修复动作拆到 quarantine_damaged()，只在真实运行时调用。

    为什么损坏必须告警：stage_entered.json 坏掉 = 全部项目按「最新进展日期」
    重新初始化，停滞天数集体失真。这是重大降级，不能只在 stderr 里飘一行。
    文件不存在则属正常（首次运行），不登记。
    """
    p = _state_path(name)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        reason = f"损坏（{type(e).__name__}）"
    else:
        if isinstance(data, dict):
            return data
        reason = "结构异常（顶层不是对象）"

    msg = f"状态文件 {name} {reason}，本次按空处理"
    STATE_DAMAGE.append({"name": name, "path": p, "message": msg})
    print(f"⚠️  {msg}", file=sys.stderr)
    return {}


def quarantine_damaged() -> list[str]:
    """
    把本次登记的损坏文件改名保留（`*.corrupt.<时间戳>`）。**只在真实运行时调用。**

    改名而不是删除：坏文件是排查时唯一的线索，而且「只增不删」是本项目的硬规矩。
    返回实际保留下来的文件名，供写进健康记录的恢复事件。
    """
    if _block("quarantine_damaged"):
        return []
    kept: list[str] = []
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for d in STATE_DAMAGE:
        p: Path = d["path"]
        if not p.exists():
            continue
        target = p.with_name(f"{p.name}.corrupt.{stamp}")
        try:
            p.rename(target)
        except OSError as e:
            print(f"⚠️  {p.name} 无法改名保留（{type(e).__name__}），原文件留在原处",
                  file=sys.stderr)
            continue
        kept.append(target.name)
        d["message"] += f"，坏文件已保留为 {target.name}"
    return kept


def ensure_state_dir() -> None:
    """
    启动前确认状态目录可写。

    不做这一步的话，整轮判定跑完、企微都推出去了，才在写状态时炸出裸 traceback ——
    那时消息已经发了，状态却没落地，下次会重推。宁可开跑前就失败。
    """
    d = state_dir()
    probe = d / f".write_probe.{uuid.uuid4().hex}"
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        raise LedgerError(f"状态目录不可写：{d}（{type(e).__name__}: {e}）")
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


def write_state(name: str, data: dict) -> None:
    if _block(f"write_state({name})"):
        return
    state_dir().mkdir(parents=True, exist_ok=True)
    p = _state_path(name)
    tmp = p.with_name(f"{p.name}.tmp.{uuid.uuid4().hex}")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)  # 原子替换，避免写一半被中断留下坏文件
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


# ── 健康记录 ──────────────────────────────────────────────────────────
# 它抓的是别的机制都抓不到的那类失败：**根本没跑**。
# 关机、休眠、gateway 没起来、cron 被误删 —— 这些连 stderr 都不会产生，
# 只有「上次成功是什么时候」这个持久化的时间戳发现得了。

HEALTH_FILE = "health.json"


def read_health() -> dict:
    return read_state(HEALTH_FILE)


def update_health(**fields) -> None:
    """更新健康记录。失败只打 stderr，绝不让它影响主流程的成败判定。"""
    if _block("update_health"):
        return
    try:
        h = read_health()
        h.update(fields)
        write_state(HEALTH_FILE, h)
    except Exception as e:  # noqa: BLE001 —— 健康记录写不了不该拖垮催办
        print(f"⚠️ 健康记录写入失败（不影响判定）：{type(e).__name__}: {e}",
              file=sys.stderr)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def runtime_fingerprint() -> dict:
    """
    「这一趟是哪个 Python 在跑」。**doctor 与主流程共用这一份**（坑 #12）。

    ═══════════════════════════════════════════════════════════════════
    🔴 2026-08-14 这台机器上同时存在三个 Python，SSL 后端各不相同：

        /usr/bin/python3                        3.9.6   LibreSSL 2.8.3
        /opt/homebrew/bin/python3               3.14.6  OpenSSL 3.6.3
        ~/.hermes/hermes-agent/venv/bin/python  3.11.15 OpenSSL 3.5.7  ← cron 跑的

    LibreSSL 2.8.3 根本不支持 TLS 1.3，另两个支持。于是同一份代码、同一个
    网络，在不同解释器下表现完全相反 —— 排查那天我自己敲了裸 `python3`
    落到 Homebrew 那个，把「用错解释器」和「真故障」混在一起，白花近一小时。

    所以真实运行把这个指纹记进 health.json，`doctor` 拿自己的跟它比：
    对不上就明说「你现在这个不是定时任务跑的那个」。
    **不写死路径**（那只在这台 Hermes 上成立，业务电脑与 WorkBuddy 都不同），
    靠比对，任何机器上都成立。
    ═══════════════════════════════════════════════════════════════════
    """
    return {
        "executable": sys.executable,
        "python": sys.version.split()[0],
        # 没编进 SSL 的 Python 极罕见，但这里是诊断代码，不许自己裸崩
        "ssl": getattr(ssl, "OPENSSL_VERSION", "未知（该 Python 没有 ssl 模块）"),
    }


# ── 需求文档变更探测 ──────────────────────────────────────────────────
#
# 业务每天推送前会对着《跟进精灵需求文档》人工核一遍「逻辑有没有变」。
# 这一段把「有没有变」自动化 —— **只回答变没变，不回答变了什么**。
# 价值在于：文档没变的那些天，那次人工核对完全可以不做。
#
# 🔴 它替代不了「读懂改了什么」。rc8 那次（哨兵④发货节律从每周三改成
#    每周一/周四，配置没跟上，业务周一来问「怎么不提醒」）真正花时间的是
#    读懂，但**代价全在没发现** —— 已经晚了几周。这一段解决的正是那一半。
#
# 取指纹用的是 manage.query_file_info，**本来就在只读白名单里**
# （只读性核对一直在用它）。所以这件事零新增能力、零 token、零新白名单项。

SPEC_WATCH_FILE = "spec_watch.json"

# 指纹里参与比对的字段。title 也算：文档被改名同样值得看一眼。
_FP_KEYS = ("title", "last_modify_name", "last_modify_time")


def spec_watch_errors(rules: dict) -> list[str]:
    """离线校验 rules.json 的 spec_watch 段。返回人话错误列表。"""
    entries = rules.get("spec_watch")
    if entries is None:
        return []
    if not isinstance(entries, list):
        return [f"rules.json 的 spec_watch 应该是数组，实际是{_type_name(entries)}"]
    errs: list[str] = []
    for i, e in enumerate(entries):
        at = f"rules.json 的第 {i + 1} 份 spec_watch"
        if not isinstance(e, dict):
            errs.append(f"{at}应该是对象，实际是{_type_name(e)}")
            continue
        fid = e.get("file_id")
        if not isinstance(fid, str) or not fid.strip():
            errs.append(f"{at}缺少 file_id"
                        f"（腾讯文档链接 docs.qq.com/doc/<这一段>）")
        if not e.get("name"):
            errs.append(f"{at}缺少 name —— 提示里要说清是哪份文档被改了")
    return errs


def _fingerprint_of(record) -> tuple:
    if not isinstance(record, dict):
        return ()
    return tuple(record.get(k) for k in _FP_KEYS)


def spec_watch_scan(entries: list, baseline: dict, fetch) -> tuple[list, dict]:
    """
    比对每份需求文档的指纹。返回（提示列表, 要写回的基线）。

    `fetch(file_id) -> dict` 注入进来，测试不必联网。

    ═══════════════════════════════════════════════════════════════════
    三条刻意的设计：

    1. **首次没有基线时只记录，不提示。** 一上来就报一条谁也没法处理的
       警报，只会教人忽略它。

    2. **变更提示持续到人工确认为止，不是只响一天。** 基线只有
       `--ack-spec` 才更新。这跟业务口径②「存量不静音」是同一条路子：
       噪声由清账消化，不由系统静音消化。只响一天的提示 == 那天没看到
       就永久错过，正好回到这个项目一路在消灭的形状。

    3. **取指纹失败也出提示，但不改基线、不影响退出码。** 需求文档读不到
       不该让当天的催办失败（它跟催办判定毫无关系），但也不许静默 ——
       否则「文档一直没变」和「一直没查成」在输出上长得一模一样。
    ═══════════════════════════════════════════════════════════════════
    """
    notices: list[str] = []
    new_baseline = dict(baseline or {})

    for e in entries or []:
        fid, name = e.get("file_id"), e.get("name") or e.get("file_id")
        try:
            now = fetch(fid)
        except Exception as ex:            # noqa: BLE001 —— 什么原因都要说出来
            notices.append(
                f"需求文档《{name}》这次没核对成：{type(ex).__name__}: {ex}。"
                f"这**不是**「文档没变」，是没查成。")
            continue

        old = new_baseline.get(fid)
        if old is None:
            # 首次见到：静静记下当前状态当基线。
            new_baseline[fid] = {k: now.get(k) for k in _FP_KEYS}
            continue

        if _fingerprint_of(old) == _fingerprint_of(now):
            continue

        who = now.get("last_modify_name") or "未知"
        when = _fmt_modify_time(now.get("last_modify_time"))
        since = _fmt_modify_time(old.get("last_modify_time"))
        notices.append(
            f"需求文档《{name}》被「{who}」改过了（{when}，上次核对时是 {since}）。"
            f"催办规则可能要跟着改 —— 核对完跑一次 "
            f"`check_followup.py --ack-spec` 消除这条提示。"
            f"（只说变没变，变了什么要人自己读）")

    return notices, new_baseline


def _fmt_modify_time(v) -> str:
    """腾讯返回的是毫秒时间戳字符串。转不出来就原样给，别为格式化炸掉。"""
    try:
        return datetime.fromtimestamp(int(v) / 1000).astimezone() \
            .strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return str(v)


def spec_watch_baseline() -> dict:
    b = read_state(SPEC_WATCH_FILE)
    return b if isinstance(b, dict) else {}


# ── 运行锁 ────────────────────────────────────────────────────────────

LOCK_FILE = ".run.lock"
LOCK_GUARD_FILE = ".run.lock.guard"
LOCK_STALE_SECONDS = 30 * 60        # 超过它才考虑「是不是卡住了」
LOCK_ABANDON_SECONDS = 6 * 60 * 60  # 进程还活着也当它僵死的绝对上限
EMPTY_LOCK_GRACE_SECONDS = 30       # 兼容升级时仍在运行的旧版两段式建锁

# 抢锁标记多久算「残留」。
#
# 🔴 正常抢占从建标记到 unlink 之间只有几个系统调用（读锁、写临时文件、
#    os.replace），毫秒级。120 秒之后标记还在，只可能是那个进程死了。
#    取 120 而不是更短，是给极端慢的磁盘留余量 —— 判早了会把正在抢锁的
#    进程踢掉，那正是标记要防的事。
STEAL_MARKER_STALE_SECONDS = 120


class LockBusy(Exception):
    """另一次运行正在进行。不是故障 —— 调用方应安静退出（退出码 0）。"""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但不是我的进程
    except (OSError, TypeError, ValueError):
        return False
    return True


def _read_lock(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— 锁文件本身坏了，按无法解析处理
        return {}
    return d if isinstance(d, dict) else {}


def _create_lock_exclusive(p: Path, payload: str) -> bool:
    """
    只有在锁不存在时才创建成功。**这是整套并发保护唯一的原子操作。**

    先完整写同目录临时文件，再用硬链接发布到目标路径。`os.link` 不覆盖已存在
    的目标，因此多个进程同时调用时仍然只有一个成功；目标一旦可见就已有完整内容。
    """
    tmp = p.with_name(f".{p.name}.create.{uuid.uuid4().hex}")
    try:
        fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            data = memoryview(payload.encode("utf-8"))
            while data:
                written = os.write(fd, data)
                if written <= 0:
                    raise OSError("运行锁内容没有完整写入")
                data = data[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(str(tmp), str(p))
        except FileExistsError:
            return False
        return True
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


@contextmanager
def _lock_metadata_guard():
    """串行化运行锁的检查、抢占和释放；进程退出时由内核自动解锁。"""
    state_dir().mkdir(parents=True, exist_ok=True)
    guard = _state_path(LOCK_GUARD_FILE)
    fd = os.open(str(guard), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _claim_steal_right(marker: Path, payload: str) -> None:
    """
    取「抢这把陈旧锁」的资格。拿不到就 LockBusy。

    ═══════════════════════════════════════════════════════════════════
    🔴 这个函数存在的唯一理由：**标记残留会把运行永久堵死。**

    0.3.0-rc1 的实现是「建不上标记就 LockBusy」，看起来对，实际有个
    致命缺口 —— 进程在建完标记之后、`finally` 的 unlink 之前被 SIGKILL
    （关机、强杀、OOM）：

        标记 .run.lock.steal.<T> 留在盘上
          → 锁文件没被替换，holder token 仍是 T
            → 下次运行算出**同一个**标记名 → 建不上 → LockBusy
              → 每天如此，**永远**

    而当时的 `_sweep_steal_markers()` 只在**抢到标记之后**的 finally 里跑，
    正好够不着自己造成的这个死锁。实测三次调用三次 LockBusy。

    表现是「每天静默跳过」—— 和「每天没有要催的」长得一模一样，
    这正是整个项目从头到尾在防的那种失败。
    ═══════════════════════════════════════════════════════════════════
    """
    if _create_lock_exclusive(marker, payload):
        return

    # 建不上。是别人正在抢（该让路），还是上次抢锁的进程死在半路了（该清理）？
    # 用 mtime 分辨 —— 这是标记留在盘上的时长。
    try:
        age = datetime.now().timestamp() - marker.stat().st_mtime
    except OSError:
        # stat 失败说明标记刚被别人清掉，再试一次
        if _create_lock_exclusive(marker, payload):
            return
        raise LockBusy("另一个进程正在抢这把陈旧锁，本次让路")

    if age < STEAL_MARKER_STALE_SECONDS:
        raise LockBusy("另一个进程正在抢这把陈旧锁，本次让路")

    # 残留。清掉后**只重试一次** —— 再失败说明有别的进程刚抢先建上了，
    # 那是正常竞争，让路即可，不能在这里循环等待。
    try:
        marker.unlink()
    except OSError:
        pass
    if _create_lock_exclusive(marker, payload):
        return
    raise LockBusy("另一个进程正在抢这把陈旧锁，本次让路")


def acquire_lock() -> tuple[Path, str, str | None]:
    """在系统级元数据保护下取得运行锁。"""
    if _block("acquire_lock"):
        raise LedgerError("只读模式不该加锁 —— 调用方应先判断 real_run")
    with _lock_metadata_guard():
        return _acquire_lock_under_guard()


def _acquire_lock_under_guard() -> tuple[Path, str, str | None]:
    """
    取运行锁。返回 `(锁路径, owner_token, 夺锁告警或 None)`。

    ═══════════════════════════════════════════════════════════════════
    🔴 旧实现三处并发缺陷，全在这里修掉：

      ① 夺陈旧锁用的是 `p.write_text()` —— **不是原子操作**。
         两个进程同时判定「锁已陈旧」，会双双写入、双双以为自己持锁，
         然后并行跑判定、并行写状态、并行推送。
         改法：先 unlink 再走一次 O_EXCL 重建 —— unlink 谁成功都无所谓
         （另一个拿到 ENOENT，无害），但**重建只有一个能赢**。

      ② 锁里没有身份标识，释放时只按路径删。A 的锁被 B 夺走后，
         A 跑完照样把 B 的锁删掉 —— 于是 C 进来又拿到锁，**并发保护归零**。
         改法：每次持锁生成一个不可复用的 uuid token，释放前核对。

      ③ 「存活但超 30 分钟」直接夺锁。那个进程还活着，夺了就是两个进程
         同时写状态 —— 正是锁要防的事。
         改法见下表：活着就不夺，只告警 + 让路；超 6 小时才当它僵死。
    ═══════════════════════════════════════════════════════════════════

    持锁判定：

      | 持锁进程 | 已持锁 | 处理 |
      |---|---|---|
      | 存活 | < 30 min | 安静跳过（LockBusy，退出码 0，无告警） |
      | 存活 | 30min–6h | **跳过 + 告警**，不夺活着的锁 |
      | 存活 | > 6h     | 夺锁 + 告警（几乎肯定僵死） |
      | 已死 | 任意      | 夺锁 + 告警 |
      | 锁文件坏 | —     | 夺锁 + 告警 |

    **必须能夺回** —— 否则一次卡死会让之后每天都静默跳过，
    而「每天静默跳过」和「每天没有超时单」长得一模一样。
    """
    state_dir().mkdir(parents=True, exist_ok=True)

    # 🔴 先扫一遍残留标记，**无论后面走哪条路**。
    #    旧实现把它放在抢锁成功后的 finally 里，于是「标记残留把自己堵死」
    #    这个场景永远等不到清理 —— 清理代码在死锁的另一侧。
    _sweep_steal_markers()

    p = _state_path(LOCK_FILE)
    token = uuid.uuid4().hex
    payload = json.dumps(
        {"pid": os.getpid(), "started_at": now_iso(), "token": token},
        ensure_ascii=False,
    )

    # ── 抢锁：O_EXCL 拿不到就看看是谁占着 ──
    #
    # 🔴 这个循环不是为了「多试几次碰运气」，是补一个真实的竞态窗口：
    #    O_EXCL 失败之后、_read_lock 之前，持锁者可能已经释放。那时
    #    holder 是空的 → token 是 None → 走到下面的夺锁路径时，
    #    「锁的 token 有没有变」这个校验会拿 None 和 None 比，**必然相等**，
    #    于是两个进程双双通过校验、双双 os.replace，**同时拿到锁**。
    #
    #    实测抓到过：四进程 15 轮里的第 11 轮出现 2 个 ok
    #    （在装出来的副本里跑、机器负载高的时候才够宽）。
    #    锁不存在时正确的动作是**重新走一次 O_EXCL**，而不是去夺一把
    #    根本不存在的锁。
    holder: dict = {}
    holder_raw = b""
    for _ in range(3):
        if _create_lock_exclusive(p, payload):
            return p, token, None
        try:
            holder_raw = p.read_bytes()
        except OSError:
            # 锁刚被释放，文件已经没了 —— 重新竞争，不走夺锁路径
            continue
        if not holder_raw.strip():
            # 🔴 空文件 = 创建者正处在「已建文件、还没写完」的中间态。
            #    _create_lock_exclusive 是 os.open(O_EXCL) 之后才 os.write，
            #    这两个系统调用之间文件是 0 字节的。
            #
            #    空内容 parse 出来是 {}，和「锁文件损坏」长得一模一样，
            #    于是会被当成陈旧锁夺走 —— 而那把锁的主人正在写它。
            #    结果：创建者以为自己持锁（它的 fd 指向已被替换掉的 inode），
            #    夺锁者也以为自己持锁，**两个进程同时在跑**。
            #
            # 新版本原子发布，不再产生空目标；但升级时可能仍有旧版进程正在用
            # 两段式建锁。新鲜空锁先让路，超过宽限期则视为创建者已死并恢复。
            try:
                empty_age = datetime.now().timestamp() - p.stat().st_mtime
            except OSError:
                continue
            if empty_age < EMPTY_LOCK_GRACE_SECONDS:
                raise LockBusy("另一个进程正在创建运行锁，本次让路")
            holder = {}
            break
        holder = _read_lock(p)
        break
    else:
        raise LockBusy("运行锁反复在竞争中变动，本次让路")

    pid = holder.get("pid")
    started = parse_dt(holder.get("started_at"))
    alive = _pid_alive(pid) if isinstance(pid, int) else False
    age = (datetime.now().astimezone() - started).total_seconds() if started else None

    if alive and age is not None and age < LOCK_STALE_SECONDS:
        raise LockBusy(
            f"另一次运行正在进行（pid {pid}，started {holder.get('started_at')}）"
        )

    if alive and (age is None or age < LOCK_ABANDON_SECONDS):
        # 活着但跑太久。**不夺** —— 夺了就是两个进程同时写状态。
        # 但也不能闷声跳过：真卡死的话每天都会跳，而那和「每天没事」一样安静。
        mins = int(age // 60) if age is not None else -1
        raise LockBusy(
            f"上一次运行已经跑了 {mins} 分钟还没结束（pid {pid}），本次让路不抢。"
            f"⚠️ 它可能卡在网络请求上；超过 "
            f"{LOCK_ABANDON_SECONDS // 3600} 小时后才会被当作僵死强行夺回。",
            )

    why = []
    if not alive:
        why.append(f"持锁进程 {pid} 已不存在")
    elif age is not None:
        why.append(f"已持锁 {int(age // 3600)} 小时，按僵死处理")
    if age is None and holder:
        why.append("锁文件里没有可解析的开始时间")
    if not holder:
        why.append("锁文件为空或内容无法解析")

    # ── 原子夺锁 ──
    # 🔴 「先 unlink 再 O_EXCL 重建」**不够**，这是实测抓出来的：
    #    A 和 B 都通过了「锁已陈旧」的判定 → A unlink、A 建好新锁 →
    #    **B 的 unlink 把 A 的新锁删掉** → B 再建自己的 → 双双得手。
    #    抓到它的是 tests/test_concurrency.py 的四进程 × 15 轮，
    #    同一进程里顺序调两次 acquire_lock 永远测不出来。
    #
    # 所以**抢占动作本身也要互斥**：以「被抢的那把锁的 token」为名建一个
    # O_EXCL 标记文件，谁建成谁才有权替换。抢同一把锁的进程争同一个标记，
    # 而抢占成功后锁的 token 就变了 —— 残留的旧标记再没人会去看，
    # 顶多是垃圾文件（下面顺手清），不会把谁挡死。
    #
    # ⚠️ 标记残留会永久堵死运行，清理逻辑见 _claim_steal_right()。
    marker = _state_path(f"{LOCK_FILE}.steal.{holder.get('token') or 'unknown'}")
    _claim_steal_right(marker, payload)
    try:
        # 拿到抢占权后再确认一次：锁可能在这几行之间已经被换掉了。
        #
        # 🔴 第二道保险：比**原始内容**，不比 token。
        #
        #    比 token 有两个洞：①锁文件损坏时根本没有 token（而损坏的锁
        #    本来就该夺回）；②两边都是 None 时 `!=` 判成相等 —— 那正是
        #    「锁已被释放」那个竞态能同时放两个进程进来的原因。
        #    比字节则两种情况都对：文件没了会抛 OSError，内容变了不相等。
        if p.read_bytes() != holder_raw:
            raise LockBusy("这把陈旧锁刚被另一个进程接管，本次让路")
        tmp = _state_path(f"{LOCK_FILE}.new.{token}")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, p)      # 原子替换，不经过「文件不存在」的窗口
    except OSError as e:
        raise LockBusy(f"无法替换陈旧锁（{type(e).__name__}），本次跳过")
    finally:
        try:
            marker.unlink()
        except OSError:
            pass
        _sweep_steal_markers()

    return p, token, ("夺回陈旧运行锁：" + "；".join(why)
                      + "。上一次运行可能是卡死或被强杀。")


def _sweep_steal_markers() -> None:
    """
    清掉残留的抢占标记。

    🔴 「它们不挡人，只是垃圾」—— 这是 0.3.0-rc1 注释里的原话，**是错的**。
       同一把陈旧锁算出的标记名是固定的（以 holder token 命名），
       残留标记会让下一次抢占永远建不上 → 永久 LockBusy。它挡人，而且挡死。

    所以这个函数现在在 acquire_lock() **入口**跑，不再是收尾清垃圾。
    """
    try:
        for f in state_dir().glob(f"{LOCK_FILE}.steal.*"):
            try:
                if (datetime.now().timestamp() - f.stat().st_mtime
                        > STEAL_MARKER_STALE_SECONDS):
                    f.unlink()
            except OSError:
                pass
    except OSError:
        pass


def release_lock(p: Path, token: str) -> None:
    """
    释放运行锁。**先核对 owner token，绝不删别人的锁。**

    不核对的话：A 的锁被 B 夺走后，A 跑完照样把 B 的锁删掉，
    于是 C 进来又拿到锁 —— 并发保护归零，而且完全无声。
    """
    with _lock_metadata_guard():
        cur = _read_lock(p)
        if not cur:
            return                  # 已经没了或读不出来，不折腾
        if cur.get("token") != token:
            print("⚠️  运行锁已被其他进程接管，本进程不删它（避免误删别人的锁）",
                  file=sys.stderr)
            return
        try:
            p.unlink()
        except OSError:
            pass


def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    return d if d.tzinfo else d.astimezone()


def iso(d: date) -> str:
    return d.isoformat()


def parse_iso(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


# ── 工作日 ────────────────────────────────────────────────────────────

def nodes_using_workdays(rules_cfg: dict) -> list[str]:
    """
    哪些节点的复提醒是按工作日算的。

    doctor 与每日运行共用这一份判断 —— 分头各写一遍迟早漂移，
    而漂移的表现是「doctor 说没问题，实际算错」。
    """
    names: list[str] = []
    for ruleset in (rules_cfg.get("rulesets") or {}).values():
        if not isinstance(ruleset, dict):
            continue
        for node in ruleset.get("nodes") or []:
            if not isinstance(node, dict) or node.get("enabled") is False:
                continue
            if ((node.get("repeat") or {}).get("workdays")
                    or (node.get("threshold") or {}).get("workdays")):
                names.append(str(node.get("stage") or node.get("name") or node.get("id") or "?"))
    return names


class WorkdayCalc:
    """
    工作日计算。本期只有②专家评估的「每2个工作日」复提醒用到。

    节假日表过期时会报警（holiday_warning），不静默退化成只排周末——
    静默降级会让人以为算得准，而实际上国庆期间照常提醒。
    """

    def __init__(self, workday_cfg: dict, holidays_cfg: dict | None,
                 workday_nodes: "tuple[str, ...] | list[str]" = ()):
        self.exclude_weekends = workday_cfg.get("exclude_weekends", True)
        self.exclude_holidays = workday_cfg.get("exclude_holidays", False)
        self.holidays: set[date] = set()
        self.extra_workdays: set[date] = set()
        self.holiday_warning: str | None = None

        if not self.exclude_holidays:
            # 🔴 关掉节假日本身是合法取舍（见模板注释），但**只有在没人依赖它时**
            #    才是合法的。一旦真有节点按「每 N 个工作日」复提醒，业务读到的
            #    「每2个工作日」就和实际算的（只排周末）不是一回事 ——
            #    国庆连休期间它会照常把假期天数算成工作日。
            #
            #    这是装机时最容易漏的一步：模板默认 false、节假日表默认是空的，
            #    而漏拷之后**原本一句提示都没有**。
            if workday_nodes:
                self.holiday_warning = (
                    f"节点「{'、'.join(workday_nodes)}」按工作日复提醒，"
                    f"但 rules.json 的 workday.exclude_holidays 是 false —— "
                    f"实际只排除了周末，法定节假日仍被当作工作日。\n"
                    f"要么把它改成 true 并补上 config/holidays.json，"
                    f"要么把这些节点的复提醒改成自然日（days），别让两边对不上。"
                )
            return
        if not holidays_cfg:
            self.holiday_warning = (
                "规则要求排除法定节假日，但 config/holidays.json 不存在。"
                "工作日计算已退化为仅排除周末。"
            )
            return
        for s in holidays_cfg.get("holidays", []):
            d = parse_iso(s)
            if d:
                self.holidays.add(d)
        for s in holidays_cfg.get("workdays", []):
            d = parse_iso(s)
            if d:
                self.extra_workdays.add(d)
        year = holidays_cfg.get("covers_year")
        if year and date.today().year != year:
            self.holiday_warning = (
                f"节假日表覆盖 {year} 年，当前已是 {date.today().year} 年。"
                f"请更新 config/holidays.json，否则工作日计算不准。"
            )
        elif not holidays_cfg.get("verified"):
            self.holiday_warning = (
                "config/holidays.json 的 verified 仍为 false —— "
                "调休安排（连休日、周末补班日）还没人工核对补齐。"
            )

    def is_workday(self, d: date) -> bool:
        if d in self.extra_workdays:
            return True
        if self.exclude_weekends and d.weekday() >= 5:
            return False
        if d in self.holidays:
            return False
        return True

    def count_between(self, start: date, end: date) -> int:
        """(start, end] 区间内的工作日数。start 当天不计。"""
        if end <= start:
            return 0
        n = 0
        cur = start + timedelta(days=1)
        while cur <= end:
            if self.is_workday(cur):
                n += 1
            cur += timedelta(days=1)
        return n


# ── 条件匹配 ──────────────────────────────────────────────────────────
#
# op 按「要带哪个取值字段」分三组。分组不只是为了好看：离线校验靠它判断
# 一个条件是不是缺了取值，而**缺取值恰恰是不报错的那一类**（见
# condition_errors 的注释）。新增 op 时只改这三个常量，
# match_condition 与离线校验会一起跟上 —— 这是「同一件事不写两份」（已中三次）。

VALUE_OPS = frozenset({"equals", "not_equals", "contains", "not_contains"})
VALUES_OPS = frozenset({"in", "not_in"})
NOARG_OPS = frozenset({"empty", "not_empty"})
CONDITION_OPS = VALUE_OPS | VALUES_OPS | NOARG_OPS


def match_condition(get_text, cond: dict) -> bool:
    """
    对一行数据求一个条件的真假。

    支持的 op 见 CONDITION_OPS：in / not_in / equals / not_equals /
             empty / not_empty / contains / not_contains
    """
    field = cond.get("field")
    op = cond.get("op", "equals")
    val = get_text(field)

    if op == "empty":
        return val == ""
    if op == "not_empty":
        return val != ""
    if op == "in":
        return val in (cond.get("values") or [])
    if op == "not_in":
        return val not in (cond.get("values") or [])
    if op == "equals":
        return val == cond.get("value", "")
    if op == "not_equals":
        return val != cond.get("value", "")
    if op == "contains":
        return cond.get("value", "") in val
    if op == "not_contains":
        # 给"公式字段拼出一整句话，值本身会变"这种场景用（比如飞书的
        # 「项目状态」把停滞天数拼进字符串里）——没法用 in/equals 枚举，
        # 只能按子串判断，而"未包含某关键词"没法用现有 op 表达。
        return cond.get("value", "") not in val
    raise LedgerError(f"配置里出现不支持的 op：{op!r}（字段 {field!r}）。"
                      f"支持的是：{'/'.join(sorted(CONDITION_OPS))}")


def condition_errors(conds, where: str) -> list[str]:
    """
    离线校验一组 when / scope_filters 条件。返回人话错误列表（空 = 通过）。

    ═══════════════════════════════════════════════════════════════════
    🔴 这里挡的四类写法**全都能过 JSON 语法、也全都不报错**，
       只是让那个节点从此不再产生催办 —— 与 rc9 修的 `{"days": -3}` 同一类：
       配置看起来是配过的，实际已经失效。

         {"op": "in"} 漏写 values      → `val in []` **恒假**，该节点永不命中
         {"op": "contains"} 漏写 value → `"" in val` **恒真**，该节点吞掉所有
                                          项目，后面的节点全部轮空
         条件缺 field                   → 取值恒为空串，判据静默失效
         op 写错（contain / eq）        → 运行时抛错退出 1。可见，
                                          但要等到次日 9:00 才知道

       前两类最狠：一条错误日志都不会有，业务只是再也收不到那条提醒。
    ═══════════════════════════════════════════════════════════════════
    """
    errs: list[str] = []
    if conds is None:
        return errs
    if not isinstance(conds, list):
        return [f"{where}的条件必须是数组，实际是{_type_name(conds)}"]

    for i, c in enumerate(conds):
        at = f"{where}的第 {i + 1} 个条件"
        if not isinstance(c, dict):
            errs.append(f"{at}必须是对象，实际是{_type_name(c)}")
            continue

        op = c.get("op", "equals")
        if op not in CONDITION_OPS:
            errs.append(f"{at}的 op={op!r} 不支持，只能是："
                        f"{'/'.join(sorted(CONDITION_OPS))}")
            continue          # op 不认识就没法再判它该带哪个取值字段

        field = c.get("field")
        if not isinstance(field, str) or not field.strip():
            errs.append(f"{at}缺少 field（要判断台账里的哪一列）。"
                        f"缺了不会报错，只会让这一列恒为空串、判据静默失效")

        if op in VALUES_OPS:
            vs = c.get("values")
            if not isinstance(vs, list) or not vs or \
                    not all(isinstance(v, str) for v in vs):
                errs.append(
                    f"{at}的 op={op!r} 必须配一个非空的 values 字符串数组，"
                    f"实际是 {vs!r}。"
                    f"（空的话 `取值 in []` **恒假**，这个条件永远不成立："
                    f"写在节点上是从此一条都不催，写在 scope_filters 上是"
                    f"整张台账被过滤光。两种都不会有任何报错）")
        elif op in VALUE_OPS:
            if "value" not in c:
                errs.append(
                    f"{at}的 op={op!r} 必须配 value。"
                    f"（缺了会当成空串：contains 恒真——这个节点会吞掉所有项目、"
                    f"让后面的节点全部轮空；equals 则只匹配空单元格）")
            elif not isinstance(c["value"], str):
                errs.append(f"{at}的 value 必须是字符串，"
                            f"实际是{_type_name(c['value'])}"
                            f"（台账取值一律按文本比对，数字 7 与文本「7」不相等）")
        else:                 # NOARG_OPS
            extra = [k for k in ("value", "values") if k in c]
            if extra:
                errs.append(
                    f"{at}的 op={op!r} 只判空不空，多写的 {'/'.join(extra)} "
                    f"不会被读取。若本意是「等于某值」请改 op，"
                    f"否则删掉以免下次误读")

    return errs


def referenced_fields(conds: list[dict]) -> set[str]:
    return {c["field"] for c in conds if c.get("field")}


# ── 入口断言 ──────────────────────────────────────────────────────────

class Assertions:
    """
    入口断言的结果。

    fatal 非空 = 必须报错退出，不许继续跑出「今天没有超时单」。
    warnings = 继续跑，但要出现在报告里。
    """

    def __init__(self):
        self.fatal: list[str] = []
        self.warnings: list[str] = []

    @property
    def ok(self) -> bool:
        return not self.fatal


# ── 人工名单完整性检查 ────────────────────────────────────────────────
#
# 三张名单直接决定项目催不催，第四张只生成复核提示。它们全部靠**序号**匹配。
# 序号填错、或业务把某一行删掉后序号被新项目占用，就会永久排除错误的项目 ——
# 而且判定照跑、总数照样对得上，没有任何人会发现。
#
# 唯一的解法是同时比对企业名称：序号是坐标，名称是身份，两者一致才认。

MANUAL_LISTS: list[tuple[str, str, bool]] = [
    # (配置键, 中文名, 不一致时是否致命)
    ("manual_terminal",          "业务确认终止名单", True),
    ("terminal_note_exceptions", "备注语义例外表",   True),
    ("paused",                   "暂缓名单",         True),
    # 灰名单只生成复核提示、不参与催与不催的判断，
    # 为一条提示停掉整个催办不成比例 → 警告即可。
    ("gray_list_for_review",     "灰色底纹复核清单", False),
]


def manual_list_entries(ledger: dict, field: str) -> list[dict]:
    """
    把一张人工名单规整成 [{key, name}, ...]。

    兼容两种写法：对象数组 [{key,name,...}] 与旧的纯字符串数组 ["6","7"]。
    后者没有 name，校验时会提示补齐，但不会因此报错 —— 不能让一次格式升级
    把正在跑的催办打断。
    """
    out: list[dict] = []
    for e in ledger.get(field) or []:
        if isinstance(e, dict):
            out.append({"key": str(e.get("key", "")), "name": (e.get("name") or "").strip()})
        else:
            out.append({"key": str(e), "name": ""})
    return out


def check_manual_lists(sheet: qqdoc.Sheet, ledger: dict) -> Assertions:
    """
    统一校验全部基于序号的人工名单。**一处实现，四张名单共用** ——
    分开写四遍，迟早有一张漏掉新加的检查。

    对每条：
      序号不在台账里            → 致命（灰名单：警告）
      有 name 但与台账当前值不符 → 致命（灰名单：警告）← 序号填错、序号被新项目复用都由这条抓住
      没有 name                 → 警告，提示补齐（无法校验身份）
    """
    a = Assertions()
    kfields = key_fields(ledger)
    try:
        ktiebreakers = key_tiebreakers(ledger)
    except ValueError:
        ktiebreakers = []  # validate_configs 已给出精确错误；这里不许二次裸崩
    key_field = key_label(kfields)
    name_field = ledger.get("name_field", "项目名称")

    # 台账当前的 序号 → 企业名
    key_map = resolve_row_keys(sheet, sheet.data_rows, kfields, ktiebreakers)
    live: dict[str, str] = {}
    for r in sheet.data_rows:
        k = key_map[r]
        n = sheet.text(r, name_field)
        if k and n:
            live[k] = n

    for field, label, fatal in MANUAL_LISTS:
        entries = manual_list_entries(ledger, field)
        if not entries:
            continue
        bucket = a.fatal if fatal else a.warnings
        no_name = []
        for e in entries:
            key, name = e["key"], e["name"]
            actual = live.get(key)
            if actual is None:
                bucket.append(
                    f"{label}里的序号 {key} 在台账里不存在"
                    + (f"（配置写的是「{name}」）" if name else "")
                    + "。该行可能已被删除，或序号填错了。"
                )
                continue
            if not name:
                no_name.append(key)
                continue
            if name != actual:
                bucket.append(
                    f"{label}里的序号 {key} 身份对不上：配置写「{name}」，"
                    f"台账现在是「{actual}」。序号可能被新项目复用了 —— "
                    f"照此运行会把错误的项目永久排除。请先核对再改配置。"
                )
        if no_name:
            a.warnings.append(
                f"{label}有 {len(no_name)} 条只有序号没有企业名"
                f"（序号 {', '.join(no_name[:8])}{' 等' if len(no_name) > 8 else ''}），"
                f"无法校验身份。建议在 ledgers.json 里补 name 字段。"
            )
    return a


def assert_sheet(sheet: qqdoc.Sheet, ledger: dict, ruleset: dict) -> Assertions:
    """
    读到数据后、判定之前的全部断言。

    这些护栏对应方案「对抗式审查发现」里实测复现过的静默失败点。
    """
    a = Assertions()
    kfields = key_fields(ledger)
    try:
        ktiebreakers = key_tiebreakers(ledger)
    except ValueError as exc:
        a.fatal.append(f"key_tiebreakers 配置无效：{exc}")
        ktiebreakers = []
    key_field = key_label(kfields)
    name_field = ledger.get("name_field", "项目名称")

    # ── P0-1 的护栏：表头断言 ──
    # 若读取起点错位，表头行会变成第一条数据行，这里就会缺列名。
    required = list(ledger.get("required_columns") or [])
    missing = [c for c in required if not sheet.has_column(c)]
    if missing:
        a.fatal.append(
            f"表头缺少必需列：{', '.join(missing)}。"
            f"实际表头：{[h for h in sheet.header if h]}。"
            f"可能是台账改了列名，也可能是读取起点错位。"
        )
    if sheet.duplicate_columns:
        a.warnings.append(
            f"表头出现重复列名 {sheet.duplicate_columns}，只取第一个同名列"
        )

    # 配置里引用到的所有字段都必须真实存在（否则判据会静默失效）
    # 主键字段也算 —— 组合键里少写一列的话 row_key 会整个返回空，
    # 表现是「主键读到空值」而不是「配置写错了」，多绕一层。
    referenced: set[str] = set(kfields)
    for node in ruleset.get("nodes", []):
        if node.get("enabled"):
            referenced |= referenced_fields(node.get("when") or [])
            clock = node.get("clock") or {}
            if clock.get("field"):
                referenced.add(clock["field"])
            referenced.update(clock_fallback_fields(clock))
            cross = node.get("cross_ledger") or {}
            if cross:
                try:
                    for spec in cross_match_fields(cross, name_field):
                        referenced.add(spec["local_field"])
                    # 🔴 not_before 的本地列也必须在这里 —— 漏了它，列名写错时
                    #    get_date() 只会返回 None，约束整条**静默失效**、退回纯计数。
                    #    实测过后果（同一份数据，只差列名一个字）：
                    #      写对 → 老项目被排除 → 剩 0 条 → 继续催（正确）
                    #      写错 → 约束失效   → 1 条命中 → 判为已接力、**停催**
                    #    一个错别字就把「正确地继续催」变成「静默不催」，
                    #    而这条约束存在的全部意义就是防这个。
                    #
                    #    只加 local_field：target_field 属于另一张台账，
                    #    已由 _cross_ledger_values 那条致命错管着；
                    #    在本地表上找它会得到一条必然误报的致命错。
                    guard = cross_not_before(cross)
                    if guard:
                        referenced.add(guard["local_field"])
                except ValueError:
                    pass  # validate_configs 已给出精确错误；避免这里二次裸崩
    for f in ledger.get("scope_filters") or []:
        if f.get("field"):
            referenced.add(f["field"])
    try:
        sa = scope_after(ledger)
        if sa:
            referenced.add(sa["field"])
    except ValueError:
        pass  # validate_configs 已给出精确错误；避免这里二次裸崩
    for t in ledger.get("terminal_states") or []:
        if t.get("field"):
            referenced.add(t["field"])
    tc = ledger.get("terminal_column") or {}
    if tc.get("enabled") and tc.get("field"):
        referenced.add(tc["field"])
    # 🔴 key_tiebreakers 字段名打错，原来不会被任何检查抓住——不像
    # key_field 那样「解析出空列表 → 主键读到空值 → 致命错」会自动暴露，
    # tiebreaker 打错字段名只会让消歧功能悄悄失效，要等到真的撞车才
    # 会看出来。纳入 referenced，字段名打错就在这里当场报错。
    referenced.update(ktiebreakers)

    ghost = sorted(f for f in referenced if not sheet.has_column(f))
    if ghost:
        # 终止判据引用的列消失了，会让终止项重新开始被催——必须报错，不许静默跳过
        a.fatal.append(
            f"配置引用了台账里不存在的列：{', '.join(ghost)}。"
            f"若这些列已被业务删除，请同步改 config/ledgers.json 或 rules.json。"
        )

    if a.fatal:
        return a  # 列都不对，后面的断言没意义

    # ── P0-2 的护栏：主键断言 ──
    # 「序号」是 NUMBER 型，用 string_value 读会得到 101 个空串，
    # 灰色名单/例外表/state 的键全部失效，而输出总数看起来正常。
    #
    # 🔴 只对**责任范围内**的行判致命。判定里只有范围内的行会产生催办状态
    #    （范围外的直接计入「范围外」跳过），所以范围外的空主键/重复主键
    #    伤不到任何东西，为它停掉整条业务线不成比例。
    #    2026-08-10 实测：GEO 表有 4 行把「提问关键词」填进了企业列
    #    （无序号、无分行），范围内 0 行，却让整份台账读不了。
    #
    # 🔴 但这一步**不能变成静默放行**：范围外的问题降级成数据质量警告，
    #    照样每天显示。而真正的系统性读失败（整列读成空串）必然同时命中
    #    范围内的行 —— 那时仍然是致命，护栏的力度一点没减。
    rows = [r for r in sheet.data_rows if sheet.text(r, name_field)]
    sa = None
    try:
        sa = scope_after(ledger)
    except ValueError:
        pass  # validate_configs 已给出精确错误；这里不许二次裸崩

    def _passes_scope_after(r: int) -> bool:
        if not sa:
            return True
        d = sheet.date(r, sa["field"])
        return d is None or d >= sa["date"]  # 取不到日期就不排除（不能证伪就不排除）

    scoped = [r for r in rows
              if all(match_condition(lambda f, _r=r: sheet.text(_r, f), c)
                     for c in (ledger.get("scope_filters") or []))
              and _passes_scope_after(r)]

    # 🔴 碰撞探测要在 rows 这个全集上做（不是 scoped）：范围外的行是否加
    #    tiebreaker 不影响任何判定结果（它们本来就不会走到 state_key），
    #    在全集上探测只是让「要不要加 tiebreaker」这件事更简单一致，
    #    不用为范围内外分别讨论一遍。见 resolve_row_keys() 的完整说明。
    key_map = resolve_row_keys(sheet, rows, kfields, ktiebreakers)

    def _key_problems(subset: list[int]) -> tuple[int, list[str]]:
        ks = [key_map[r] for r in subset]
        return (sum(1 for k in ks if not k),
                sorted({k for k in ks if k and ks.count(k) > 1}))

    blank, dupes = _key_problems(scoped)
    if blank:
        a.fatal.append(
            f"{key_field} 列有 {blank} 行读到空值（责任范围内共 {len(scoped)} 行有项目名）。"
            f"这会让终止例外表、暂缓名单、催办状态全部失效。"
        )
    if dupes:
        a.fatal.append(
            f"{key_field} 出现重复值：{', '.join(dupes)}。"
            f"重复主键会让两个项目共用一条催办状态，必须先在台账里改掉。"
        )
    # 🔴 2026-08-18 业务决定：责任范围外的条目不告警，只报责任范围内的问题。
    #    range 外空主键/重复主键伤不到任何判定（P0-2 注释已说明），
    #    所以不再统计、不再提示 —— 之前的「预警」用途由业务口径本身取代。

    # ── P0-3 的护栏：日期断言 ──
    date_fields = set()
    for node in ruleset.get("nodes", []):
        clock = node.get("clock") or {}
        # 固定日期列仍做逐列质量提示。fallback 是一组候选字段，不能逐列说
        # “为空就会跳过”；实际只有整组都不可用时才会在当前节点报无起点。
        if clock.get("field"):
            date_fields.add(clock["field"])
    # 🔴 只看**责任范围内、且还没终止**的行。业务 2026-08-14 逐条核对后定的口径。
    #
    #    在此之前这里扫的是全表，于是每天报的几乎全是噪声：舆情「日期」报
    #    1056 行为空，实际全在责任范围外（江苏、南京那些，本来就不催）；
    #    GEO 报 256 行空值，责任范围内是 0 行。真正要动手的问题被埋在里面 ——
    #    飞书「立项时间」整列 731 行解析失败、3 个项目无人管，就是这么被盖住的，
    #    直到业务自己问了一句「那行警告是什么」才挖出来。
    #
    #    **告警太吵和告警缺失是同一种失效**：一堆没人看的警告 == 没有警告。
    #
    #    终止行也要排除：已交付/已关闭的项目，日期填没填都不影响任何判定。
    #    舆情那 5 行「解析不出」全是「已交付」、值只是个 '2026'，报它没有意义。
    terminal_conds = ledger.get("terminal_states") or []

    def _is_terminal(r: int) -> bool:
        return bool(terminal_conds) and all(
            match_condition(lambda f, _r=r: sheet.text(_r, f), c)
            for c in terminal_conds)

    live = [r for r in scoped if not _is_terminal(r)]

    # 🔴 再收一层：**只报真正会用到这一列的行**。
    #    2026-08-14 差点自己制造一批新噪声 —— ④发货 改用「方案完成情况」计时后，
    #    那些值是「未完成」的行被报成「解析不出日期」，可它们根本进不了这个节点
    #    （when 要求 contains 已完成）。报了也没人该动手，纯属吵。
    #    这里按节点自己的 when 过滤，宁可略微多报（一行同时满足两个节点的 when
    #    时，实际只会落在靠前那个），也不少报。
    def _rows_needing(field: str) -> list[int]:
        want = set()
        for node in ruleset.get("nodes", []):
            if not node.get("enabled"):
                continue
            if (node.get("clock") or {}).get("field") != field:
                continue
            conds = node.get("when") or []
            want.update(
                r for r in live
                if conds and all(
                    match_condition(lambda f, _r=r: sheet.text(_r, f), c)
                    for c in conds))
        return sorted(want)

    for f in sorted(date_fields):
        # 🔴 「该列为空」和「有内容但解析不出日期」必须分开说。
        #    合起来报「N 行无法解析为日期」会把「业务还没填」说成故障 ——
        #    2026-08-04 排查哨兵线时我自己就被这句话带偏了半小时：
        #    哨兵前期 11 行、飞书 53 行报「无法解析」，实际全是空值，完全正常。
        #    而真正的解析失败（比如关联字段读出 record id）才是要动手的问题。
        blank, unparsable = [], []
        for r in _rows_needing(f):
            if sheet.date(r, f) is not None:
                continue
            (blank if not sheet.text(r, f) else unparsable).append(key_map[r])
        if blank:
            a.warnings.append(
                f"{f} 有 {len(blank)} 行为空（{', '.join(blank[:8])}"
                f"{' 等' if len(blank) > 8 else ''}）。"
                f"这些行在依赖该列的节点上会被跳过 —— 若属正常（业务尚未填写）可忽略。"
            )
        if unparsable:
            # 🔴 「整列全废」和「几行填错」是两回事，必须分开说。
            #
            #    2026-08-14：飞书「立项时间」整列 731 行解析失败（列类型变成了
            #    带时区的 ISO 字符串），后果是 ④发货 拿不到计时起点、
            #    3 个项目掉进「既不催也不终止」。而它当天报出来的样子，
            #    和「3 行业务还没填」**一模一样**，就那么躺了不知道多久 ——
            #    最后是业务随口问了一句「那行警告是什么」才挖出来。
            #
            #    这条警告本来就会进企微推送（render_wecom 的「需要注意」那节），
            #    所以问题从来不在通道，**在措辞分不出轻重**。
            needed = len(_rows_needing(f))
            if unparsable and len(unparsable) == needed:
                a.warnings.append(
                    f"🔴 {f} 整列失效：需要这一列的 {needed} 行**没有一行**能算出日期。"
                    f"依赖它的节点现在一条都不会催，而这不会报错。"
                    f"几乎可以肯定是台账里那一列的类型或写法变了"
                    f"（例如从日期列改成了关联字段、或改成了带时区的时间戳）。"
                    f"举例：{', '.join(unparsable[:5])}"
                    f"{' 等' if len(unparsable) > 5 else ''}"
                )
            else:
                a.warnings.append(
                    f"{f} 有 {len(unparsable)} 行有内容但解析不出日期"
                    f"（{', '.join(unparsable[:8])}"
                    f"{' 等' if len(unparsable) > 8 else ''}）。"
                    f"这些行在依赖该列的节点上会被跳过。"
                    f"常见原因：该列实际是关联/引用字段，读到的是记录 ID 而不是日期。"
                )
        # 🔴 2026-08-18 业务决定：责任范围外的条目不告警。
        #    这些行本来就不进催办流程，日期解不解析都不影响任何判定。
        today = date.today()
        future = [
            key_map[r] for r in live
            if (d := sheet.date(r, f)) and d > today
        ]
        if future:
            a.warnings.append(
                f"{f} 有 {len(future)} 行是未来日期（序号 {', '.join(future[:8])}）"
                f"，可能填错年份"
            )

    # ── 数据一致性：取值白名单 ──
    # 🔴 这里**不能一刀切地收窄到责任范围内**，两类列的用途相反：
    #
    #    · 用于 scope_filters 的列（比如「分行」）必须继续扫**全表** ——
    #      它正是「台账把『深圳分行』改成『深圳』导致整表落空」那道护栏。
    #      先按范围过滤再检查，等于用出问题的那个条件去筛出问题的证据，
    #      写法一变就再也报不出来，而表现是「今天没有要催的」。
    #    · 其余列（状态、启动优化时间…）只影响范围内的判定，
    #      扫全表只会把范围外的噪声顶上来（GEO 实测：全表 1 种未知取值，
    #      5 行全在范围外）。
    scope_fields = {c.get("field") for c in (ledger.get("scope_filters") or [])}
    for field, allowed in (ledger.get("known_values") or {}).items():
        if not sheet.has_column(field):
            continue
        subject = rows if field in scope_fields else live
        seen = {sheet.text(r, field) for r in subject}
        unknown = sorted(v for v in seen if v and v not in allowed)
        if unknown:
            where = "全表" if field in scope_fields else "责任范围内未终止的行"
            a.warnings.append(
                f"{field} 出现未知取值 {unknown}（已知：{allowed}；查的是{where}）。"
                f"若该列用于范围过滤，这些行会被静默丢掉——请确认是否写法不统一。"
            )

    # ── 复提醒间隔与阈值边界必填/合法 ──
    # 复提醒缺失就默认"只催一次"是不可接受的：新设计的退出机制只有「推进」和「终止」，
    # 任何节点都不该有"自动静默"这第三种。
    for node in ruleset.get("nodes", []):
        if not node.get("enabled"):
            continue
        # 与 validate_configs 共用同一份检查，见 repeat_errors 的注释
        a.fatal.extend(repeat_errors(node.get("repeat"),
                                     f"节点「{node.get('name')}」"))
        thr = node.get("threshold") or {}
        # 存在性用 in 判断，不用真值判断——threshold.days=0 是合法值
        # （"进入即算超期"，靠 repeat.weekday 管节奏），不能被当成"没填"。
        if not ("days" in thr or "workdays" in thr):
            a.fatal.append(
                f"节点「{node.get('name')}」缺少 threshold.days/workdays（阈值）"
            )
        if "days" in thr and "workdays" in thr:
            a.fatal.append(
                f"节点「{node.get('name')}」的 threshold 同时填了 days 和 workdays，"
                f"两者是互斥的两种单位，必须只选一个。"
            )
        b = thr.get("boundary", "after")
        if b not in ("on", "after"):
            # 写错值不许静默按默认走 —— 那会悄悄改掉这个节点的口径
            a.fatal.append(
                f"节点「{node.get('name')}」的 threshold.boundary＝{b!r} 不合法，"
                f"只能是 \"on\"（满 N 天当天提醒）或 \"after\"（N 天内没有才提醒）。"
            )

    # ── 人工名单身份校验 ──
    m = check_manual_lists(sheet, ledger)
    a.fatal.extend(m.fatal)
    a.warnings.extend(m.warnings)

    return a


def first_reminder_day(node: dict) -> int:
    """
    这个节点停滞到第几"天"首次提醒。doctor 的边界对照表和判定共用同一个算法。

    单位取决于 threshold 里填的是 days 还是 workdays——两者不会同时出现
    （assert_sheet 已校验过）。这个数字本身不关心单位，只负责边界换算；
    调用方（evaluate_ledger）要按同一个字段决定「停滞天数」是按自然日
    还是按工作日算，否则「阈值 3 个工作日」会被拿自然日天数去比。
    """
    thr = node.get("threshold") or {}
    # 用 in 判断存在性，不用真值判断——threshold.days=0（"进入即算超期"，
    # 靠 repeat.weekday 之类的日历节律来真正管住节奏）是合法配置，
    # 而 `0 or thr.get("workdays")` 会把它误判成"没填 days，退回 workdays"。
    raw = thr.get("days") if "days" in thr else thr.get("workdays")
    days = int(raw or 0)
    return days if thr.get("boundary", "after") == "on" else days + 1


def is_overdue(stalled_days: int, node: dict) -> bool:
    """
    是否已超期。

    boundary = "on"    → 满 days 天当天就算超期（原文「满1周应提醒」）
    boundary = "after" → days 天内没有才算超期（原文「3天内没有则提醒」）
    缺省 after，即维持历史行为 —— 新增节点不会因为忘填而静默改口径。
    """
    return stalled_days >= first_reminder_day(node)


def allowance_days(node: dict) -> int:
    """
    这个节点「允许拖多久」。超过它才开始算超期天数。

    ═══════════════════════════════════════════════════════════════════
    业务 2026-07-31：「安装日+21天才开始启动提醒，超出 21 天才开始算停滞日期」。

    她的理由比字面更强：现在「节能测试 24 天」和「待收资 29 天」看着差不多，
    实际一个超期 3 天、一个超期 22 天 —— **同一个数字在不同阶段含义完全不同，
    没法横向比较**。改成超期天数之后才排得出优先级。
    ═══════════════════════════════════════════════════════════════════

    用 first_reminder_day() - 1 而不是直接取 threshold.days，有两个理由：

      1. 显示与判定共用同一个边界定义，不会各自漂移。改了 boundary，
         两边同时跟着变
      2. 保证项目一进清单，超期天数必定 ≥ 1。一条刚触发的催办显示
         「超期 0 天」会像 bug —— 而 ①收资 是 boundary=on，按 threshold.days
         算首推那天正好是 0

    ④节能测试 boundary=after days=21 → 首提第 22 天 → 允许 21 → 第 22 天「超期 1 天」
    ①收资     boundary=on    days=7  → 首提第 7 天  → 允许 6  → 第 7 天「超期 1 天」
    """
    return max(first_reminder_day(node) - 1, 0)


# ── 判定 ──────────────────────────────────────────────────────────────

class Item:
    """一条待催单。"""

    def __init__(self, ledger_id, line, key, name, node_id, node_name,
                 stalled_days, clock_from, clock_source, action,
                 backlog_note="", line_label="", stage="", extra=None,
                 allowance=0):
        self.ledger_id = ledger_id
        self.line = line
        # line_label / stage 是「给人读」的名字，用于催办措辞。
        # line  = 内部标识（盒子）、node_name = 带编号的节点名（④节能测试），
        # 都不适合放进句子。编号用于排序，不该出现在给业务看的文字里。
        self.line_label = line_label or line
        self.stage = stage or node_name.lstrip("①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳")
        self.key = key
        self.name = name
        self.node_id = node_id
        self.node_name = node_name
        # 🔴 两个天数，别混：
        #    stalled_days  = 在这个节点待了多久（今天 − 起点）。**判定、排序、
        #                    起点追溯都用它**，口径变更时一行都没动
        #    allowance     = 这个节点允许拖多久
        #    overdue_days  = 超出允许期多久，**只用于显示**
        #
        #    把显示口径做成派生属性，是为了让这次变更的爆炸半径止步于渲染层 ——
        #    判定逻辑是这个工具唯一不能出错的部分，不该为了改个数字去碰它。
        self.stalled_days = stalled_days
        self.allowance = allowance
        self.clock_from = clock_from
        self.clock_source = clock_source
        self.action = action
        self.backlog_note = backlog_note
        self.extra = extra or {}

    @property
    def overdue_days(self) -> int:
        """超出允许期几天。业务看到的就是这个数。"""
        return max(self.stalled_days - self.allowance, 0)

    @property
    def state_key(self) -> str:
        return f"{self.ledger_id}|{self.key}|{self.node_id}"


class Report:
    """一份台账一次运行的完整结果。所有被排除的量都要能报出来。"""

    def __init__(self, ledger: dict):
        self.ledger_id = ledger.get("id")
        self.ledger_name = ledger.get("name")
        self.line = ledger.get("line")
        self.line_label = ledger.get("display_name") or ledger.get("line")
        self.due: list[Item] = []          # 今天要催的
        self.overdue_muted: list[Item] = []  # 超期但未到复提醒间隔
        self.terminal: list[tuple] = []
        self.paused: list[tuple] = []
        # 跨台账核对命中：项目已经在另一份台账里出现，本节点不用再催。
        # 跟 terminal/paused 分开记，是因为它既不是"结束"也不是"暂缓"——
        # 只是催办的接力棒交给了另一张台账，需要单独一栏才能解释总数对不对得上。
        self.advanced: list[tuple] = []
        self.out_of_scope = 0
        self.out_of_scope_detail: dict[str, int] = {}
        self.no_node = 0
        self.not_overdue = 0
        self.disabled_nodes: list[str] = []
        # warnings = 数据质量问题，会影响判定正确性 → 始终显示，不许藏进 --verbose
        self.warnings: list[str] = []
        # notices  = 运维提示（节假日表待核对之类），不影响判定 → 仅 --verbose
        self.notices: list[str] = []
        self.review_hints: list[str] = []
        self.total_rows = 0

    @property
    def accounted(self) -> int:
        return (len(self.due) + len(self.overdue_muted) + len(self.terminal)
                + len(self.paused) + len(self.advanced) + self.out_of_scope
                + self.no_node + self.not_overdue)

    @property
    def in_scope_rows(self) -> int:
        """责任范围内行数；不改变 total_rows/accounted 的逐台账核对。"""
        return max(self.total_rows - self.out_of_scope, 0)


def judge_terminal(get_text, ledger: dict, key: str) -> str | None:
    """
    终止判定。三个信号可靠性不同，按固定优先级判，不混着投票。

    返回终止依据的说明（供业务复核是哪一层做的决定），未终止返回 None。
    """
    # 优先级 0：业务显式点名「无法推进」的项目 —— 最权威，一票定案。
    # 🔴 必须排在语义例外表之前：曾被列为例外（判定为「仍需跟进」）的项目，
    #    业务后来可能改口说推不动了。新口径要能覆盖旧口径，否则例外表会
    #    永远放行它，业务说了不用催却还在催。
    for m in ledger.get("manual_terminal") or []:
        if str(m.get("key")) == key:
            return f"业务确认终止（{m.get('confirmed', '')}）"

    # 优先级 1a：「终止」列（业务将新增）——权威，一票定案
    tc = ledger.get("terminal_column") or {}
    if tc.get("enabled") and tc.get("field"):
        v = get_text(tc["field"])
        if v and v in (tc.get("truthy_values") or []):
            return f"「{tc['field']}」列＝{v}"

    # 优先级 1b：状态列明确的终止值——权威，一票定案，不再看备注、不再看底纹
    for t in ledger.get("terminal_states") or []:
        if match_condition(get_text, t):
            return f"状态列「{t['field']}」＝{get_text(t['field'])}"

    # 优先级 2：备注语义（需先查例外表，否则会误杀）
    exceptions = {str(e.get("key")) for e in (ledger.get("terminal_note_exceptions") or [])}
    if key in exceptions:
        return None
    note = get_text("备注")
    if note:
        for phrase in ledger.get("terminal_note_phrases") or []:
            if phrase in note:
                return f"备注语义「{phrase}」"

    # 优先级 3：灰色底纹——已降级为仅作提示，不再作判据
    return None


def key_fields(ledger: dict) -> list[str]:
    """
    主键字段，允许写成一个字段名或**多个字段名的数组**（组合键）。

    🔴 为什么需要组合键：三份企微台账**一个唯一列都没有**（2026-08-10 实测）。
       舆情的「编号」有 3 组重复，AI体检的「序号」2 组，GEO 的「序号」1 组
       外加 4 行空值；而「企业」这类名字列重复得更多（同一家公司在不同分行
       各开一个项目很正常）。
       重复主键会让两个项目共用一条催办状态、互相静音，所以入口断言判它致命 ——
       也就是说不支持组合键这三份台账一行都读不了。
       实测「编号/序号 + 企业」在三张表上**全部唯一，0 重复**。

    放宽方式与 clock.fallback、repeat.weekday 完全一致（标量或数组都收），
    不另起一套。
    """
    raw = ledger.get("key_field", "序号")
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, list):
        return [f for f in raw if isinstance(f, str) and f.strip()]
    return []


def key_tiebreakers(ledger: dict) -> list[str]:
    """
    可选的「消歧字段」，跟 key_field 是两回事：**有值才拼进主键，没值不影响**。

    🔴 2026-08-20 接 AI外贸拓客台账实测：同一家企业、同一机构、同一天，
       业务确认过某个真实客户会有多个不同方向的项目并行
       跟进——两条记录唯一的区别是「目标国家地区」（一个欧洲、一个日本）。
       但这一列**同时也是①客户填表节点要催的东西**：全新线索本来就还没填。
       把它塞进 key_field（必填），会让最该被催的那批因为空主键报致命错；
       不塞，同一天并行的多个项目就会撞车、共用一条催办状态。

       两难的根源是同一个字段身兼两职：**有值时是消歧信息，没值时是
       "待催办"的信号本身**。key_field 的「任一段为空就整个作废」这条
       规则对它不适用，需要另一种语义：能区分就区分，不能区分不算错。

    跟 key_field 一样支持标量或数组写法，不另起一套。

    🔴 2026-08-21 复审发现：数字、对象、混合数组这类非法值原来会被
       静默过滤成空列表——配置打错，消歧功能悄悄不生效，直到真的撞车
       才会在运行时暴露成主键重复的致命错，而那时候完全看不出来根因
       是 key_tiebreakers 本身配错了。改成非法输入直接报错，离线校验
       当场抓住，不留到「哪天真撞车了才知道」。
    """
    raw = ledger.get("key_tiebreakers")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, list):
        bad = [f for f in raw if not (isinstance(f, str) and f.strip())]
        if bad:
            raise ValueError(
                f"key_tiebreakers 数组里有非法元素：{bad!r}，"
                f"每一项都必须是非空字符串"
            )
        return list(raw)
    raise ValueError(
        f"key_tiebreakers 必须是字符串或字符串数组，实际是 {_type_name(raw)}"
    )


def row_key(sheet, row: int, fields: list[str], tiebreakers: list[str] | None = None) -> str:
    """
    一行的主键值。组合键用 `|` 连接。

    🔴 任一段为空就整个返回空 —— 让入口断言的「主键读到空值」抓住它，
       而不是拼出一个 "|甲公司" 这样两行可能撞上的半截键。

    tiebreakers（可选）：只在**有值**时追加到 key 末尾帮助消歧；
    留空不影响 —— 它解决的是「必填字段相同时还能不能分清是哪一行」，
    不是「这行到底有没有主键」，所以不参与「任一段为空就整个作废」那条规则。
    分隔符跟主 key 的 `|` 不同（用 `‖`），避免两种字段的值凑巧撞在一起。
    """
    parts = [sheet.text(row, f) for f in fields]
    if not all(parts):
        return ""
    key = "|".join(parts)
    for f in (tiebreakers or []):
        v = sheet.text(row, f)
        if v:
            key += f"‖{v}"
    return key


def resolve_row_keys(sheet, rows: list[int], fields: list[str],
                     tiebreakers: list[str] | None = None) -> dict[int, str]:
    """
    给一批行统一算出最终主键。**这是 tiebreakers 唯一该被使用的入口**——
    别再直接给 row_key() 传 tiebreakers，那样算出来的 key 会漂移。

    ═══════════════════════════════════════════════════════════════════
    🔴 复审发现：`row_key(..., tiebreakers)` 原来的语义——"消歧字段有值就
    追加"——会让**同一个项目**的 key 随时间漂移。消歧字段（比如「目标国家
    地区」）往往是业务后填的：一个项目推进一步、这一列从空变有值，
    key 就从 "企业|机构|日期" 变成 "企业|机构|日期‖地区"。而 state_key
    直接拼自这个 key，旧记录（stage_entered / followup_state）在新 key
    下查不到，项目被当成从没出现过。

    实测复现：一个仍在 14 天静默期里的项目，只因为消歧字段被业务填上，
    第二天就重新进了待催清单，而且没有任何报错——这正是本项目最怕的
    「静默变成不该有的行为」。

    根因是把「消歧」和「主键本身」混在了一起。真正需要消歧字段参与的
    只有一种情况：**不加它就会跟别的行撞车**（业务确认过的「同一天
    并行多个项目」，实测只占极少数——trade_qq 里 36 行中的 4 行）。
    绝大多数行从头到尾只有它自己一个，压根不需要消歧，它的 key 就该
    只由 key_field 决定、永远不随消歧字段的值变化而变化。

    做法：先用**不含 tiebreakers** 的基础 key 扫一遍，只有基础 key
    出现次数 > 1（真的撞车）的那几行，才追加 tiebreakers；其余行原样
    用基础 key。消歧字段从空变有值，因此只会影响真正撞车的那一小撮行。

    🔴 残留的、更小众的风险没有完全消除：如果原本 singleton 的一行，
    某天业务新增了一条相同基础 key 的记录（比如真的复购了），且这一行
    自己的消歧字段本来就有值，它的 key 会在这一刻从「不带后缀」变成
    「带后缀」——这仍是一种漂移，但触发条件苛刻得多（需要「新增一行
    同基础 key 记录」这个业务动作，不是每个项目迟早都会经历的填表流程），
    且这本身就是「两个同名项目」这种场景固有的复杂度，不是这个函数
    能单独兜底的。留作已知限制，不在本轮解决。
    ═══════════════════════════════════════════════════════════════════
    """
    base = {r: row_key(sheet, r, fields) for r in rows}
    dup_count: dict[str, int] = {}
    for k in base.values():
        if k:
            dup_count[k] = dup_count.get(k, 0) + 1
    result = {}
    for r in rows:
        b = base[r]
        if b and tiebreakers and dup_count.get(b, 0) > 1:
            result[r] = row_key(sheet, r, fields, tiebreakers)
        else:
            result[r] = b
    return result


def key_label(fields: list[str]) -> str:
    """报错文案里怎么称呼这个主键。"""
    return "+".join(fields) if len(fields) > 1 else (fields[0] if fields else "序号")


def clock_fallback_fields(clock: dict) -> list[str]:
    """返回已通过配置校验的 fallback 字段，非法值防御性地返回空数组。"""
    fallback = clock.get("fallback")
    if isinstance(fallback, str):
        return [fallback.strip()] if fallback.strip() else []
    if isinstance(fallback, list) and all(isinstance(value, str) for value in fallback):
        return [value.strip() for value in fallback if value.strip()]
    return []


# ── 复提醒节律 ──────────────────────────────────────────────────────
#
# 业务改提醒时间是常态，**不该每次都要改代码**。2026-08-10 就栽过：
# 业务把哨兵④发货从「每周三」改成「每周一和每周四」，纯口径改动，
# 却因为 repeat.weekday 只收单个字符串而必须动判定引擎；在那之前的一周里
# 项目每周一都从清单上消失，而业务以为系统漏了它。
#
# 四种写法互斥，各自独立：days / workdays / weekday / monthday。
# 合法性由 assert_sheet 在**配置校验期**把关；下面这几个归一化函数
# 一律防御性返回空，判定期不因为配置写错而崩。

repeat_forms = ("days", "workdays", "weekday", "monthday")

_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _rep(rep) -> dict:
    """repeat 一律先归一成字典 —— 判定期不因为配置写成别的类型而裸崩。
    合法性由 repeat_errors 在校验期把关，这里只保证不炸。"""
    return rep if isinstance(rep, dict) else {}


def _members(raw, scalar_types) -> list:
    """标量或数组都归一成列表 —— 与 clock.fallback 同一个放宽路子。"""
    if isinstance(raw, scalar_types) and not isinstance(raw, bool):
        return [raw]
    return list(raw) if isinstance(raw, list) else []


def repeat_weekdays(rep: dict) -> list[int]:
    """repeat.weekday → 星期索引列表（周一=0）。收字符串或字符串数组。"""
    out: list[int] = []
    for value in _members(_rep(rep).get("weekday"), str):
        idx = _WEEKDAY_INDEX.get(value) if isinstance(value, str) else None
        if idx is not None and idx not in out:
            out.append(idx)
    return sorted(out)


def repeat_monthdays(rep: dict) -> list[int]:
    """repeat.monthday → 几号列表（1~31）。收整数或整数数组。"""
    out: list[int] = []
    for value in _members(_rep(rep).get("monthday"), int):
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if 1 <= value <= 31 and value not in out:
            out.append(value)
    return sorted(out)


def monthday_hits(day: date, wanted: list[int]) -> bool:
    """
    今天是不是配置里的某个「几号」。

    🔴 配了 31 而当月只有 30 天时**按当月最后一天算**，不是整月不发。
       静默跳过一整个月又是一次「看着配了、实际不发」—— 正是这个项目
       一路在消灭的那个形状。
    """
    if not wanted:
        return False
    last = calendar.monthrange(day.year, day.month)[1]
    return any(min(w, last) == day.day for w in wanted)


def repeat_errors(rep: dict, where: str) -> list[str]:
    """
    复提醒节律的合法性检查。**离线校验（validate_configs）与运行时校验
    （assert_sheet）共用这一份**。

    🔴 分开各写一遍的话，迟早只有一边挡得住。rc6 的 alert.timeout_seconds
       就是只有运行时挡、离线还报「通过」；而 2026-08-10 把 weekday 改成
       数组时，`doctor --validate-config` 照样报「全部通过」，直到真读台账
       才炸 TypeError —— 离线校验没覆盖到 repeat，这个函数就是来补这个的。
    """
    errs: list[str] = []
    if rep is None:
        rep = {}
    if not isinstance(rep, dict):
        # 业务照着中文说明写成 "repeat": "每周一" 是最自然的手滑。
        # 不拦的话运行时是一句 AttributeError —— 校验函数自己变成了错误本身。
        return [f"{where}的 repeat 必须是对象，"
                f'如 {{"weekday": ["Mon", "Thu"]}}，实际是 {rep!r}']
    # 🔴 按「键在不在」判，不按值真不真。业务改节律改到一半（旧值删空了、
    #    键忘了删，又加了一行新的）会写出 {"weekday": [], "days": 7} ——
    #    按真值判会看成「只配了 days」而放行，于是节律看着改成 weekday 了、
    #    实际按 days 走，且没有任何地方会说出来。
    forms = [k for k in repeat_forms if k in rep]
    if not forms:
        errs.append(
            f"{where}缺少 repeat（复提醒间隔）。不允许默认成「只催一次」，"
            f"请在 rules.json 里补上。可用写法：days / workdays / weekday / monthday。"
        )
    if len(forms) > 1:
        # 判定是 if weekday … elif monthday … elif workdays … else days，
        # 同时配两种会让后面那种被**静默忽略**。而业务改节律时最可能的动作
        # 恰恰是「加一行新的、忘了删旧的」—— 后果是节律看着改了、实际没改，
        # 且没有任何地方会说出来。这里直接拦死。
        errs.append(
            f"{where}的 repeat 同时配了 {'、'.join(forms)}，只能配一种。"
            f"多配的那种会被静默忽略，所以不许这么写。"
        )
    for key in ("days", "workdays"):
        if key not in rep:
            continue
        n = rep[key]
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            # 🔴 负数是这里面最坏的一种：它**不炸**。判定是
            #    `(today - last).days >= int(days)`，负数恒真，于是节律
            #    静默变成「每天提醒」，而配置看起来是配过的。
            errs.append(
                f"{where}的 repeat.{key} 必须是 ≥1 的整数，实际是 {n!r}。"
                f"（小数会被静默截断、负数会变成每天提醒，所以都不许写）")
    if "weekday" in rep:
        members = _members(rep.get("weekday"), str)
        bad = [m for m in members if not (isinstance(m, str) and m in _WEEKDAY_INDEX)]
        if bad or not members:
            errs.append(
                f"{where}的 repeat.weekday 里 {bad!r} 不合法，"
                f"只能是 Mon/Tue/Wed/Thu/Fri/Sat/Sun 或其全称"
                f"（单个字符串或字符串数组都行）"
            )
    if "monthday" in rep:
        members = _members(rep.get("monthday"), int)
        bad = [m for m in members
               if isinstance(m, bool) or not isinstance(m, int) or not (1 <= m <= 31)]
        if bad or not members:
            errs.append(
                f"{where}的 repeat.monthday 里 {bad!r} 不合法，"
                f"只能是 1~31 的整数（单个整数或整数数组都行）。"
                f"配 31 时，当月不足 31 天则按当月最后一天算。"
            )
    return errs


def cadence_text(rep: dict) -> str:
    """
    把复提醒节律写成人话，给业务在推送里核对「规则改成什么了」。

    **只有这一份实现**，两套渲染与静默期日志共用 —— 分开各写一遍的话，
    迟早只改了一处。认不出来的形状返回空串：不猜，宁可不显示。
    """
    rep = _rep(rep)
    weekdays = repeat_weekdays(rep)
    if weekdays:
        return "/".join(_WEEKDAY_CN[i] for i in weekdays) + "提醒"
    monthdays = repeat_monthdays(rep)
    if monthdays:
        return "每月 " + "/".join(str(d) for d in monthdays) + " 号提醒"
    if rep.get("workdays"):
        return f"每 {int(rep['workdays'])} 个工作日提醒"
    if rep.get("days"):
        n = int(rep["days"])
        return "每天提醒" if n == 1 else f"每 {n} 天提醒"
    return ""


# ── 计时起点的人工播种 ──────────────────────────────────────────────────
#
# 有的台账**一个可靠的日期列都没有**。GEO 那张就是：「需求提出时间」写成
# `6.12`（没有年份）且一大半是空，「结束优化时间」混着「优化中」「7.9有成效」
# 「/」，「启动优化时间」其实是状态列。没有起点就算不出「卡了多久」。
#
# 这种表只能人工读一遍、把每条的起点定下来。播种表就是那份结论。
#
# 🔴 为什么放**配置仓**而不是直接写 state 文件：
#    ① git 留痕，每条都写明依据，业务可以复核；
#    ② 状态目录被清掉或重装之后播种还在；
#    ③ 不碰生产状态文件（铁律④）。
#
# 🔴 播种只是**兜底的一级**，永远排在真实快照之后 —— 见 resolve_clock。
#    反过来的话，程序观测到的真实节点变更会被一条写死的旧日期永久盖住。

def stage_seed_errors(ledger: dict, where: str) -> list[str]:
    """
    播种表的合法性检查。**离线校验与运行时共用这一份。**

    🔴 写坏了不能静默丢掉。丢掉的后果是这一条回退到「无可用起点」而被跳过 ——
       又是一次「配置里明明配了、实际没生效」，而且没有任何地方会说出来。
       同一个形状已经在 alert.timeout_seconds、repeat 上各中过一次。
    """
    seeds = ledger.get("manual_stage_entered")
    if seeds is None:
        return []
    if not isinstance(seeds, list):
        return [f"{where}的 manual_stage_entered 应该是数组，实际是{_type_name(seeds)}"]
    errs: list[str] = []
    for i, s in enumerate(seeds, start=1):
        at = f"{where}的 manual_stage_entered 第 {i} 条"
        if not isinstance(s, dict):
            errs.append(f"{at}应该是对象，实际是{_type_name(s)}")
            continue
        for f in ("key", "node", "entered"):
            if not s.get(f):
                errs.append(f"{at}缺少 {f}")
        entered = s.get("entered")
        if entered and parse_iso(str(entered)) is None:
            errs.append(f"{at}的 entered={entered!r} 不是 YYYY-MM-DD 日期")
        if not s.get("confirmed"):
            # 依据必须写下来 —— 一年后没人记得这个日期是怎么来的，
            # 而它正在决定某个项目每天催不催。
            errs.append(f"{at}缺少 confirmed（这条起点是怎么定出来的，写给复核的人看）")
    return errs


def stage_seed_map(ledger: dict) -> dict:
    """播种表 → {(key, node_id): (日期, 依据)}。合法性已由 stage_seed_errors 把关。"""
    out: dict = {}
    for s in ledger.get("manual_stage_entered") or []:
        if not isinstance(s, dict):
            continue
        when = parse_iso(str(s.get("entered") or ""))
        if when is None:
            continue
        out[(str(s.get("key")), str(s.get("node")))] = (when, s.get("confirmed", ""))
    return out


def resolve_clock(node: dict, get_text, get_date, stage_entered: dict,
                  state_key: str, last_snapshot_date: date | None,
                  last_snapshot_node: str | None, node_id: str,
                  seed: dict | None = None, seed_key: str | None = None
                  ) -> tuple[date | None, str]:
    """
    确定这个节点的计时起点，返回 (起点日期, 起点来源说明)。

    ═══════════════════════════════════════════════════════════════════
    🔴 这是全方案最容易写错、且写错不报错的地方。

    需求原文里 ③ 是「专家评估后 3 周」、④ 是「安装成功后 3 周」——起点都是
    某个节点的完成时刻。但台账里没有这两个日期列（只有「需求上报日期」和
    「最新进展日期」）。所以起点靠快照 diff 自建。

    首次运行时没有任何历史快照。此时若把起点写成"今天"，停滞 171 天的项目
    会被算成 0 天，首日一条都不催 —— 36 条全部消失，而且不报错，看起来
    像"今天很安静"。必须回退到「最新进展日期」。
    ═══════════════════════════════════════════════════════════════════
    """
    clock = node.get("clock") or {}

    # 固定日期列（①② 的「自上报日起」）
    if clock.get("field"):
        return get_date(clock["field"]), clock["field"]

    if not clock.get("stage_entered"):
        raise LedgerError(f"节点「{node.get('name')}」的 clock 配置无效")

    fallback_field, fallback = None, None
    fallback_fields = clock_fallback_fields(clock)
    for name in fallback_fields:
        candidate = get_date(name)
        if candidate:
            fallback_field, fallback = name, candidate
            break
    if fallback_field is None:
        fallback_field = "、".join(fallback_fields) if fallback_fields else None

    recorded = parse_iso(stage_entered.get(state_key))
    if recorded:
        return recorded, "快照累积的节点进入时间"

    # 🔴 人工播种：**排在真实快照之后、fallback 之前**，一级都不能挪。
    #    排到快照前面的话，程序观测到的节点变更会被一条写死的旧日期永久盖住，
    #    而项目推进了却还按旧起点算超期 —— 天数只会越算越大，没人看得出不对。
    seeded = (seed or {}).get((seed_key, node_id))
    if seeded:
        when, basis = seeded
        return when, f"人工播种起点（{basis}）" if basis else "人工播种起点"

    # 该 (项目, 节点) 没有在跑的周期。三种情形：
    #   a) 首次运行（没有上次快照）→ 用 fallback，这是上面说的那个陷阱
    #   b) 节点在两次运行之间推进了
    #   c) 节点回退，旧周期刚被 evaluate_ledger 作废掉
    #
    # b/c 的口径（业务 2026-07-31 确认）：**用「最新进展日期」，但不早于上次快照日**。
    # 理由：上次快照那天它确知还在别的节点，不可能更早进入本节点。
    #   · 天天跑 → 误差最多 1 天
    #   · 停机两周再开机 → 也不会算出「停滞 200 天」这种假数字
    changed = last_snapshot_node not in (None, node_id)
    if changed and last_snapshot_date:
        if not fallback:
            # 日期列是空的，但我们确知它是在上次快照之后才进本节点的
            return last_snapshot_date, "上次快照日（日期列为空）"
        if fallback < last_snapshot_date:
            return last_snapshot_date, f"上次快照日（{fallback_field} 未随节点变更更新）"
    if fallback:
        src = f"{fallback_field}（首次初始化，近似值）"
        return fallback, src
    return None, "无可用起点"


def close_cycle(stage_history: dict, state_key: str, stage_entered: dict,
                today: date, reason: str) -> None:
    """
    结束一个节点的计时周期：从 stage_entered 摘掉，存进 stage_history。

    只增不删。副产品是各阶段真实耗时的数据 —— 方案里说「要跑 1~2 个月才有样本」
    的那份，从现在开始自动积累。
    """
    entered = stage_entered.pop(state_key, None)
    if not entered:
        return
    stage_history.setdefault(state_key, []).append(
        {"entered": entered, "closed": iso(today), "reason": reason}
    )


_WEEKDAY_INDEX = {
    "Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6,
    "Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3, "Friday": 4,
    "Saturday": 5, "Sunday": 6,
}


def read_ledger_sheet(ledger: dict):
    """
    按台账的 source 取数，返回统一接口（header/data_rows/text/date/has_column）
    的 Sheet 对象——core.py 其余逻辑不关心背后是腾讯文档还是飞书多维表格。

    接新数据源时只在这里加一个分支，不动调用方。
    """
    source = ledger.get("source", "tencent_mcp")
    if source == "tencent_mcp":
        return qqdoc.read_sheet(ledger["file_id"], ledger["sheet_id"])
    if source == "lark_cli":
        return lark_base.read_sheet(
            ledger["base_token"], ledger["table_id"],
            profile=ledger.get("profile", "sentinel"),
            link_date_fields=ledger.get("link_date_fields"),
        )
    if source == "wecom_doc":
        # 🔴 url + sheet_id 必须成对：实测两份不同文档里都有 sheet_id BB08J2。
        return wecom_doc.read_sheet(ledger["url"], ledger["sheet_id"])
    raise LedgerError(
        f"台账「{ledger.get('name')}」的 source={source!r} 不支持。"
        f"目前只实现了 tencent_mcp、lark_cli 和 wecom_doc。"
    )


def _cross_identity(get_text, specs: list[dict], side: str) -> tuple[str, ...] | None:
    """按配置生成联合身份；任一字段为空时不进行不可靠匹配。"""
    values = []
    for spec in specs:
        field = spec[f"{side}_field"]
        value = get_text(field).strip()
        if not value:
            return None
        values.append(spec["normalize_map"].get(value, value))
    return tuple(values)


def _cross_cache_key(lid: str, specs: list[dict], guard: dict | None) -> tuple:
    return (
        lid,
        tuple((spec["target_field"],
               tuple(sorted(spec["normalize_map"].items()))) for spec in specs),
        # 🔴 guard 的目标列必须进缓存键：同一份目标台账被两个节点引用、
        #    而只有一个配了 not_before 时，少了这一项两边会共用同一份索引，
        #    其中一个拿到的日期列表是空的 —— 约束静默失效。
        (guard or {}).get("target_field"),
    )


def _cross_ledger_values(cfg: dict, all_ledgers: dict, cache: dict,
                         default_local: str = "项目名称"
                         ) -> tuple[dict, list[dict], dict | None]:
    """
    建立目标责任范围内的联合身份索引。

    值是**每个身份下各命中行的日期列表**（配了 not_before 时取那一列，
    否则整列为 None）。以前只存一个计数 —— 那样拿不到任何时间信息，
    也就没法排除「立项时间比需求提出还早」的行。
    """
    lid = cfg.get("ledger_id")
    target = (all_ledgers or {}).get(lid)
    if target is None:
        raise LedgerError(
            f"节点的 cross_ledger 引用了台账 id={lid!r}，但没有在 all_ledgers 里找到。"
            f"多台账核对必须能看到全部台账配置，不能静默当成「没查到」——"
            f"那会让这个节点永远催、永远不消失。"
        )
    try:
        specs = cross_match_fields(cfg, default_local)
        guard = cross_not_before(cfg)
    except ValueError as exc:
        raise LedgerError(f"跨台账核对配置无效：{exc}") from exc
    cache_key = _cross_cache_key(lid, specs, guard)
    if cache_key in cache:
        return cache[cache_key], specs, guard

    target_sheet = read_ledger_sheet(target)
    name_field = target.get("name_field", "项目名称")
    required = {name_field}
    required.update(spec["target_field"] for spec in specs)
    required.update(f.get("field") for f in target.get("scope_filters") or []
                    if f.get("field"))
    if guard:
        # 列不存在必须走下面那条致命错，不能当成「没查到」——
        # 那会让约束静默失效，而表现只是「怎么还在催」。
        required.add(guard["target_field"])
    for field in sorted(required):
        if not target_sheet.has_column(field):
            raise LedgerError(
                f"跨台账核对：目标台账「{target.get('name') or lid}」里没有"
                f"名为「{field}」的列。现有列：{[h for h in target_sheet.header if h][:20]}。\n"
                f"🔴 这不能当成「没查到」继续跑 —— 那会让这个节点永远催下去。"
            )

    index: dict[tuple[str, ...], list] = {}
    for row in target_sheet.data_rows:
        target_get = lambda field, _row=row: target_sheet.text(_row, field)  # noqa: E731
        if not target_get(name_field):
            continue
        if any(not match_condition(target_get, condition)
               for condition in target.get("scope_filters") or []):
            continue
        identity = _cross_identity(target_get, specs, "target")
        if identity is not None:
            # 日期取不到就存 None ——「不能证伪」的行必须保留为候选，
            # 不许因为读不出日期就把它排除掉（那会让判定误停）。
            when = (target_sheet.date(row, guard["target_field"])
                    if guard else None)
            index.setdefault(identity, []).append(when)
    cache[cache_key] = index
    return index, specs, guard


def evaluate_ledger(ledger: dict, ruleset: dict, workday: WorkdayCalc,
                    today: date, stage_entered: dict, followup_state: dict,
                    last_snapshot: dict, stage_history: dict | None = None,
                    all_ledgers: dict | None = None,
                    ) -> tuple[Report, dict]:
    """
    对一份台账跑一次完整判定。

    返回 (报告, 本次快照)。不写任何文件——写入由调用方统一做，
    这样 doctor 可以跑判定而不留痕。

    all_ledgers：{ledger_id: 台账配置} 的映射，只在某个节点声明了
    cross_ledger（比如"是否已出现在另一张台账里"）时才用到。不传就是 None——
    这类节点碰到时会直接报错，而不是静默当成"没查到"继续催个没完。
    """
    if stage_history is None:
        stage_history = {}
    rep = Report(ledger)
    cross_cache: dict[tuple, dict] = {}
    sheet = read_ledger_sheet(ledger)

    a = assert_sheet(sheet, ledger, ruleset)
    if not a.ok:
        raise LedgerError(
            "入口断言未通过，拒绝继续判定（避免把故障表现成「今天没有超时单」）：\n  - "
            + "\n  - ".join(a.fatal)
        )
    rep.warnings.extend(a.warnings)
    if workday.holiday_warning:
        # 运维提示，不影响判定正确性 → 归 notices，只在 --verbose 显示
        rep.notices.append(workday.holiday_warning)

    for node in ruleset.get("nodes", []):
        if not node.get("enabled"):
            label = node.get("stage") or node.get("name", "")
            rep.disabled_nodes.append(
                f"{label}：未启用"
                + (f" —— {node.get('_禁用原因')}" if node.get("_禁用原因") else "")
            )

    kfields = key_fields(ledger)
    # key_tiebreakers(ledger) 已在 assert_sheet 里校验过（config 就是同一个
    # ledger），走到这里必然合法，不必再 try/except —— 那道 gate 已经在本函数
    # 开头把 not a.ok 的情况挡在了这里之前。
    ktiebreakers = key_tiebreakers(ledger)
    key_field = key_label(kfields)
    name_field = ledger.get("name_field", "项目名称")
    paused_map = {str(p.get("key")): p.get("reason", "") for p in (ledger.get("paused") or [])}
    # 计时起点的人工播种，给那些一个可靠日期列都没有的台账用（见 stage_seed_map）
    seed = stage_seed_map(ledger)
    # 灰名单支持对象数组与旧的字符串数组两种写法，统一由 manual_list_entries 规整
    gray = {e["key"] for e in manual_list_entries(ledger, "gray_list_for_review")}

    snapshot: dict[str, str] = {}
    rows = [r for r in sheet.data_rows if sheet.text(r, name_field)]
    rep.total_rows = len(rows)
    # scope_after(ledger) 已在 assert_sheet 里校验过（config 就是同一个 ledger），
    # 走到这里必然合法，不必再 try/except —— 那道 gate 已经在 evaluate_ledger
    # 开头把 not a.ok 的情况挡在了这个循环之前。
    sa = scope_after(ledger)
    # 🔴 主键必须用 resolve_row_keys()，不能直接给 row_key() 传 ktiebreakers——
    # 那样算出来的 key 会随消歧字段的值漂移，state_key 跟着变，旧的
    # stage_entered/followup_state 记录查不到，项目被当成从没出现过，
    # 可能重发一次首次催办。见 resolve_row_keys() 的完整说明。
    key_map = resolve_row_keys(sheet, rows, kfields, ktiebreakers)

    for r in rows:
        get_text = lambda f, _r=r: sheet.text(_r, f)  # noqa: E731
        get_date = lambda f, _r=r: sheet.date(_r, f)  # noqa: E731
        key = key_map[r]
        name = sheet.text(r, name_field)

        # 1) 责任范围：范围外的行压根不进催办流程
        skipped = False
        for f in ledger.get("scope_filters") or []:
            if not match_condition(get_text, f):
                rep.out_of_scope += 1
                label = f"{f['field']}＝{get_text(f['field']) or '(空)'}"
                rep.out_of_scope_detail[label] = rep.out_of_scope_detail.get(label, 0) + 1
                skipped = True
                break
        if not skipped and sa:
            d = get_date(sa["field"])
            # 取不到日期就不排除（不能证伪就不排除，同 P0-2 那条护栏一个道理）
            if d is not None and d < sa["date"]:
                rep.out_of_scope += 1
                label = f"{sa['field']}早于{sa['date'].isoformat()}"
                rep.out_of_scope_detail[label] = rep.out_of_scope_detail.get(label, 0) + 1
                skipped = True
        if skipped:
            continue

        # 2) 终止：范围内但已结束
        reason = judge_terminal(get_text, ledger, key)
        if reason:
            rep.terminal.append((key, name, reason))
            # 灰色名单与终止判定不一致的，列出来供业务清账
            if key in gray:
                pass  # 一致，无需提示
            continue
        if key in gray:
            rep.review_hints.append(
                f"{key} {name}：底纹标灰但按规则未判终止，请确认是漏标还是底纹标错"
            )

        # 3) 暂缓：等外部事件，不催但单独记
        if key in paused_map:
            rep.paused.append((key, name, paused_map[key]))
            continue

        # 4) 当前节点 = 第一个 when 全部满足的启用节点
        current = None
        for node in ruleset.get("nodes", []):
            if not node.get("enabled"):
                continue
            conds = node.get("when") or []
            if conds and all(match_condition(get_text, c) for c in conds):
                current = node
                break
        if current is None:
            rep.no_node += 1
            continue

        # 4b) 跨台账核对：这个节点的完成信号不在本台账里，而是"出现在另一张
        # 台账里"（比如③分行扫码登记）。查到了就不再算它卡在这个节点——
        # 不管是不是在阈值窗口内查到的：迟到也是到了，不该继续催。
        cross_cfg = current.get("cross_ledger")
        if cross_cfg:
            index, specs, guard = _cross_ledger_values(
                cross_cfg, all_ledgers or {}, cross_cache, name_field)
            identity = _cross_identity(get_text, specs, "local")
            hits = list(index.get(identity, ())) if identity is not None else []
            raw = len(hits)

            # 「时间不可能倒流」：目标行的日期早于本行的日期，就不可能是同一个
            # 项目 —— 主台账里的项目是从这里推进过去的。见 cross_not_before()。
            #
            # 🔴 两处「不能证伪就不排除」：本行日期取不到 → 整条约束不生效；
            #    目标行日期取不到（存的是 None）→ 该行保留为候选。
            #    两者都倒向「继续催」，而不是「误判为已接力后静默不催」。
            excluded = 0
            if guard:
                floor = get_date(guard["local_field"])
                if floor is not None:
                    kept = [d for d in hits if d is None or d >= floor]
                    excluded = len(hits) - len(kept)
                    hits = kept

            matches = len(hits)
            if matches == 1:
                target_name = (all_ledgers or {}).get(cross_cfg.get("ledger_id"), {}).get("name", cross_cfg.get("ledger_id"))
                # 业务口径④：判定结果要标明依据。靠时间排除才定下来的，
                # 必须说出来，否则业务无从复核这一步对不对。
                why = f"已出现在「{target_name}」"
                if excluded:
                    why += (f"（同名 {raw} 条，按「{guard['target_field']}"
                            f"不早于{guard['local_field']}」排除 {excluded} 条）")
                rep.advanced.append((key, name, why))
                if excluded:
                    # 🔴 `advanced` 在渲染层**只以计数出现**（「跨台账核对通过 N」），
                    #    逐条理由不显示 —— 光把 why 塞进去等于没写。
                    #    靠时间推断才定下来的，得能复核，所以另发一条 notice。
                    #    notices 只进终端/日志渲染，**不进企微推送**：
                    #    这条已经settled 的判定天天推给业务就是噪声，
                    #    而排查的人看日志时需要它。
                    rep.notices.append(f"{key} {name}：{why}")
                continue
            if matches > 1:
                rep.review_hints.append(
                    f"{key} {name}：跨台账联合身份命中 {matches} 条，"
                    f"无法确认是哪一个项目，未判定为已接力，继续催办")

        node_id = current["id"]
        snapshot[key] = node_id
        state_key = f"{ledger['id']}|{key}|{node_id}"

        last_snap_node = (last_snapshot.get("nodes") or {}).get(key)
        last_snap_date = parse_iso((last_snapshot or {}).get("date"))

        # ── 节点变更处理（含返工回退）──────────────────────────────
        # 🔴 旧实现只 setdefault、从不清理。项目 A→B 时 key|A 的旧条目留在文件里；
        #    日后返工退回 A，recorded 直接命中它，凭空算出几百天停滞。
        #    而且不报错 —— 只表现为「这条怎么突然停滞 200 天」。
        if last_snap_node is not None and last_snap_node != node_id:
            # 1) 上一个节点的周期到此为止，归档
            close_cycle(stage_history, f"{ledger['id']}|{key}|{last_snap_node}",
                        stage_entered, today, f"节点变更为 {node_id}")

            # 2) 进入本节点 = 开新周期，本节点的一切遗留都必须清掉。
            #
            # 🔴 这里有个反直觉的地方，第一版就踩了：**不能只看 stage_entered**。
            #    A→B 时 A 的 stage_entered 条目已经被上面那句 close_cycle 摘走了，
            #    等到 B→A 回来时它早就不在了，看它等于什么都没看见。
            #    真正活到现在的是 followup_state —— 它只在「未超期」时才被清，
            #    一个超期状态下换了节点的项目，会把 last_notified 一直带着。
            #    不清它，回退后的新周期一进来就被上一轮的复提醒间隔静默掉：
            #    **返工之后反而不催了**，而这不会报错。
            revisit = (state_key in stage_entered
                       or state_key in followup_state
                       or state_key in stage_history)
            if state_key in stage_entered:
                close_cycle(stage_history, state_key, stage_entered, today,
                            f"从 {last_snap_node} 重新进入本节点，旧周期作废")
            followup_state.pop(state_key, None)
            if revisit:
                rep.notices.append(
                    f"{key} {name}：从「{last_snap_node}」退回「{node_id}」，已开始新的计时周期"
                )

        start, src = resolve_clock(current, get_text, get_date, stage_entered,
                                   state_key, last_snap_date, last_snap_node, node_id,
                                   seed=seed, seed_key=key)
        if start is None:
            rep.warnings.append(f"{key} {name}：{current['name']} 无可用计时起点，跳过")
            rep.no_node += 1
            continue

        # 🔴 起点一确定就落盘，且对「未超期」的项目同样落盘。
        # 若只给超期项落盘、未超期项留到下次用"今天"，它们的计时会每天重新开始，
        # 永远跨不过阈值 —— 又一处不报错的静默失效。
        stage_entered.setdefault(state_key, iso(start))

        thr_cfg = current.get("threshold") or {}
        if thr_cfg.get("workdays"):
            stalled = workday.count_between(start, today)
        else:
            stalled = (today - start).days
        item = Item(ledger["id"], ledger.get("line"), key, name, node_id,
                    current["name"], stalled, start, src,
                    current.get("action", ""), current.get("backlog_note", ""),
                    ledger.get("display_name", ""), current.get("stage", ""),
                    allowance=allowance_days(current))

        # 5) 未超期：清掉计时，让"推进过又重新卡住"能重新算作新超期
        # 边界由节点的 threshold.boundary 决定（on = 满 N 天当天，after = 第 N+1 天），
        # 不再是写死的 <=。见 is_overdue()。
        if not is_overdue(stalled, current):
            followup_state.pop(state_key, None)
            rep.not_overdue += 1
            continue

        # 6) 超期：按复提醒间隔决定今天催不催
        st = followup_state.setdefault(state_key, {})
        st.setdefault("first_overdue", iso(today))
        last = parse_iso(st.get("last_notified"))
        rep_cfg = current.get("repeat") or {}
        weekdays = repeat_weekdays(rep_cfg)
        monthdays = repeat_monthdays(rep_cfg)
        if weekdays:
            # 日历节律：不是"距上次过了几天"，是"只在这几个星期几提醒"——
            # 首次超期也不例外，否则第一条会在错的日子被催出去。
            due = today.weekday() in weekdays
        elif monthdays:
            # 同上，只不过锚在「几号」。月末回退见 monthday_hits。
            due = monthday_hits(today, monthdays)
        elif last is None:
            due = True  # 首次超期，一定催
        elif rep_cfg.get("workdays"):
            due = workday.count_between(last, today) >= int(rep_cfg["workdays"])
        else:
            due = (today - last).days >= int(rep_cfg["days"])

        item.extra["first_overdue"] = st["first_overdue"]
        item.extra["last_notified"] = st.get("last_notified")
        # 待催和静默两份都带上：静默期日志正是靠它解释「今天为什么没有它」
        item.extra["cadence"] = cadence_text(rep_cfg)
        if due:
            rep.due.append(item)
        else:
            rep.overdue_muted.append(item)

    # 责任范围把整张台账过滤光了 —— 报一声，别让它长得像「今天没有要催的」。
    #
    # 🔴 2026-08-06 真踩过：业务把「分行」从「深圳分行」改成「深圳」，
    #    而 scope_filters 里写的还是带后缀的写法，于是 AI体检 / GEO 两条线
    #    唯一那行被判成范围外，推送里显示「总任务量：0 个项目里，0 个要催办」。
    #    known_values 只校验状态列，scope_filters 的取值一直没有任何校验。
    #
    # 🔴 只报数据质量警告，**不报故障、不改退出码**：「今天这条线确实一个
    #    项目都不在责任范围内」是完全合法的状态（新线刚接入时就是），
    #    报错会制造狼来了。
    if rep.total_rows > 0 and rep.in_scope_rows == 0:
        seen = "、".join(f"{k} {v} 行" for k, v in
                         sorted(rep.out_of_scope_detail.items(), key=lambda kv: -kv[1]))
        rep.warnings.append(
            f"整张台账都被责任范围过滤掉了（{rep.total_rows} 行全部判为范围外）。"
            f"实际观测到的取值：{seen or '(无)'}。"
            "请确认 scope_filters 的写法是否还匹配台账现状 —— "
            "台账里换个写法（比如「深圳分行」改成「深圳」）就会全表落空，"
            "而表现和「今天没有要催的」一模一样。"
        )

    # stage_entered 已在主循环里逐行落盘（起点一算出就 setdefault），
    # 这里不再补写 —— 补写会把未超期项目的起点误设为今天。
    return rep, {"date": iso(today), "nodes": snapshot}
