# -*- coding: utf-8 -*-
"""统一错误类型与脱敏工具（合并三项目的 redact / classify 逻辑）。"""
from __future__ import annotations

import re
from typing import Any, Dict

_SECRET_KEY_RE = re.compile(
    r"cookie|token|secret|csrf|auth|session|sign|device|web_id|webid|tdid|uid|sid|password",
    re.I,
)
_SECRET_INLINE_RE = re.compile(
    r"(?i)((?:x-signature|a_bogus|mstoken|uifid|verifyfp|sessionid|session_key|"
    r"device_id|web_id|webid|tea_web_id|v2_spipe_web_id|tdid|authorization|sign|"
    r"access_token|refresh_token)\s*[=:])[^&;\"\s]+"
)
_SECRET_JSON_RE = re.compile(
    r'(?i)((?:"|\b)(?:session_key|access_token|refresh_token|authorization|token|'
    r'secret_access_key|device_id|sessionid|sid_tt)(?:"|)\s*:\s*"?)[^",}\s]+'
)


class UpstreamError(RuntimeError):
    """上游/传输错误。error_class 见 channels.base.ERROR_CLASSES。"""

    def __init__(self, message: str, error_class: str = "upstream",
                 status: int | None = None, payload: Any = None):
        super().__init__(redact_text(message))
        self.error_class = error_class
        self.status = status
        self.payload = redact(payload) if payload is not None else None


class PlatformError(RuntimeError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def redact_text(value: Any) -> str:
    s = str(value)
    s = _SECRET_INLINE_RE.sub(r"\1<REDACTED>", s)
    s = _SECRET_JSON_RE.sub(r"\1<REDACTED>", s)
    return s


def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: ("<REDACTED>" if _SECRET_KEY_RE.search(str(k)) else redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


def classify_error(code: Any, message: str) -> str:
    """通用错误分类；渠道可覆写以识别自家错误码（如 jimeng 1006/1310）。"""
    c = str(code or "")
    m = str(message or "").lower()
    if c in {"1006", "1007", "402"} or any(x in m for x in ("credit", "quota", "积分", "权益", "余额不足")):
        return "quota"
    if c in {"401", "403", "4013"} or any(x in m for x in ("login", "登录", "unauthorized", "risk", "风控", "expired")):
        return "auth_or_risk"
    if c in {"1310", "429"} or any(x in m for x in ("parallel", "并发", "rate limit", "too many")):
        return "concurrency"
    if "timeout" in m or "超时" in m:
        return "timeout"
    if any(x in m for x in ("content", "policy", "违规", "审核", "sensitive")):
        return "content"
    return "upstream"


def find_values(obj: Any, keys: set) -> list:
    """递归抽取指定 key 的值（三项目共用的 payload 挖掘工具）。"""
    out: list = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in keys and isinstance(v, (str, int, float)) and v != "":
                out.append(v)
            out += find_values(v, keys)
    elif isinstance(obj, list):
        for v in obj:
            out += find_values(v, keys)
    return out
