# -*- coding: utf-8 -*-
"""统一账号池（合并 jimeng2api.account_manager + tiktok2api.account_manager + coze2api.pool）。

所有渠道共用同一套账号生命周期：添加 -> 轮换 -> 冷却 -> 恢复。
差异只在凭据形态（cookies / browser_state / oauth），由 AccountSecret 承载。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.config import config
from src.core.crypto import protect, unprotect
from src.channels.base import AccountSecret
from src.platform.antiban import circuit, fingerprints, proxy_binder
from src.platform.db import db, now, jd, jl
from src.platform.session import keeper

DEFAULT_COOLDOWN = 300.0
MAX_CONSECUTIVE_ERRORS = 3


def _flatten_cookies(raw: Any) -> Dict[str, str]:
    """把任意 cookie 形态归一化为扁平 {name: value}（供请求头使用）。

    兼容 Cookie-Editor 数组 / {"cookies":[...]} / 扁平 dict / JSON 字符串。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return {}
    if isinstance(raw, list):
        return {str(c["name"]): str(c.get("value", "")) for c in raw
                if isinstance(c, dict) and "name" in c}
    if isinstance(raw, dict):
        inner = raw.get("cookies")
        if isinstance(inner, list):
            return {str(c["name"]): str(c.get("value", "")) for c in inner
                    if isinstance(c, dict) and "name" in c}
        return {str(k): ("" if v is None else str(v)) for k, v in raw.items()
                if isinstance(v, (str, int, float, bool)) or v is None}
    return {}


class AccountManager:
    def __init__(self):
        self.lock = threading.RLock()
        self._cursor: Dict[str, int] = {}          # channel -> round-robin 游标
        self.running: Dict[str, int] = {}          # account_id -> 在飞任务数

    # ---- 原始读取 ----
    def raw(self, aid: str) -> Optional[dict]:
        return db.one("SELECT * FROM accounts WHERE id=?", (aid,))

    def raws(self, channel: Optional[str] = None) -> List[dict]:
        if channel:
            return db.all("SELECT * FROM accounts WHERE channel=? ORDER BY created_at", (channel,))
        return db.all("SELECT * FROM accounts ORDER BY channel, created_at")

    def secret(self, aid: str) -> AccountSecret:
        r = db.one("SELECT * FROM account_secrets WHERE account_id=?", (aid,)) or {}
        raw_cookies = jl(unprotect(r.get("encrypted_cookies", "")), {})
        extra = jl(unprotect(r.get("encrypted_extra", "")), {})
        # 保留原始 cookie 结构（含 expires），供会话过期解析；headers 使用扁平视图
        extra = {**extra, "cookies_raw": raw_cookies}
        # 注入固定设备指纹，供渠道/传输层统一 UA 与语言头（防风控关键）
        if config.get("antiban", "fingerprint", True):
            try:
                row = self.raw(aid) or {}
                extra["fingerprint"] = fingerprints.get_or_create(aid, row.get("channel") or "")
            except Exception:
                pass
        return AccountSecret(
            cookies=_flatten_cookies(raw_cookies),
            browser_state=jl(unprotect(r.get("encrypted_browser_state", "")), {}),
            oauth=jl(unprotect(r.get("encrypted_oauth", "")), {}),
            extra=extra,
        )

    def public(self, row: Optional[dict]) -> Optional[dict]:
        if not row:
            return None
        s = db.one("SELECT * FROM account_secrets WHERE account_id=?", (row["id"],)) or {}
        credit = db.one("SELECT * FROM account_credits WHERE account_id=?", (row["id"],))
        caps = db.all("SELECT model_id,available,quota,checked_at FROM account_capabilities WHERE account_id=?", (row["id"],))
        # 生成任务成功/失败统计（jobs 表真实模型调用），区别于保活次数
        gen = db.one("SELECT "
                     "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS ok, "
                     "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS fail "
                     "FROM jobs WHERE account_id=?", (row["id"],)) or {}
        return {
            **{k: v for k, v in row.items() if k not in ("cooldown_until",)},
            "cooldown_until": row.get("cooldown_until"),
            "cooling": bool((row.get("cooldown_until") or 0) > time.time()),
            "has_cookies": bool(s.get("encrypted_cookies")),
            "has_browser_state": bool(s.get("encrypted_browser_state")),
            "has_oauth": bool(s.get("encrypted_oauth")),
            "has_profile": bool(row.get("profile_path")) and Path(str(row.get("profile_path"))).exists(),
            "credit": credit,
            "capabilities": caps,
            "gen_ok": int(gen.get("ok") or 0),
            "gen_fail": int(gen.get("fail") or 0),
        }

    def list(self, channel: Optional[str] = None) -> List[dict]:
        return [self.public(r) for r in self.raws(channel)]

    def get(self, aid: str) -> Optional[dict]:
        return self.public(self.raw(aid))

    # ---- 增删改 ----
    def add(self, channel: str, name: str = "", cookies=None, browser_state=None,
            oauth=None, extra=None, profile_path: str = "", proxy: str = "",
            site_region: str = "cn", note: str = "") -> Optional[dict]:
        aid = "acc_" + uuid.uuid4().hex[:12]
        profile = profile_path or str(config.profile_dir / channel / aid)
        Path(profile).parent.mkdir(parents=True, exist_ok=True)
        t = now()
        db.execute(
            "INSERT INTO accounts(id,channel,name,status,site_region,proxy,profile_path,created_at,note)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (aid, channel, name or aid, "unknown", site_region, proxy or "", profile, t, note), True)
        self.set_secrets(aid, cookies=cookies or {}, browser_state=browser_state or {},
                         oauth=oauth or {}, extra=extra or {})
        # 建号即固定设备指纹 + 粘性代理（一号一 IP），降低被识别为新设备的概率
        try:
            if config.get("antiban", "fingerprint", True):
                fingerprints.get_or_create(aid, channel)
            pool = config.antiban_proxy_pool
            if pool:
                proxy_binder.bind(aid, channel, pool)
        except Exception:
            pass
        return self.get(aid)

    def set_secrets(self, aid: str, cookies=None, browser_state=None, oauth=None, extra=None) -> None:
        row = db.one("SELECT * FROM account_secrets WHERE account_id=?", (aid,)) or {}
        old_c = jl(unprotect(row.get("encrypted_cookies", "")), {})
        old_b = jl(unprotect(row.get("encrypted_browser_state", "")), {})
        old_o = jl(unprotect(row.get("encrypted_oauth", "")), {})
        old_e = jl(unprotect(row.get("encrypted_extra", "")), {})
        c = old_c if cookies is None else cookies
        b = old_b if browser_state is None else browser_state
        o = old_o if oauth is None else oauth
        e = old_e if extra is None else extra
        t = now()
        cookie_at = t if cookies is not None or not row else (row.get("cookie_updated_at") or t)
        token_at = t if (oauth is not None or browser_state is not None) or not row else (row.get("token_updated_at") or t)
        db.execute(
            "INSERT INTO account_secrets(account_id,encrypted_cookies,encrypted_browser_state,"
            "encrypted_oauth,encrypted_extra,cookie_updated_at,token_updated_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(account_id) DO UPDATE SET encrypted_cookies=excluded.encrypted_cookies,"
            "encrypted_browser_state=excluded.encrypted_browser_state,encrypted_oauth=excluded.encrypted_oauth,"
            "encrypted_extra=excluded.encrypted_extra,cookie_updated_at=excluded.cookie_updated_at,"
            "token_updated_at=excluded.token_updated_at",
            (aid, protect(jd(c)), protect(jd(b)), protect(jd(o)), protect(jd(e)), cookie_at, token_at), True)
        # 凭据发生变更时做一份加密冗余快照（延长登录态：cookie 失效可回退）
        if (cookies is not None or oauth is not None or browser_state is not None):
            try:
                secret = self.secret(aid)
                if secret.cookies or secret.oauth or secret.browser_state:
                    keeper.backup(aid, secret, tag="auto")
            except Exception:
                pass

    def update(self, aid: str, fields: dict) -> Optional[dict]:
        allowed = {"name", "status", "site_region", "proxy", "profile_path", "note"}
        fields = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if fields:
            db.execute("UPDATE accounts SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?",
                       (*fields.values(), aid), True)
        return self.get(aid)

    def delete(self, aid: str) -> bool:
        for table in ("account_secrets", "account_capabilities", "account_credits", "checkin_runs"):
            db.execute(f"DELETE FROM {table} WHERE account_id=?", (aid,), True)
        db.execute("UPDATE jobs SET account_id=NULL WHERE account_id=?", (aid,), True)
        # 同步清理账号级代理绑定与指纹（避免残留）
        try:
            from src.platform.proxy_pool import proxy_pool
            proxy_pool.remove(aid)
        except Exception:
            pass
        db.execute("DELETE FROM accounts WHERE id=?", (aid,), True)
        return True

    # ---- 调度 ----
    def _eligible(self, channel: str, account_ids: Optional[List[str]] = None,
                  model: Optional[str] = None) -> List[dict]:
        t = now()
        rows = self.active(channel, account_ids)
        out = []
        for r in rows:
            # 冷却到期自动恢复
            if r.get("cooldown_until") and r["cooldown_until"] <= t:
                db.execute("UPDATE accounts SET status='active',cooldown_until=NULL,consecutive_errors=0 WHERE id=?",
                           (r["id"],), True)
                r = self.raw(r["id"])
            if (r.get("cooldown_until") or 0) > t:
                continue
            # 熔断打开的账号本轮不派发（避免持续触发风控）
            try:
                if circuit.state(f"{channel}:{r['id']}")["state"] == "open":
                    continue
            except Exception:
                pass
            out.append(r)
        return out

    def active(self, channel: Optional[str] = None, account_ids: Optional[List[str]] = None) -> List[dict]:
        if account_ids:
            ph = ",".join("?" for _ in account_ids)
            rows = db.all(f"SELECT * FROM accounts WHERE id IN ({ph}) AND status IN ('active','unknown')", tuple(account_ids))
            idx = {r["id"]: r for r in rows}
            return [idx[a] for a in account_ids if a in idx]
        if channel:
            return db.all("SELECT * FROM accounts WHERE channel=? AND status IN ('active','unknown') "
                          "ORDER BY last_used_at IS NULL DESC, last_used_at", (channel,))
        return db.all("SELECT * FROM accounts WHERE status IN ('active','unknown') ORDER BY last_used_at IS NULL DESC, last_used_at")

    def pick(self, channel: str, mode: str = "auto", account_id: Optional[str] = None,
             account_ids: Optional[List[str]] = None, model: Optional[str] = None) -> Optional[dict]:
        """统一调度：auto/round_robin 轮询，least_used 最小负载，specific 指定，concurrent 批量由调用方循环。"""
        with self.lock:
            if mode == "specific" and account_id:
                r = self.raw(account_id)
                if not r or r.get("status") not in ("active", "unknown"):
                    return None
                if (r.get("cooldown_until") or 0) > now():
                    return None
                return r
            rows = self._eligible(channel, account_ids, model)
            if not rows:
                return None
            if mode == "least_used":
                chosen = min(rows, key=lambda x: (x.get("total_requests") or 0, x.get("last_used_at") or 0))
            else:
                i = self._cursor.get(channel, 0) % len(rows)
                self._cursor[channel] = i + 1
                chosen = rows[i]
            db.execute("UPDATE accounts SET last_used_at=? WHERE id=?", (now(), chosen["id"]), True)
            return chosen

    def acquire(self, aid: str) -> None:
        with self.lock:
            self.running[aid] = self.running.get(aid, 0) + 1

    def release(self, aid: str, success: bool, error: str = "") -> None:
        with self.lock:
            self.running[aid] = max(0, self.running.get(aid, 1) - 1)
            r = self.raw(aid)
            if not r:
                return
            if success:
                db.execute("UPDATE accounts SET status='active',consecutive_errors=0,total_success=total_success+1,"
                           "last_checked_at=?,error_count=0 WHERE id=?", (now(), aid), True)
            else:
                streak = (r.get("consecutive_errors") or 0) + 1
                status = "cooldown" if streak >= MAX_CONSECUTIVE_ERRORS else r.get("status", "active")
                until = now() + DEFAULT_COOLDOWN if status == "cooldown" else r.get("cooldown_until")
                db.execute("UPDATE accounts SET status=?,consecutive_errors=?,total_failure=total_failure+1,"
                           "error_count=error_count+1,last_checked_at=?,cooldown_until=? WHERE id=?",
                           (status, streak, now(), until, aid), True)

    def mark(self, aid: str, status: Optional[str] = None, error: bool = False,
             account_type: Optional[str] = None) -> None:
        if status:
            db.execute("UPDATE accounts SET status=?,last_checked_at=? WHERE id=?", (status, now(), aid), True)
        if account_type:
            db.execute("UPDATE accounts SET account_type=? WHERE id=?", (account_type, aid), True)
        if error:
            db.execute("UPDATE accounts SET error_count=error_count+1 WHERE id=?", (aid,), True)

    def bump_generation(self, aid: str) -> None:
        db.execute("UPDATE accounts SET total_generations=total_generations+1 WHERE id=?", (aid,), True)

    # ---- 额度 / 能力 ----
    def save_credits(self, aid: str, view) -> dict:
        db.execute(
            "INSERT INTO account_credits(account_id,total,allocatable,buckets_json,raw_redacted_json,checked_at,error)"
            " VALUES(?,?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET total=excluded.total,"
            "allocatable=excluded.allocatable,buckets_json=excluded.buckets_json,"
            "raw_redacted_json=excluded.raw_redacted_json,checked_at=excluded.checked_at,error=excluded.error",
            (aid, float(getattr(view, "total", 0) or 0), float(getattr(view, "allocatable", 0) or 0),
             jd(getattr(view, "buckets", {}) or {}), jd(getattr(view, "raw", {}) or {}),
             now(), getattr(view, "error", "") or None), True)
        return db.one("SELECT * FROM account_credits WHERE account_id=?", (aid,))

    def credits(self, aid: str) -> Optional[dict]:
        return db.one("SELECT * FROM account_credits WHERE account_id=?", (aid,))

    def set_capabilities(self, aid: str, models: List[dict]) -> None:
        db.execute("DELETE FROM account_capabilities WHERE account_id=?", (aid,), True)
        for m in models or []:
            db.execute(
                "INSERT OR REPLACE INTO account_capabilities(account_id,model_id,available,quota,raw_redacted_json,checked_at)"
                " VALUES(?,?,?,?,?,?)",
                (aid, str(m.get("id") or ""), 1 if m.get("available", True) else 0,
                 str(m.get("quota") or ""), jd(m), now()), True)

    # ---- 会话 / 备份 / 指纹（登录态延长 + 防风控视图）----
    def session_snapshot(self, aid: str) -> Optional[dict]:
        """登录态 + 健康 + 指纹 + 代理 的组合视图。"""
        row = self.raw(aid)
        if not row:
            return None
        snap = keeper.snapshot(aid, self.secret(aid))
        snap["health_detail"] = keeper.health(aid)
        snap["fingerprint"] = self.fingerprint(aid)
        snap["proxy"] = proxy_binder.get(aid) or row.get("proxy") or ""
        snap["antiban"] = self.antiban_state(aid)
        return snap

    def fingerprint(self, aid: str) -> dict:
        row = self.raw(aid) or {}
        return fingerprints.get_or_create(aid, row.get("channel") or "")

    def rotate_fingerprint(self, aid: str) -> dict:
        row = self.raw(aid) or {}
        return fingerprints.rotate(aid, row.get("channel") or "")

    def backups(self, aid: str) -> List[dict]:
        return keeper.list_backups(aid)

    def backup_now(self, aid: str, tag: str = "manual") -> dict:
        return keeper.backup(aid, self.secret(aid), tag=tag)

    def restore_secret(self, aid: str, backup_id: Optional[str] = None) -> bool:
        """从冗余快照恢复凭据（cookie 失效时的兜底通道）。"""
        secret = keeper.restore(aid, backup_id)
        if secret is None:
            return False
        self.set_secrets(aid, cookies=secret.cookies, browser_state=secret.browser_state,
                         oauth=secret.oauth, extra=secret.extra)
        return True

    def antiban_state(self, aid: str) -> dict:
        row = self.raw(aid) or {}
        return circuit.state(f"{row.get('channel')}:{aid}")

    def summary(self) -> dict:
        rows = db.all("SELECT channel,status,COUNT(*) c FROM accounts GROUP BY channel,status")
        out: Dict[str, Dict[str, int]] = {}
        for r in rows:
            out.setdefault(r["channel"], {})[r["status"]] = r["c"]
        return out


manager = AccountManager()
