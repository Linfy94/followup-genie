#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
真双进程竞态测试用的 worker。

**必须单独成模块**：macOS 上 multiprocessing 默认用 spawn，
子进程是全新解释器，只能 import 顶层可导入的函数 ——
测试类里的闭包和局部函数都传不过去。

子进程从环境变量继承 `FOLLOWUP_HOME`，所以它和父进程操作的是同一个锁文件，
但走的是完全独立的进程与解释器 —— 这才是真的并发，
同一进程里顺序调两次 acquire_lock 测不出任何竞态。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


def grab(barrier, queue, hold: bool = False):
    """
    在 barrier 上对齐后同时抢锁，把结果丢回队列。

    barrier 是关键：没有它，四个进程的启动时间差着几十毫秒，
    第一个早就拿到锁了，后面三个走的是「锁存在且新鲜」这条平凡路径，
    测不到真正的竞争窗口。
    """
    import core   # noqa: E402 —— spawn 出来的新解释器要自己 import

    try:
        barrier.wait(timeout=30)
    except Exception as e:  # noqa: BLE001
        queue.put(("barrier-failed", f"{type(e).__name__}: {e}"))
        return

    try:
        path, token, steal = core.acquire_lock()
    except core.LockBusy as e:
        queue.put(("busy", str(e)))
        return
    except Exception as e:  # noqa: BLE001
        queue.put(("error", f"{type(e).__name__}: {e}"))
        return

    if hold:
        # 不释放，留给父进程检查锁文件内容
        queue.put(("ok", token))
        return

    core.release_lock(path, token)
    queue.put(("ok", token))


def release_someone_elses(queue, fake_token: str):
    """
    拿着一个**不属于自己**的 token 去释放锁。

    模拟的是：A 的锁被 B 夺走后，A 跑完照样来删锁。
    不核对 token 的话它会把 B 的锁删掉，于是 C 进来又拿到锁 —— 并发保护归零。
    """
    import core   # noqa: E402

    core.release_lock(core.state_dir() / core.LOCK_FILE, fake_token)
    queue.put(("released", fake_token))


def hold_metadata_guard(entered, release, queue):
    """持有锁元数据保护，供父进程确定性验证第二个进程不能同时进入。"""
    import core   # noqa: E402

    try:
        with core._lock_metadata_guard():
            entered.set()
            if not release.wait(timeout=30):
                queue.put(("error", "等待父进程释放信号超时"))
                return
            time.sleep(0.05)
        queue.put(("ok", "released"))
    except Exception as e:  # noqa: BLE001
        queue.put(("error", f"{type(e).__name__}: {e}"))


def enter_metadata_guard(entered, queue):
    """尝试进入锁元数据保护；进入后立即报告。"""
    import core   # noqa: E402

    try:
        with core._lock_metadata_guard():
            entered.set()
        queue.put(("ok", "entered"))
    except Exception as e:  # noqa: BLE001
        queue.put(("error", f"{type(e).__name__}: {e}"))
