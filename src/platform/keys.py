# -*- coding: utf-8 -*-
"""对外 API Key 管理（合并 tiktok2api.core.auth 的多 key 设计，加渠道白名单）。

Key 格式：`u2a_<prefix>_<secret>`，落库存 sha256 + 明文（单机反代，便于 reveal）。
"""
from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any, Dict, List, Optional

from src.platform.db import db, now, jd, jl

PUBLIC_PATHS = {"/", "/health", "/docs", "/openapi.json", "/redoc", "/console", "/favicon.ico"}


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class ApiKeyStore:
    def __init__(self):
        self._last_persist = 0.0

    def create(self, name: str = "client", note: str = "", rpm_limit: int = 120,
               max_concurrent: int = 8, allowed_channels: Optional[List[str]] = None) -> Dict[str, Any]:
        prefix = secrets.token_hex(4)
        secret = secrets.token_urlsafe(24)
        full = f"u2a_{prefix}_{secret}"
        kid = secrets.token_hex(8)
        db.execute(
            "INSERT INTO api_keys(id,name,prefix,secret_hash,secret_plain,enabled,created_at,"
            "rpm_limit,max_concurrent,allowed_channels,note) VALUES(?,?,?,?,?,1,?,?,?,?,?)",
            (kid, name, prefix, _hash(full), full, now(), rpm_limit, max_concurrent,
             jd(allowed_channels or []), note), True)
        return {"id": kid, "name": name, "api_key": full, "prefix": prefix,
                "hint": f"u2a_{prefix}_***", "rpm_limit": rpm_limit,
                "max_concurrent": max_concurrent, "allowed_channels": allowed_channels or []}

    def list(self) -> List[Dict[str, Any]]:
        rows = db.all("SELECT * FROM api_keys ORDER BY created_at DESC")
        for r in rows:
            r.pop("secret_hash", None)
            r.pop("secret_plain", None)
            r["enabled"] = bool(r.get("enabled"))
            r["allowed_channels"] = jl(r.get("allowed_channels"), [])
            r["hint"] = f"u2a_{r.get('prefix')}_***"
        return rows

    def reveal(self, kid: str) -> Optional[str]:
        r = db.one("SELECT secret_plain FROM api_keys WHERE id=?", (kid,))
        return r.get("secret_plain") if r else None

    def delete(self, kid: str) -> bool:
        db.execute("DELETE FROM api_keys WHERE id=?", (kid,), True)
        return True

    def set_enabled(self, kid: str, enabled: bool) -> Optional[dict]:
        db.execute("UPDATE api_keys SET enabled=? WHERE id=?", (1 if enabled else 0, kid), True)
        r = db.one("SELECT * FROM api_keys WHERE id=?", (kid,))
        if r:
            r.pop("secret_hash", None)
            r.pop("secret_plain", None)
        return r

    def authenticate(self, raw: Optional[str]) -> Optional[dict]:
        token = (raw or "").strip()
        if not token:
            return None
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        h = _hash(token)
        for k in db.all("SELECT * FROM api_keys WHERE enabled=1"):
            if k.get("secret_hash") == h or (k.get("secret_plain") and k["secret_plain"] == token):
                # 降低写频率
                if time.time() - self._last_persist >= 60:
                    self._last_persist = time.time()
                    try:
                        db.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (now(), k["id"]), True)
                    except Exception:
                        pass
                k["allowed_channels"] = jl(k.get("allowed_channels"), [])
                return k
        return None

    def ensure_bootstrap(self) -> Optional[str]:
        """首次启动自动生成一把 key，返回明文（只此一次）。"""
        if db.one("SELECT id FROM api_keys LIMIT 1"):
            return None
        created = self.create("default", note="auto-generated")
        return created["api_key"]


keys = ApiKeyStore()
