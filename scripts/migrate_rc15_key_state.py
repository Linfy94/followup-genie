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

🔴 2026-08-21 第四轮复审揪出一个更狠的组合拳：conflict（改名冲突）
   原来的处理是"冲突的那条跳过、其余照常写入"——单独看没问题，但
   `bootstrap.py` 看到迁移非零退出就会整体回滚，**而回滚只恢复代码，
   状态文件已经写进去的那部分改不回来**。于是"冲突→部分写入→退出
   非零→代码回滚"这条链走完，state 已经是新 key、代码却退回了旧版，
   旧代码按旧规则找不到这批刚被改名的历史记录，当成新项目重新催办——
   跟这整个迁移脚本要防的 P0 一模一样，只是换了个触发路径。
   实测复现过：两条台账，一条正常改名、一条冲突，冲突的那条确实被
   跳过了，但正常那条已经落盘，此时退出码非零。

   修复：**只要出现任何一个冲突，整次迁移一个字节都不写**——跟
   台账读取失败走同一条"全须全尾要么都成、要么都不动"的路径，
   不再有"部分写入 + 非零退出"这种中间态。真正写盘之后如果还失败
   （比如磁盘写到一半炸了），必须在还持着锁的状态下，用刚才那份
   备份把所有受影响的文件整体复原，不能留半写状态给下一次运行去猜。

   此外原来每次升级都会重新联网读一遍配置了 key_tiebreakers 的台账、
   即使早就迁移完成——这既浪费，也让日后所有升级都莫名其妙依赖一次
   跟本次改动无关的网络请求，网络或权限一抖就把整次升级挡住。改成
   迁移成功后在状态目录里落一个"迁移已完成"的标记，`bootstrap.py`
   升级时先看这个标记，标记在就完全跳过（不联网、不进子进程、不占锁）。
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
import json
import shutil
import sys
from datetime import datetime

import core

# state_key 形如 "ledger_id|key|node_id" 的三份共享状态文件。
STATE_FILES = ("stage_entered.json", "followup_state.json", "stage_history.json")

# 迁移一旦干净地跑完一次（零冲突、零读取失败），语义上就再也不会需要
# 重跑——rc15 之后的代码从没生产过 rc14 式的旧 key，未来任何新增的
# key_tiebreakers 台账从第一天起就是新 key，没有旧 key 可迁。这个标记
# 让 bootstrap.py 往后每次升级都能跳过一次不必要的联网 + 抢锁。
#
# 常量定义在 core.py（跟 check_followup.py 共用，见那边的说明），这里
# 引出同名别名，不改动本文件里原有的引用写法，也保持 `migrate.
# MIGRATION_MARKER_FILE` 这个外部可见名字不变（测试在用）。
MIGRATION_MARKER_FILE = core.MIGRATION_MARKER_FILE
MIGRATION_ID = core.MIGRATION_ID


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
    ap.add_argument("--recover-only", action="store_true",
                    help="只判断/处理上一次没走完的改写，不做任何新的迁移计划、"
                         "不联网。专给 bootstrap.py 在迁移子进程异常退出"
                         "（含被强杀）之后调用，见 _recover_only() 的说明。")
    args = ap.parse_args()

    # 🔴 锁只在真正写盘时才取——纯读计划不写状态，跟每日任务不存在竞态，
    #    不该为了看一眼计划就去抢锁、跟正在跑的 9 点任务过不去。
    #    --recover-only 本身可能要写（复原/清理），也要取锁。
    need_lock = args.apply or args.recover_only
    lock_path = None
    lock_token = ""
    if need_lock:
        try:
            lock_path, lock_token, steal = core.acquire_lock()
            if steal:
                print(f"⚠️  {steal}", file=sys.stderr)
        except core.LockBusy as e:
            print(f"❌ 抢不到运行锁，本次未执行（{e}）。"
                  f"多半是每日任务正在跑，等它结束后重试。", file=sys.stderr)
            return 1

    try:
        if args.recover_only:
            return _recover_only()
        return _run(apply=args.apply)
    finally:
        if lock_path:
            core.release_lock(lock_path, lock_token)


def _recover_only() -> int:
    """
    只判断并处理"上一次改写有没有走完"，不做任何新的迁移计划、不联网。

    ═══════════════════════════════════════════════════════════════════
    🔴 2026-08-21 第七轮复审指出的 P0：迁移子进程一旦被**强杀**（不是
    异常退出，是收到 SIGKILL 这类连 `except` 都拦不住的信号），进程内
    自己那套"炸了就地复原"的 try/except 完全没有机会执行——`main()`
    的 `finally` 也不会跑，连锁都不会释放。`bootstrap.py` 只看到子进程
    退出码非零（信号杀死的进程 `subprocess.run` 会报出负的信号号），
    直接回滚**代码**。但那时状态可能停在"已经改了一部分"的中间态，
    而**回滚代码这个动作本身完全不知道事务日志的存在**——旧代码
    （被回滚回去的那版）压根没有这套机制，日志会一直孤零零地留在
    盘上，没有任何东西会去处理它。

    这个函数就是补的那个缺口：`bootstrap.py` 在打算回滚代码之前，
    **趁新版代码还没被换掉**，先调用这个模式，把状态判个清楚。

    退出码是给 bootstrap.py 读的机器接口，不是人话——它要拿这个做
    "能不能回滚代码"的判断，不能靠翻日志文字去猜（脆，见坑#12）：

      0 = 本来就没有没走完的改写。可能是这次失败根本没走到
          begin_state_transaction() 那一步（比如联网取数失败），
          状态从头到尾没被碰过，回滚代码是安全的。
      2 = 发现过，且确认那次其实已经完整写完——只是清理日志这最后
          一步没走完，已经补上清理。**状态是新的**，回滚代码会造出
          "状态新、代码旧"，绝不能回滚。
      3 = 发现过，确认没写完，已经照备份把状态整体复原回旧版认得
          的样子。可以放心回滚代码。
      1 = 发现过，但没能可靠复原（备份缺失、或复原本身也写失败）。
          状态到底是新是旧、完不完整，现在判不清——绝不能宣称
          "已安全"，调用方必须整体拒绝、不能让定时任务碰这批状态。
    ═══════════════════════════════════════════════════════════════════
    """
    core.set_read_only(False)
    txn = core.pending_state_transaction()
    if txn is None:
        print("没有发现没走完的状态改写。")
        return 0

    # 🔴 必须在调用 recover（它会改状态）**之前**问这个问题——答案要的是
    #    "崩溃现场当时是什么样"，不是"复原动作跑完之后是什么样"。这个
    #    结论一旦成立就不会再变：它是纯读出来的，"完整"分支本身不改
    #    任何状态文件，只尝试删日志——删不删得掉跟"数据是不是完整的"
    #    是两件独立的事，不能混在一起判。
    was_already_complete = core.state_transaction_matches_expected(txn)

    for line in core.recover_state_transaction():
        print(line)

    # 🔴 2026-08-24 自审发现：这里原来先看"日志删掉了没"，再看
    #    was_already_complete——顺序反了。数据已经确认完整时，"完整"
    #    分支唯一可能失败的动作是删日志本身（finish_state_transaction
    #    返回警告，但不代表数据有问题）；此时日志依然留在盘上，
    #    `pending_state_transaction()` 依然不是 None——如果先判这个，
    #    会把一个已经确认安全的结果，误判成"判不清"，让 bootstrap.py
    #    白白拒绝一次本该正常保留新代码的升级。已知安全的结论必须先判、
    #    且不能被后续单纯的清理失败推翻。
    if was_already_complete:
        print("✅ 上一次改写其实已经完整落地——状态是新的，不需要回滚代码。")
        return 2

    if core.pending_state_transaction() is not None:
        print("❌ 无法可靠判定/复原上一次的状态改写——备份或复原本身出了问题，"
              "状态可能处于不完整状态，需要人工核对（详见上面的日志，以及 "
              "state/.state-transaction.json 里指向的备份目录）。", file=sys.stderr)
        return 1

    print("✅ 上一次改写没有走完，已照备份把状态整体复原——"
          "状态回到了旧版本认得的样子，可以放心回滚代码。")
    return 3


def _run(*, apply: bool) -> int:
    core.set_read_only(not apply)

    # 🔴 先收拾上一次没走完的改写（如果有）。已持锁，是安全的时机。
    #    只读模式下 recover 自己会被闸门挡住，所以单独报一声。
    if apply:
        # 同样的"先问后做"顺序（见 _recover_only()）：数据是不是已经完整，
        # 要在 recover 动手之前就问，答案不能被后面单纯的清理失败推翻。
        pending_before = core.pending_state_transaction()
        was_already_complete = (
            pending_before is not None
            and core.state_transaction_matches_expected(pending_before))
        for line in core.recover_state_transaction():
            print(f"↩️ {line}")
        # 🔴 2026-08-24 自审发现：这里原来只打印日志、不看结果，直接往下走
        #    到 plan_ledger()——如果上面这次 recover 没能彻底解决（备份缺失、
        #    复原本身又写失败，日志仍留着），就会在一份自己都不确定完不完整
        #    的状态上再开一次新的迁移事务，把水搅得更浑。这不是自动化路径
        #    （bootstrap.py 出问题时走的是专门的 --recover-only，已经在那边
        #    正确处理），但人手动重跑这个脚本时也不该被放过去——判不清就
        #    停，跟这一整套机制其余每一处是同一条规矩。"已经确认完整、
        #    只是清理日志失败"不算判不清，不能拦。
        if not was_already_complete and core.pending_state_transaction() is not None:
            print("❌ 上一次状态改写没能彻底复原（见上面的日志），不能在这份"
                  "还不确定完不完整的状态上开始新的迁移。请先人工核对"
                  "state/.state-transaction.json 里指向的备份目录，解决后重跑。",
                  file=sys.stderr)
            return 1
    elif core.pending_state_transaction() is not None:
        print("⚠️  上一次状态改写没走完（存在事务日志）。"
              "本次是只读预览，不做复原；加 --apply 会先复原再继续。",
              file=sys.stderr)

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
        # 🔴 标记只在真正的 --apply 下才落——dry-run 是"永远只读"的硬约束，
        # 哪怕落的是一个空操作也不该碰，即便 write_state 本身会被只读闸门
        # 挡住，语义上也不该在这条分支里去"尝试"写。
        if apply and not had_read_failure:
            # 这条分支没动过任何状态文件，所以标记写失败不会造成"状态新、
            # 代码旧"——非零退出让安装器回滚代码，状态本来就还是旧的，
            # 两边一致。不必开事务，但也不能裸崩成一屏堆栈。
            try:
                _mark_migration_done()
            except Exception as e:
                print(f"\n❌ 写迁移完成标记失败（{type(e).__name__}: {e}）。"
                      f"本次没有改动任何状态文件，重跑即可。", file=sys.stderr)
                return 1
        print("没有发现需要迁移的记录，状态文件与 rc15 主键规则已经一致。")
        return 1 if had_read_failure else 0

    if not apply:
        print("\n以上是计划，未写入任何文件。加 --apply 才会真正改写。")
        return 1 if had_read_failure else 0

    # 🔴 读取失败、改名冲突，都是"这次没法把事情做全"——都必须走同一条
    # 全须全尾的路径：一个字节都不写。冲突原来是"跳过冲突的那条、其余
    # 照常写入"，单独看没问题，但 bootstrap.py 看到非零退出会整体回滚
    # 代码、不会回滚已经写下去的状态，state 停在新 key、代码退回旧版，
    # 旧代码按旧规则找不到这批刚改名的历史记录，当成新项目重新催办——
    # 跟这个脚本要防的 P0 一模一样，只是换了个触发路径。见模块 docstring。
    if had_read_failure or had_conflict:
        reason = "有台账取数失败" if had_read_failure else "有记录改名遇到冲突"
        print(f"\n❌ {reason}，为避免部分写入、跟安装器的代码回滚凑成半写状态，"
              f"本次不写入任何文件。请解决后重跑。", file=sys.stderr)
        return 1

    backup_dir = core.state_dir() / f".migrate-rc15-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    # 🔴 改完之后每个文件该长什么样，**在动手之前就全部算出来**。
    #    两个理由：
    #      ① 完成标记必须一起进这批文件。否则「状态和标记都写完了、但删
    #         事务日志之前被打断」时，恢复会把状态退回迁移前、标记却留着
    #         说"已迁移"，迁移再也不会重跑 —— 又一个状态与代码不一致。
    #      ② 事务日志要记下这批"应有内容"的指纹，恢复时才分得清
    #         「没改完」和「改完了只是没删掉日志」这两种相反的情况。
    #         标记里的时间戳因此也必须在这里定死，不能等到写的时候再取，
    #         否则算出来的指纹跟实际写下去的对不上。
    marker = core.read_state(MIGRATION_MARKER_FILE)
    marker[MIGRATION_ID] = core.now_iso()
    final: dict[str, dict] = {name: state[name] for name in STATE_FILES}
    final.update({snapshot_file_name(lid): snap for lid, snap in snapshots.items()})
    final[MIGRATION_MARKER_FILE] = marker

    for name in final:
        src = core.state_dir() / name
        if src.exists():
            shutil.copy2(src, backup_dir / name)
    print(f"\n已备份改写前的状态文件到 {backup_dir}")

    # 🔴 备份做完之后、第一次改写之前，先宣告「要开始改了」。从这一刻起，
    #    不管在哪一步被打断（异常、断电、被强杀、连下面的恢复动作自己
    #    失败），盘上都留着事务日志，下一个碰状态的进程会照它处理。
    #    见 core.py 里「状态改写事务日志」那一段。
    try:
        core.begin_state_transaction(backup_dir, final)
    except Exception as e:
        # 🔴 日志自己都没写成，就绝不能开始改 —— 真改了再炸，盘上没有
        #    任何痕迹说明"有一次改写没走完"，恢复机制整个失灵。
        #    此刻一个字节都还没动，直接退出就是安全的。
        print(f"\n❌ 写事务日志失败（{type(e).__name__}: {e}），"
              f"未改动任何状态文件。请检查状态目录是否可写后重跑。", file=sys.stderr)
        return 1

    try:
        for name, data in final.items():
            core.write_state(name, data)
        print("已写入迁移后的状态文件。")
    except Exception as e:
        # 就地复原一次。**即便这次复原自己也失败**，事务日志仍然留在盘上，
        # 下一次运行（迁移重跑或每日任务启动）会再试一次 —— 这正是加日志
        # 要买的东西：不再要求"预先想到会在哪炸"。
        for line in core.recover_state_transaction():
            print(f"    {line}")
        print(f"\n❌ 写入状态文件时失败（{type(e).__name__}: {e}），"
              f"已按事务日志把状态整体复原，没有留下半写状态。", file=sys.stderr)
        return 1

    # 🔴 删日志失败**不算迁移失败**：状态已经完整落地了，此时返回非零会
    #    让安装器回滚代码，反而制造出"状态新、代码旧"。只把警告打出来。
    for line in core.finish_state_transaction():
        print(line, file=sys.stderr)
    return 0


def _mark_migration_done() -> None:
    marker = core.read_state(MIGRATION_MARKER_FILE)
    marker[MIGRATION_ID] = core.now_iso()
    core.write_state(MIGRATION_MARKER_FILE, marker)


if __name__ == "__main__":
    sys.exit(main())
