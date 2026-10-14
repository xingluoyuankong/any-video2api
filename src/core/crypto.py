# -*- coding: utf-8 -*-
"""统一凭据加密（合并 jimeng2api / coze2api 的 DPAPI + XOR 回退）。

Windows 用 DPAPI（CryptProtectData），非 Windows 退化为本地 XOR。
密文带命名空间前缀，便于未来算法迁移。
"""
from __future__ import annotations

import base64
import os
import sys

PREFIX = "dpapi:v1:"
XOR_PREFIX = "xor:v1:"


def protect(value: str, namespace: str = "any-video2api") -> str:
    raw = (value or "").encode("utf-8")
    if sys.platform == "win32":
        try:
            import win32crypt  # type: ignore
            blob = win32crypt.CryptProtectData(raw, namespace, None, None, None, 0)[1]
            return PREFIX + base64.b64encode(blob).decode()
        except Exception:
            pass
    key = (os.getenv("AV2A_DEV_KEY") or os.getenv("UNIFIED_DEV_KEY") or f"{namespace}-local-key").encode()
    enc = bytes(b ^ key[i % len(key)] for i, b in enumerate(raw))
    return XOR_PREFIX + base64.b64encode(enc).decode()


def unprotect(value: str, namespace: str = "any-video2api") -> str:
    if not value:
        return ""
    if value.startswith(PREFIX) and sys.platform == "win32":
        import win32crypt  # type: ignore
        return win32crypt.CryptUnprotectData(
            base64.b64decode(value[len(PREFIX):]), None, None, None, 0
        )[1].decode("utf-8")
    if value.startswith(XOR_PREFIX):
        key = (os.getenv("AV2A_DEV_KEY") or os.getenv("UNIFIED_DEV_KEY") or f"{namespace}-local-key").encode()
        enc = base64.b64decode(value.split(":", 2)[2])
        return bytes(b ^ key[i % len(key)] for i, b in enumerate(enc)).decode("utf-8")
    return value
