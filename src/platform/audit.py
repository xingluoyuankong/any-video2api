# -*- coding: utf-8 -*-
"""脱敏审计：只记结构与头名，不落任何 token / cookie / 签名值。"""
from __future__ import annotations

from typing import Any, Dict, Iterable

from src.core.errors import redact
from src.platform.db import db, now, jd


def _shape(obj: Any, depth: int = 0) -> Any:
    """把 payload 压成"形状"：只保留 key 与类型，丢弃所有值。"""
    if depth > 6:
        return "..."
    if isinstance(obj, dict):
        return {str(k): _shape(v, depth + 1) for k, v in list(obj.items())[:64]}
    if isinstance(obj, list):
        return [_shape(obj[0], depth + 1)] if obj else []
    return type(obj).__name__


def _header_names(headers: Any) -> list:
    if isinstance(headers, dict):
        return sorted(str(k) for k in headers.keys())
    return []


def write(direction: str, channel: str = "", account_id: str = "", method: str = "",
          url: str = "", status: int = 0, body: Any = None, headers: Any = None) -> None:
    try:
        db.execute(
            "INSERT INTO audit_log(ts,channel,account_id,direction,method,url,status,body_shape,headers_shape)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (now(), channel, account_id, direction, method, _safe_url(url), int(status or 0),
             jd(_shape(body)), jd(_header_names(headers))), True)
    except Exception:
        pass


def _safe_url(url: str) -> str:
    try:
        from urllib.parse import urlparse, urlunparse
        p = urlparse(str(url))
        return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))
    except Exception:
        return str(url)[:300]


def recent(limit: int = 100, channel: str = "") -> list:
    if channel:
        return db.all("SELECT * FROM audit_log WHERE channel=? ORDER BY id DESC LIMIT ?", (channel, int(limit)))
    return db.all("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (int(limit),))
