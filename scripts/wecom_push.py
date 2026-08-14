#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
企微群机器人推送（零第三方依赖，仅标准库）。

═══════════════════════════════════════════════════════════════════════
定位：**主通知通道**。业务只从这里收清单。

这条通道的成败直接决定「能不能把项目记为已通知」——
推没出去却记成已通知，业务会静默 7 天收不到催办，而这看起来和
「今天没有超时单」一模一样。所以 push() 必须如实返回发了几片、
成没成功，绝不用一个裸 bool 把「没启用」和「发失败」混为一谈。
═══════════════════════════════════════════════════════════════════════

硬闸门：--dry-run / --today / --json 一律不真发。
      对外可见的副作用一旦发出就收不回，而且打扰的是真人。
      做降噪回归时会反复跑 --today 2026-08-06 这类命令，没有闸门
      业务会莫名收到一堆「未来的」催办。

技术约束（已实测）：
  · text 类型上限 2048 字节，31 条实测 3264 字节装不下 → 用 markdown_v2（4096）
  · 群机器人每分钟最多 20 条 → 拆条时每条间隔 1 秒
  · qyapi.weixin.qq.com 是国内域名，不需要代理
"""

from __future__ import annotations  # 兼容 Python 3.9（macOS 自带版本）

import json
import sys
import time
import urllib.error
import urllib.request

import core
import nethttp

# 留 96 字节余量，避免 emoji/换行的字节数估算偏差踩线
DEFAULT_SPLIT_BYTES = 4000
HARD_LIMIT = 4096
RATE_SLEEP = 1.0

# 企微支持的消息类型。markdown_v2 才有列表/表格/分割线；
# 旧版 markdown 只有标题/加粗/链接/引用/行内代码 + 3 种颜色，没有列表——
# 那正是清单读起来「像一堵墙」的原因。
# text 上限 2048 字节，两种 markdown 都是 4096。
SUPPORTED_MSGTYPES = frozenset({"text", "markdown", "markdown_v2"})

# 单行硬拆时优先在这些字符后断开，尽量不把一个词劈成两半
_SOFT_BREAKS = "、，,；;　 ·—-）)】]」》"


class PushResult:
    """
    一次推送的结果。

    调用方要靠它区分三件**性质完全不同**的事，不能用 bool 糊在一起：
      · attempted=False → 根本没试（没启用 / 没内容 / 被闸门拦下）
      · ok=False        → 试了，没成或只成了一部分 ← 这是故障，必须告警且不许提交状态
      · ok=True         → 全部分片确认送达 ← 只有这一种才允许记「已通知」
    """

    def __init__(self, *, attempted: bool, ok: bool | None = None,
                 sent: int = 0, total: int = 0,
                 errors: list[str] | None = None, skipped_reason: str = ""):
        self.attempted = attempted
        self.ok = ok
        self.sent = sent
        self.total = total
        self.errors = errors or []
        self.skipped_reason = skipped_reason

    @property
    def partial(self) -> bool:
        return self.attempted and 0 < self.sent < self.total

    def summary(self) -> str:
        if not self.attempted:
            return f"未推送（{self.skipped_reason}）"
        if self.ok:
            return f"已推送 {self.sent}/{self.total} 条"
        head = "部分失败" if self.partial else "全部失败"
        return f"{head}：{self.sent}/{self.total} 条成功；" + "；".join(self.errors[:3])


def _webhook_url() -> str | None:
    """
    从 <运行时目录>/.env 读 webhook 地址。

    这个地址等同于密码——拿到就能往那个群里发消息，所以只放 .env，
    不进配置文件、不进 skill 包、任何情况下不打印。
    """
    return core.read_env("FOLLOWUP_WECOM_WEBHOOK")


def _bytes(s: str) -> int:
    return len(s.encode("utf-8"))


def _prefix_for(total: int) -> str:
    """分片序号前缀。只有多片时才有。"""
    return f"（{total}/{total}）\n" if total > 1 else ""


def _hard_wrap(line: str, limit: int) -> list[str]:
    """
    单行本身就超限时的安全拆分。

    🔴 旧实现在这里直接放行原样返回，实测 4500 字节的单行会被整片发出去，
       超过企微 4096 的硬上限，整条消息被拒。

    优先在标点/空格处断开；实在找不到分隔符才按**码点**边界断
    （绝不按字节切——那会把一个汉字劈成两半变乱码）。
    断点补「…」表示还有后续。

    实际会走到这里的基本只有数据质量告警行（可能拼接大量序号）；
    项目条目最长也就几十字节，永远够。
    """
    out: list[str] = []
    rest = line
    while _bytes(rest) > limit:
        # 逐码点累加，找出能塞进 limit 的最长前缀（给「…」留 3 字节）
        budget = limit - 3
        cut = 0
        used = 0
        for i, ch in enumerate(rest):
            w = _bytes(ch)
            if used + w > budget:
                break
            used += w
            cut = i + 1
        if cut <= 0:
            cut = 1  # 极端情况：limit 比一个字符还小，保证前进，不死循环
        # 往回找一个软断点，别把词劈开（只在最后 20 个字符里找，避免切太碎）
        soft = -1
        for i in range(cut - 1, max(cut - 21, 0) - 1, -1):
            if rest[i] in _SOFT_BREAKS:
                soft = i + 1
                break
        if soft > 0:
            cut = soft
        out.append(rest[:cut] + "…")
        rest = rest[cut:]
    if rest:
        out.append(rest)
    return out


def _split_once(text: str, limit: int) -> list[str]:
    """按 空行 → 行 → 硬拆 三级拆分，保证每片 ≤ limit。"""
    if _bytes(text) <= limit:
        return [text]

    chunks: list[str] = []
    cur = ""

    def flush() -> None:
        nonlocal cur
        if cur:
            chunks.append(cur)
            cur = ""

    def add_line(ln: str) -> None:
        """把一行塞进当前片，塞不下就换片；行本身超限则硬拆。"""
        nonlocal cur
        if _bytes(ln) > limit:
            flush()
            chunks.extend(_hard_wrap(ln, limit))
            return
        cand = f"{cur}\n{ln}" if cur else ln
        if _bytes(cand) <= limit:
            cur = cand
        else:
            flush()
            cur = ln

    # 先按空行切成块（每块通常是一个阶段），块之间保留空行分隔
    for blk in text.split("\n\n"):
        cand = f"{cur}\n\n{blk}" if cur else blk
        if _bytes(cand) <= limit:
            cur = cand
            continue
        flush()
        if _bytes(blk) <= limit:
            cur = blk
        else:
            for ln in blk.split("\n"):
                add_line(ln)
    flush()
    return chunks


def split_message(text: str, limit: int = DEFAULT_SPLIT_BYTES) -> list[str]:
    """
    拆成多条，**保证每条加上「第几条」前缀之后仍不超限**。

    🔴 旧实现的第二个洞：前缀是拆完之后才加的，实测 limit=4000 时
       两片各 4009 字节。默认配置下 4009 < 4096 侥幸没炸，但把
       split_bytes 调到 4096 就会被企微直接拒收。

    两遍法：先不算前缀拆一次得到片数 n，再按 (limit − 前缀长度) 重拆。
    片数变多会让前缀更长、可用空间更小，是单调的，几轮就收敛。

    **绝不截断内容** —— 业务明确要求每笔项目都要看到，
    截断等于替她决定哪些不用看。
    """
    limit = min(int(limit), HARD_LIMIT)
    if limit <= 0:
        raise ValueError(f"拆分上限必须为正数，收到 {limit}")

    chunks = _split_once(text, limit)
    for _ in range(5):
        need = _bytes(_prefix_for(len(chunks)))
        if need == 0:
            break
        eff = limit - need
        if eff <= 0:
            raise ValueError(f"拆分上限 {limit} 太小，装不下分片前缀")
        again = _split_once(text, eff)
        if len(again) == len(chunks):
            chunks = again
            break
        chunks = again
    return chunks


def render_chunks(text: str, limit: int) -> list[str]:
    """拆分 + 加前缀 + 最终断言。发出去的就是这里返回的东西。"""
    chunks = split_message(text, limit)
    prefix_total = len(chunks)
    out = []
    for i, c in enumerate(chunks):
        out.append(f"（{i + 1}/{prefix_total}）\n{c}" if prefix_total > 1 else c)
    # 🔴 最后一道闸：宁可整批失败并告警，也不发一条会被企微拒收的消息。
    for i, m in enumerate(out):
        n = _bytes(m)
        if n > min(limit, HARD_LIMIT):
            raise ValueError(
                f"第 {i + 1}/{len(out)} 片 {n} 字节，超过上限 "
                f"{min(limit, HARD_LIMIT)}。拆分逻辑有 bug，拒绝发送。"
            )
    return out


def _post(url: str, payload: dict) -> tuple[bool, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        # 走 nethttp：TLS 1.3 被中间层搞坏时自动降到 1.2。见该模块头注。
        # 2026-08-14 就是这条链路上 0/1 条失败，业务没收到当天的清单。
        # 🔴 idempotent=False：这是「发出去就收不回」的调用。仍然会降级重试
        #    （宁可重复，也不静默漏催），但响应阶段失败后的重试会额外提示
        #    可能重复，业务真收到两条时原因在日志里查得到。
        data = json.loads(
            nethttp.fetch(req, timeout=20, idempotent=False)
            .decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if data.get("errcode") != 0:
        # 常见：93000 webhook 无效 / 45009 频率超限 / 40001 key 错误
        return False, f"errcode={data.get('errcode')} {data.get('errmsg')}"
    return True, "ok"


def push(text: str, output_cfg: dict, *, allowed: bool, stream=None) -> PushResult:
    """
    把清单推到企微群。

    allowed：调用方给的闸门。False 时只打印「本该发什么」，绝不真发。
             --dry-run / --today / --json 都应传 False。

    **返回 PushResult，调用方必须处理。** 这里不再吞异常：
    拆分算法出 bug、URL 缺失都会如实反映在结果里，由调用方决定
    「不提交状态 + 非零退出 + 告警」这一整套。
    """
    out = stream or sys.stderr
    cfg = (output_cfg or {}).get("wecom_webhook") or {}

    if not cfg.get("enabled"):
        return PushResult(attempted=False, skipped_reason="配置里未启用企微通道")
    if not text.strip():
        return PushResult(attempted=False, skipped_reason="没有内容要发")

    url = _webhook_url()
    if not url:
        msg = "企微推送已启用，但 <运行时目录>/.env 里没有 FOLLOWUP_WECOM_WEBHOOK"
        print(f"🔴 {msg}", file=out)
        return PushResult(attempted=True, ok=False, sent=0, total=0, errors=[msg])

    limit = int(cfg.get("split_bytes") or DEFAULT_SPLIT_BYTES)
    try:
        messages = render_chunks(text, limit)
    except ValueError as e:
        msg = f"消息拆分失败，未发送任何内容：{e}"
        print(f"🔴 {msg}", file=out)
        return PushResult(attempted=True, ok=False, sent=0, total=0, errors=[msg])

    if not allowed:
        print(f"\n🔒 企微推送已拦截（试跑模式）—— 本该发 {len(messages)} 条消息、"
              f"共 {_bytes(text)} 字节到业务群", file=out)
        print("   （--dry-run / --today / --json 一律不真发，避免业务收到测试消息）",
              file=out)
        return PushResult(attempted=False, total=len(messages),
                          skipped_reason="试跑模式闸门")

    msgtype = cfg.get("msgtype") or "markdown_v2"
    if msgtype not in SUPPORTED_MSGTYPES:
        print(f"⚠️ 不认识的 msgtype {msgtype!r}，回退到 markdown_v2。"
              f"可选：{sorted(SUPPORTED_MSGTYPES)}", file=out)
        msgtype = "markdown_v2"

    ok_n = 0
    errors: list[str] = []
    for i, chunk in enumerate(messages):
        # 企微三种类型的 payload 内层 key 名与 msgtype 一致，可以通用构造。
        # 别写成 if/else 二分——那样 markdown_v2 会掉进 text 分支，
        # 消息会带着满屏 ** # - 原样发出去。
        payload = {"msgtype": msgtype, msgtype: {"content": chunk}}
        ok, msg = _post(url, payload)
        if ok:
            ok_n += 1
        else:
            errors.append(f"第 {i + 1}/{len(messages)} 条：{msg}")
            print(f"⚠️ 企微推送第 {i + 1}/{len(messages)} 条失败：{msg}", file=out)
        if i < len(messages) - 1:
            time.sleep(RATE_SLEEP)  # 群机器人每分钟上限 20 条

    if ok_n == len(messages):
        print(f"📨 企微已推送 {ok_n} 条消息到业务群", file=out)
        return PushResult(attempted=True, ok=True, sent=ok_n, total=len(messages))

    print(f"🔴 企微推送不完整：{ok_n}/{len(messages)} 条成功。"
          f"业务这次收到的清单是残缺的，本次不会记为已通知。", file=out)
    return PushResult(attempted=True, ok=False, sent=ok_n,
                      total=len(messages), errors=errors)
