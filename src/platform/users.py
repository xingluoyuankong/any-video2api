# -*- coding: utf-8 -*-
"""控制台用户与会话（账号密码登录）。

分工：
    控制台（人）  -> 用户名 + 密码 -> session cookie
    程序调用（API）-> API Key（见 src/platform/keys.py）

密码用 PBKDF2-HMAC-SHA256（标准库，无额外依赖）加盐哈希；
会话 token 随机生成，库里只存 sha256 摘要，支持过期与全量吊销。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import List, Optional

from src.platform.db import db, now

SESSION_COOKIE = "av2a_session"
PBKDF2_ROUNDS = 200_000
SESSION_TTL = 7 * 24 * 3600          # 默认 7 天
INITIAL_ADMIN_FILE = "data/initial_admin.txt"

_DDL = (
    "CREATE TABLE IF NOT EXISTS users("
    "id TEXT PRIMARY KEY, username TEXT UNIQUE, password_hash TEXT, salt TEXT,"
    " role TEXT DEFAULT 'admin', created_at REAL, last_login_at REAL, disabled INTEGER DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS user_sessions("
    "token_hash TEXT PRIMARY KEY, user_id TEXT, username TEXT, created_at REAL,"
    " expires_at REAL, last_seen REAL, ip TEXT, ua TEXT)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_user ON user_sessions(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_exp ON user_sessions(expires_at)",
)


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                               salt.encode("utf-8"), PBKDF2_ROUNDS).hex()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class UserStore:
    def __init__(self) -> None:
        self._ready = False

    def _ensure(self) -> None:
        if self._ready:
            return
        for stmt in _DDL:
            db.execute(stmt, (), True)
        self._ready = True

    # ---- 用户 ----
    def ensure_default_admin(self) -> Optional[str]:
        """首次启动创建管理员，返回初始明文密码（仅这一次）；已存在则返回 None。

        密码来源优先级：环境变量 AV2A_ADMIN_PASSWORD > 随机生成。
        随机密码同时写入 data/initial_admin.txt（权限 600），避免日志被刷掉后找不回。
        """
        self._ensure()
        if db.one("SELECT id FROM users LIMIT 1"):
            return None
        import os
        from pathlib import Path
        username = os.getenv("AV2A_ADMIN_USER", "admin")
        password = os.getenv("AV2A_ADMIN_PASSWORD") or secrets.token_urlsafe(12)
        self.create(username, password, role="admin")
        try:
            p = Path(INITIAL_ADMIN_FILE)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"用户名: {username}\n密码:   {password}\n"
                         f"（登录后请立即在控制台修改密码，然后删除本文件）\n", encoding="utf-8")
            try:
                os.chmod(p, 0o600)
            except Exception:
                pass
        except Exception:
            pass
        return password

    def create(self, username: str, password: str, role: str = "admin") -> dict:
        self._ensure()
        username = (username or "").strip()
        if not username or not password:
            raise ValueError("用户名与密码不能为空")
        if db.one("SELECT id FROM users WHERE username=?", (username,)):
            raise ValueError(f"用户名已存在: {username}")
        uid = "u_" + secrets.token_hex(8)
        salt = secrets.token_hex(16)
        db.execute(
            "INSERT INTO users(id,username,password_hash,salt,role,created_at,last_login_at,disabled)"
            " VALUES(?,?,?,?,?,?,NULL,0)",
            (uid, username, _hash_password(password, salt), salt, role, now()), True)
        return self.get(username)

    def get(self, username: str) -> Optional[dict]:
        self._ensure()
        return db.one("SELECT * FROM users WHERE username=?", (username,))

    def list(self) -> List[dict]:
        self._ensure()
        rows = db.all("SELECT id,username,role,created_at,last_login_at,disabled FROM users ORDER BY created_at")
        for r in rows:
            r["disabled"] = bool(r.get("disabled"))
        return rows

    def verify(self, username: str, password: str) -> Optional[dict]:
        """校验账号密码；成功返回用户行，失败返回 None（不区分用户不存在/密码错）。"""
        self._ensure()
        row = self.get((username or "").strip())
        if not row or row.get("disabled"):
            # 恒定时间：避免通过响应时间枚举用户名
            _hash_password(password or "", "dummy-salt")
            return None
        calc = _hash_password(password or "", row["salt"])
        if not hmac.compare_digest(calc, row["password_hash"]):
            return None
        db.execute("UPDATE users SET last_login_at=? WHERE id=?", (now(), row["id"]), True)
        return self.get(row["username"])

    def change_password(self, username: str, old_password: str, new_password: str) -> bool:
        self._ensure()
        if not self.verify(username, old_password):
            return False
        return self.reset_password(username, new_password)

    def reset_password(self, username: str, new_password: str) -> bool:
        self._ensure()
        if not new_password:
            raise ValueError("新密码不能为空")
        row = self.get(username)
        if not row:
            return False
        salt = secrets.token_hex(16)
        db.execute("UPDATE users SET password_hash=?, salt=? WHERE id=?",
                   (_hash_password(new_password, salt), salt, row["id"]), True)
        self.revoke_user_sessions(row["id"])   # 改密后强制重新登录
        return True

    def set_disabled(self, username: str, disabled: bool) -> bool:
        self._ensure()
        row = self.get(username)
        if not row:
            return False
        db.execute("UPDATE users SET disabled=? WHERE id=?", (1 if disabled else 0, row["id"]), True)
        if disabled:
            self.revoke_user_sessions(row["id"])
        return True

    def delete(self, username: str) -> bool:
        self._ensure()
        row = self.get(username)
        if not row:
            return False
        if db.one("SELECT COUNT(*) c FROM users")["c"] <= 1:
            raise ValueError("至少保留一个用户")
        self.revoke_user_sessions(row["id"])
        db.execute("DELETE FROM users WHERE id=?", (row["id"],), True)
        return True

    # ---- 会话 ----
    def create_session(self, user: dict, ip: str = "", ua: str = "",
                       ttl: float = SESSION_TTL) -> str:
        self._ensure()
        token = secrets.token_urlsafe(32)
        t = now()
        db.execute(
            "INSERT INTO user_sessions(token_hash,user_id,username,created_at,expires_at,last_seen,ip,ua)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (_hash_token(token), user["id"], user["username"], t, t + ttl, t, ip[:64], ua[:200]), True)
        self.cleanup()
        return token

    def validate_session(self, token: Optional[str]) -> Optional[dict]:
        if not token:
            return None
        self._ensure()
        row = db.one("SELECT * FROM user_sessions WHERE token_hash=?", (_hash_token(token),))
        if not row:
            return None
        if (row.get("expires_at") or 0) <= now():
            db.execute("DELETE FROM user_sessions WHERE token_hash=?", (row["token_hash"],), True)
            return None
        user = db.one("SELECT id,username,role,disabled FROM users WHERE id=?", (row["user_id"],))
        if not user or user.get("disabled"):
            return None
        # 滑动续期：超过 5 分钟才写一次，降低写放大
        if now() - (row.get("last_seen") or 0) > 300:
            db.execute("UPDATE user_sessions SET last_seen=?, expires_at=? WHERE token_hash=?",
                       (now(), now() + SESSION_TTL, row["token_hash"]), True)
        user["disabled"] = bool(user.get("disabled"))
        return user

    def revoke_session(self, token: Optional[str]) -> bool:
        if not token:
            return False
        self._ensure()
        db.execute("DELETE FROM user_sessions WHERE token_hash=?", (_hash_token(token),), True)
        return True

    def revoke_user_sessions(self, user_id: str) -> int:
        self._ensure()
        rows = db.all("SELECT token_hash FROM user_sessions WHERE user_id=?", (user_id,))
        db.execute("DELETE FROM user_sessions WHERE user_id=?", (user_id,), True)
        return len(rows)

    def list_sessions(self, user_id: Optional[str] = None) -> List[dict]:
        self._ensure()
        if user_id:
            return db.all("SELECT username,created_at,expires_at,last_seen,ip,ua FROM user_sessions"
                          " WHERE user_id=? ORDER BY last_seen DESC", (user_id,))
        return db.all("SELECT username,created_at,expires_at,last_seen,ip,ua FROM user_sessions"
                      " ORDER BY last_seen DESC")

    def cleanup(self) -> int:
        self._ensure()
        rows = db.all("SELECT token_hash FROM user_sessions WHERE expires_at<=?", (now(),))
        if rows:
            db.execute("DELETE FROM user_sessions WHERE expires_at<=?", (now(),), True)
        return len(rows)


users = UserStore()
