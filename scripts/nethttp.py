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

🔴 为什么捕 ssl.SSLError 而不是只认 BAD_RECORD_MAC：
   坏掉的中间层不止一种报法（还见过 SSLEOFError）。只认一种字符串，
   换个报法就退回「连不上」，而那会伪装成「今天没有要催的」。
   降级失败照样抛原异常，力度没减。
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import ssl
import sys
import urllib.request

# 本进程内是否已经降级。降级过一次就粘住，别让后面每一个请求
# （9 份台账 × 各自的重试）都先赔一次失败的 1.3 握手。
_degraded = False


def degraded() -> bool:
    """本进程这一趟有没有降级到 TLS 1.2。给自检/日志用。"""
    return _degraded


def _tls12_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    # 只封顶，不降低验证要求：证书校验、主机名校验都照旧。
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def urlopen(req, timeout, *, stream=None):
    """
    `urllib.request.urlopen` 的替身：TLS 失败时降到 1.2 再试一次。

    非 TLS 的错误（HTTPError、超时、DNS）一律原样抛出 —— 这个函数
    只管传输层握手，不吞任何业务错误，调用方原有的重试逻辑不受影响。
    """
    global _degraded
    out = stream or sys.stderr

    if _degraded:
        return urllib.request.urlopen(req, timeout=timeout,
                                      context=_tls12_context())
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except ssl.SSLError as first:
        try:
            resp = urllib.request.urlopen(req, timeout=timeout,
                                          context=_tls12_context())
        except Exception:
            raise first          # 降级也不行 —— 报原来那个错，别掩盖真实原因
        _degraded = True
        print(f"⚠️ TLS 1.3 握手失败（{type(first).__name__}: {first}），"
              f"本次运行已降级到 TLS 1.2。"
              f"这通常是本机代理/VPN 搞坏了 TLS 1.3；程序能继续跑，"
              f"但值得查一下网络。", file=out)
        return resp
