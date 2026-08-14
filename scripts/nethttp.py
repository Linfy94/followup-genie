#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
发 HTTPS 请求时的传输层兜底。**腾讯文档读取与企微推送共用这一份。**

═══════════════════════════════════════════════════════════════════════
🔴 为什么要单独成一个模块：TLS 1.3 在这台机器上会被中间层搞坏。

2026-08-14 实测（每格 5~10 次，curl 与 Python 表现完全一致）：

              TLS 1.3    TLS 1.2
  腾讯文档       0        全通
  企微文档       0        全通
  企微推送       0        全通
  飞书          0        全通
  Google        0        全通      ← 连它都断，说明不是某一家的事

业务电脑上必须常开代理，代理把所有 TLS 1.3 记录搞坏，报
`SSLV3_ALERT_BAD_RECORD_MAC`。**关代理不是可选项**，所以只能程序这边扛。

为什么以前没发作：系统 Python 3.9 用的是 LibreSSL 2.8.3，**根本不支持
TLS 1.3**，只能协商 1.2。而 cron 实际跑在 hermes 自带的
Python 3.11 + OpenSSL 3.5.7 上，它会优先选 TLS 1.3 —— 网络一坏就中招。
当天的表现是：读数正常（2512 行），**推送 0/1 条失败，业务没收到清单**。

🔴 为什么是「先试后降」而不是直接写死 1.2：
   写死等于永久降级，网络修好了也不会自己回到 1.3，而且没人会记得改回来。
   这里每个进程只付一次失败握手的代价（降级后本进程内粘住），
   进程重启就重新试 1.3 —— 网络恢复当天自动回到 1.3，不需要任何人动手。

═══════════════════════════════════════════════════════════════════════
🔴 rc3 在这里踩过一个「只对了一半」的坑，务必别改回去。

rc3 只捕裸 `ssl.SSLError`，而且把 `resp.read()` 留在调用方。实测下来
这只覆盖了三种失败形态里的一种 —— 当天恰好命中的那一种，
所以真机验证 10/10 通过，看起来像是修好了。

`urllib.request.AbstractHTTPHandler.do_open` 的骨架（实测确认）：

    try:
        try:
            h.request(...)          ← 连接 / 握手 / 发请求体
        except OSError as err:
            raise URLError(err)     ← **只有这一段被包装**
        r = h.getresponse()         ← 读响应头，异常原样抛出
    except:
        raise

于是同一个 bad record mac 有三种长相：

  失败位置            抛出                       rc3
  连接/握手/发请求    URLError(reason=SSLError)   ❌ 漏掉（ssl.SSLError 是 OSError 子类）
  读响应头            裸 ssl.SSLError             ✅ 当天命中的就是它
  读响应体            裸 ssl.SSLError（在模块外）  ❌ 漏掉

**所以 read() 必须收进这个模块**，否则「读到一半断掉」既不降级、
也只会表现成调用方的普通重试失败。

🔴 幂等性：请求阶段失败 vs 响应阶段失败，重试的安全性完全不同。
   · 请求阶段（URLError 包装）：请求没发完整，服务端不可能处理过 → 重试绝对安全
   · 响应阶段（裸 SSLError）：请求已完整送达，服务端**可能已经处理**
     → 对企微推送这种「发出去就收不回」的调用，重试有让业务收到重复清单的风险

   两害相权仍然重试，因为这是本项目一以贯之的取舍：
   **「重复消息业务能识别，静默漏催她发现不了」**（见 wecom_push.push 的注释）。
   但重试要**说出来**，业务真收到两条时，原因在日志里查得到。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import ssl
import sys
import urllib.error
import urllib.request

# 本进程内是否已经降级。降级过一次就粘住，别让后面每一个请求
# （9 份台账 × 各自的重试）都先赔一次失败的 1.3 握手。
_degraded = False


def degraded() -> bool:
    """本进程这一趟有没有降级到 TLS 1.2。给自检/日志用。"""
    return _degraded


def reset() -> None:
    """只给测试用：把粘滞标志归零。"""
    global _degraded
    _degraded = False


def _tls12_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    # 只封顶协议版本，不降低验证要求：证书校验、主机名校验都照旧。
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def tls_failure_phase(exc: BaseException) -> str | None:
    """
    这个异常是不是 TLS 传输层失败；是的话发生在哪个阶段。

    返回 "request"（请求阶段，重试绝对安全）、"response"（响应阶段，
    服务端可能已处理）或 None（不是传输层问题，不该在这里重试）。
    """
    # HTTPError 是「服务端好好地回了个错误码」，传输层没问题。
    # 它是 URLError 的子类，必须先判，否则会被下面那条吞掉，
    # 让 401/403 这类凭证问题被当成网络抖动重试掉。
    if isinstance(exc, urllib.error.HTTPError):
        return None
    if isinstance(exc, urllib.error.URLError):
        return "request" if isinstance(exc.reason, ssl.SSLError) else None
    if isinstance(exc, ssl.SSLError):
        return "response"
    return None


def _once(req, timeout, ctx) -> bytes:
    """发一次并把响应体读完 —— read() 必须在这里面，见模块头注。"""
    if ctx is None:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read()


def fetch(req, timeout, *, idempotent: bool, stream=None) -> bytes:
    """
    发一次请求并返回响应体。TLS 失败时降到 1.2 重试一次。

    idempotent：这个请求重发一次是否无害。
        True  —— 读取类调用（腾讯文档 JSON-RPC）
        False —— 会产生对外副作用的调用（企微推送）。仍然会重试，
                 但「响应阶段失败后重试」会额外提示可能重复。

    非 TLS 的错误（HTTPError、超时、DNS）一律原样抛出 —— 这个函数
    只管传输层，不吞任何业务错误，调用方原有的重试逻辑不受影响。
    """
    global _degraded
    out = stream or sys.stderr

    if _degraded:
        return _once(req, timeout, _tls12_context())

    try:
        return _once(req, timeout, None)
    except BaseException as first:
        phase = tls_failure_phase(first)
        if phase is None:
            raise
        try:
            data = _once(req, timeout, _tls12_context())
        except BaseException:
            # 降级也不行 —— 报**最初**那个错。当天正是靠原始错误码
            # 定位到代理的；换成第二次的错，排查方向整个偏掉。
            raise first
        _degraded = True
        print(f"⚠️ TLS 1.3 失败（{type(first).__name__}: {first}），"
              f"本次运行已降级到 TLS 1.2。"
              f"这通常是本机代理/VPN 搞坏了 TLS 1.3；程序能继续跑，"
              f"但值得查一下网络。", file=out)
        if phase == "response" and not idempotent:
            print("   ⚠️ 这次失败发生在读响应阶段，请求已完整送达 —— "
                  "服务端可能已经处理过一次，业务或许会收到重复内容。"
                  "（本项目一贯取舍：宁可重复，也不静默漏催）", file=out)
        return data
