#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一次性迁移：rc14 主键规则 → rc15 撞车感知主键规则。

═══════════════════════════════════════════════════════════════════════
🔴 2026-08-21 复审发现的 P0：rc14 里 key_tiebreakers 只要字段有值就无条件
拼进主键；rc15 换成 resolve_row_keys()——只有真撞车（同一个基础主键当前
有 ≥2 行）才拼，其余一律用不带后缀的基础主键。"从没撞过车、但消歧字段
本来就有值"的 singleton 项目，主键会在升级那一刻从带后缀变回不带后缀——
state_key 跟着变，stage_entered/followup_state/stage_history 里的历史
记录在新 key 下查不到，项目被当成从没出现过，升级本身（零数据变化）
就会让它们集体重新触发一次首次催办。

这份脚本要把状态文件里"仍按 rc14 规则命名"的记录，原地改名到 rc15 会
算出的新 key，历史值一个字节不动，不判定、不改台账、不发任何通知。跟
运行时的撞车安全网（见 core.py 里 resolve_row_keys 的 `existing_state_keys`
参数）是两回事、互补：这份脚本管的是"升级那一刻、零数据变化"的一次性
漂移；撞车安全网管的是"迁移完成之后，未来某天真的新增一行同基础 key
记录"这种持续风险。

🔴 2026-08-21 第三轮复审又发现两处：
  1. `snapshot_last_<台账>.json` 里的 `nodes`（记录"这个 key 上次在哪个
     节点"，用来判断项目是不是从上一节点推进过来）也是按同一套 key 存的，
     漏了不改，会导致：迁移后项目实际推进了节点，判定却查不到旧 snapshot
     的 key，识别不出"节点变了"，于是旧节点的计时/通知状态没被正确归档，
     新节点又另开一份——两边同时活着，后续复提醒全乱。这份脚本现在跟
     另外三份状态文件一起迁移它。
  2. 迁移必须跟每日任务共用同一把运行锁（`core.acquire_lock`）——不然
     真赶上 9 点任务在跑的当口执行迁移，会覆盖对方刚写完的状态。
     锁只在真正写盘（`--apply`）时才取，纯读计划不需要。
     任何一步失败（台账读取失败、改名遇到冲突、抢不到锁）都必须让整个
     脚本非零退出——不能打印一堆"看起来在工作"的输出、最后却说"已写入"。
═══════════════════════════════════════════════════════════════════════

用法：
    cd ~/.hermes/skills/work/followup-genie
    FOLLOWUP_HOME=~/.hermes python3 scripts/migrate_rc15_key_state.py            # 只打印计划
    FOLLOWUP_HOME=~/.hermes python3 scripts/migrate_rc15_key_state.py --apply    # 真正改写

--apply 前会把改写前的状态文件整份拷贝备份到
state/.migrate-rc15-backup-<时间戳>/，不是覆盖式改名——铁律④
「不删除文件，只新增；修改需留痕」。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime

import core

# state_key 形如 "ledger_id|key|node_id" 的三份共享状态文件。
STATE_FILES = ("stage_entered.json", "followup_state.json", "stage_history.json")


def snapshot_file_name(ledger_id: str) -> str:
    return f"snapshot_last_{ledger_id}.json"


def plan_ledger(ledger: dict) -> list[tuple[str, str]]:
    """返回这份台账 [(旧 key, 新 key), ...] 的漂移清单，没有漂移就是空列表。"""
    kfields = core.key_fields(ledger)
    try:
        ktiebreakers = core.key_tiebreakers(ledger)
    except ValueError:
        return []  # 配置本身不合法，交给 --validate-config 报，这里不重复报
    if not ktiebreakers:
        return []  # 没配消歧字段，rc14/rc15 算出来的 key 从来不会有差异

    sheet = core.read_ledger_sheet(ledger)
    name_field = ledger.get("name_field", "项目名称")
    rows = [r for r in sheet.data_rows if sheet.text(r, name_field)]

    # rc14：无条件传 tiebreakers，字段有值就拼；rc15：只有真撞车才拼。
    old_map = {r: core.row_key(sheet, r, kfields, ktiebreakers) for r in rows}
    new_map = core.resolve_row_keys(sheet, rows, kfields, ktiebreakers)

    plans = []
    for r in rows:
        old_key, new_key = old_map[r], new_map[r]
        # 空主键、本行本轮就是撞车歧义冻结（new_key is None）、没有漂移，都跳过。
        if old_key and new_key is not None and old_key != new_key:
            plans.append((old_key, new_key))
    return plans


def apply_plan(ledger_id: str, plans: list[tuple[str, str]],
               state: dict[str, dict]) -> list[str]:
    """把 state 三份字典里匹配旧 key 的条目原地改名到新 key。返回人话日志。"""
    log = []
    for old_key, new_key in plans:
        old_prefix = f"{ledger_id}|{old_key}|"
        new_prefix = f"{ledger_id}|{new_key}|"
        for name, d in state.items():
            for k in [k for k in d if k.startswith(old_prefix)]:
                new_k = new_prefix + k[len(old_prefix):]
                if new_k in d:
                    log.append(f"⚠️  {name}：{k} 想改名到 {new_k}，但目标已存在，"
                              f"跳过（两条记录都原样保留，需人工核对）")
                    continue
                d[new_k] = d.pop(k)
                log.append(f"{name}：{k} → {new_k}")
    return log


def apply_snapshot_plan(plans: list[tuple[str, str]], nodes: dict) -> list[str]:
    """
    snapshot_last_<台账>.json 的 `nodes` 字典直接按裸 key 存（不带 ledger_id/
    node_id 前后缀），跟另外三份状态文件的 key 形状不一样，单独处理。
    """
    log = []
    for old_key, new_key in plans:
        if old_key not in nodes:
            continue
        if new_key in nodes:
            log.append(f"⚠️  snapshot_last：{old_key} 想改名到 {new_key}，"
                      f"但目标已存在，跳过（两条记录都原样保留，需人工核对）")
            continue
        nodes[new_key] = nodes.pop(old_key)
        log.append(f"snapshot_last：{old_key} → {new_key}")
    return log


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="真正改写状态文件；不加只打印计划")
    args = ap.parse_args()

    # 🔴 锁只在真正写盘时才取——纯读计划不写状态，跟每日任务不存在竞态，
    #    不该为了看一眼计划就去抢锁、跟正在跑的 9 点任务过不去。
    lock_path = None
    lock_token = ""
    if args.apply:
        try:
            lock_path, lock_token, steal = core.acquire_lock()
            if steal:
                print(f"⚠️  {steal}", file=sys.stderr)
        except core.LockBusy as e:
            print(f"❌ 抢不到运行锁，本次迁移未执行（{e}）。"
                  f"多半是每日任务正在跑，等它结束后重试。", file=sys.stderr)
            return 1

    try:
        return _run(apply=args.apply)
    finally:
        if lock_path:
            core.release_lock(lock_path, lock_token)


def _run(*, apply: bool) -> int:
    core.set_read_only(not apply)

    ledgers_cfg, _, _ = core.load_configs()
    ledgers = [l for l in ledgers_cfg.get("ledgers", []) if l.get("enabled")]
    state = {name: core.read_state(name) for name in STATE_FILES}

    any_plan = False
    had_read_failure = False
    had_conflict = False
    snapshots: dict[str, dict] = {}  # ledger_id -> snapshot_last 内容，仅对有计划的台账加载

    for ledger in ledgers:
        lid = ledger["id"]
        try:
            plans = plan_ledger(ledger)
        except Exception as e:
            had_read_failure = True
            print(f"⚠️  台账「{lid}」取数失败，跳过（{type(e).__name__}: {e}）",
                  file=sys.stderr)
            continue
        if not plans:
            continue
        any_plan = True
        print(f"── {lid} ──")
        for old_key, new_key in plans:
            print(f"  {old_key!r} → {new_key!r}")

        log = apply_plan(lid, plans, state)
        snap = core.read_state(snapshot_file_name(lid))
        log += apply_snapshot_plan(plans, snap.setdefault("nodes", {}))
        snapshots[lid] = snap

        for line in log:
            print(f"    {line}")
            if line.lstrip().startswith("⚠️"):
                had_conflict = True

    if not any_plan:
        print("没有发现需要迁移的记录，状态文件与 rc15 主键规则已经一致。")
        return 1 if had_read_failure else 0

    if not apply:
        print("\n以上是计划，未写入任何文件。加 --apply 才会真正改写。")
        return 1 if had_read_failure else 0

    if had_read_failure:
        print("\n❌ 有台账取数失败，为避免只迁移一部分留下不一致的状态，本次不写入任何文件。"
              "请解决取数问题后重跑。", file=sys.stderr)
        return 1

    backup_dir = core.state_dir() / f".migrate-rc15-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for name in list(STATE_FILES) + [snapshot_file_name(lid) for lid in snapshots]:
        src = core.state_dir() / name
        if src.exists():
            shutil.copy2(src, backup_dir / name)
    print(f"\n已备份改写前的状态文件到 {backup_dir}")

    for name in STATE_FILES:
        core.write_state(name, state[name])
    for lid, snap in snapshots.items():
        core.write_state(snapshot_file_name(lid), snap)
    print("已写入迁移后的状态文件。")

    if had_conflict:
        print("⚠️  部分记录因目标 key 已存在而跳过（见上方日志），需人工核对——"
              "本次退出码仍非零，提醒不要当作完全成功。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
