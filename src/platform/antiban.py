# -*- coding: utf-8 -*-
"""防风控核心：滑动窗口限流 / 人类化节流 / 风控熔断 / 设备指纹 / 代理粘性绑定。

自包含单文件，复用平台公共件（config / db / errors），不依赖 accounts、channels。
表结构在本文件内惰性创建（首次调用时 CREATE TABLE IF NOT EXISTS），import 无副作用。

集成方调用点：
    请求前  antiban.before_request(channel, account_id, rpm_limit)
    请求后  antiban.after_request(channel, account_id, ok, error_class)
    建会话  fingerprints.get_or_create(account_id, channel)
    定代理  proxy_binder.bind(account_id, channel, pool)
"""
from __future__ import annotations

import hashlib
import random
import threading
import time
import uuid
from collections import deque
from typing import Deque, Dict, List, Optional

from src.core.config import config
from src.platform.db import db, now, jd, jl

# 计入熔断失败的错误类型（风控 / 并发），其余错误分类不计失败也不重置
FAIL_CLASSES = ("auth_or_risk", "concurrency")

# 真实 Chrome UA 池：同账号按稳定哈希命中固定一条
_UA_POOL: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36",
]
_LANG_POOL: List[str] = [
    "zh-CN,zh;q=0.9,en;q=0.8",
    "zh-CN,zh;q=0.9",
    "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
]
_SCREEN_POOL: List[str] = ["1920x1080", "2560x1440", "1366x768", "1536x864", "1440x900"]
_TZ_POOL: List[str] = ["Asia/Shanghai"]

# 惰性建表 DDL（不改 db.py 的 SCHEMA）
_DDL_DEVICE = (
    "CREATE TABLE IF NOT EXISTS device_fingerprints("
    "account_id TEXT PRIMARY KEY, channel TEXT, fingerprint_json TEXT, "
    "created_at REAL, rotated_at REAL)"
)
_DDL_PROXY = (
    "CREATE TABLE IF NOT EXISTS proxy_bindings("
    "account_id TEXT PRIMARY KEY, channel TEXT, proxy TEXT, bound_at REAL)"
)


def _stable_index(account_id: str, salt: str, size: int) -> int:
    """对 account_id 做稳定哈希取模，保证同账号永远命中同一项。"""
    h = hashlib.sha256(f"{salt}:{account_id}".encode("utf-8")).hexdigest()
    return int(h, 16) % max(1, size)


class RateLimiter:
    """滑动窗口限流，支持「渠道」「账号」「渠道:账号」任意 key 维度。"""

    def __init__(self) -> None:
        self._hits: Dict[str, Deque[float]] = {}
        self._limits: Dict[str, int] = {}
        self._windows: Dict[str, float] = {}
        self._lock = threading.RLock()

    def _prune(self, dq: Deque[float], ts: float, window: float) -> None:
        while dq and ts - dq[0] >= window:
            dq.popleft()

    def acquire(self, key: str, limit: int, window: float = 60.0) -> float:
        """尝试占用一个名额；放行返回 0，超限返回需等待秒数（不登记超限请求）。"""
        ts = time.monotonic()
        limit = max(1, int(limit))
        with self._lock:
            dq = self._hits.setdefault(key, deque())
            self._limits[key] = limit
            self._windows[key] = float(window)
            self._prune(dq, ts, window)
            if len(dq) < limit:
                dq.append(ts)
                return 0.0
            return max(0.0, window - (ts - dq[0]))

    def snapshot(self, key: str) -> dict:
        """返回 {count, limit, remaining, reset_in}。"""
        ts = time.monotonic()
        with self._lock:
            dq = self._hits.get(key)
            limit = int(self._limits.get(key, 0))
            window = float(self._windows.get(key, 60.0))
            if not dq:
                return {"count": 0, "limit": limit, "remaining": limit, "reset_in": 0.0}
            self._prune(dq, ts, window)
            count = len(dq)
            reset_in = max(0.0, window - (ts - dq[0])) if dq else 0.0
            return {"count": count, "limit": limit,
                    "remaining": max(0, limit - count), "reset_in": reset_in}

    def snapshot_all(self) -> Dict[str, dict]:
        with self._lock:
            return {k: self.snapshot(k) for k in list(self._hits)}


class Throttle:
    """人类化节流：同 key 两次调用间隔 >= min_interval + random(0, jitter)。"""

    def __init__(self) -> None:
        self._last: Dict[str, float] = {}
        self._lock = threading.RLock()

    def plan(self, key: str, min_interval: float, jitter: float) -> float:
        """只计算不 sleep，并预留该时间槽（供 Worker 计算 next_poll_at）。"""
        with self._lock:
            ts = time.monotonic()
            last = self._last.get(key)
            gap = max(0.0, float(min_interval)) + random.uniform(0.0, max(0.0, float(jitter)))
            target = (last + gap) if last is not None else ts
            wait = max(0.0, target - ts)
            self._last[key] = ts + wait
            return wait

    def wait(self, key: str, min_interval: float, jitter: float) -> float:
        """内部真的 sleep，返回实际 sleep 秒数。"""
        wait = self.plan(key, min_interval, jitter)
        if wait > 0:
            time.sleep(wait)
        return wait


class CircuitBreaker:
    """风控熔断器，三态 closed / open / half_open。"""

    def __init__(self) -> None:
        self._st: Dict[str, dict] = {}
        self._lock = threading.RLock()

    def _cfg(self) -> tuple:
        threshold = int(config.get("antiban", "failure_threshold", 3))
        cooldown = float(config.get("antiban", "cooldown", 120))
        return max(1, threshold), max(0.0, cooldown)

    def _entry(self, key: str) -> dict:
        e = self._st.get(key)
        if e is None:
            e = {"state": "closed", "failures": 0, "opened_at": 0.0}
            self._st[key] = e
        return e

    def allow(self, key: str) -> bool:
        """closed 放行；open 未过冷却拒绝、过了冷却转 half_open 放行探测。"""
        _, cooldown = self._cfg()
        with self._lock:
            e = self._entry(key)
            if e["state"] == "open":
                if now() - e["opened_at"] >= cooldown:
                    e["state"] = "half_open"
                    return True
                return False
            return True

    def record(self, key: str, error_class: str) -> str:
        """auth_or_risk / concurrency 计失败；成功重置；其余分类中性。"""
        threshold, _ = self._cfg()
        ec = str(error_class or "").lower()
        with self._lock:
            e = self._entry(key)
            if ec in FAIL_CLASSES:
                e["failures"] += 1
                if e["state"] == "half_open" or e["failures"] >= threshold:
                    e["state"] = "open"
                    e["opened_at"] = now()
            elif ec in ("", "success", "ok"):
                e["state"] = "closed"
                e["failures"] = 0
                e["opened_at"] = 0.0
            return e["state"]

    def state(self, key: str) -> dict:
        _, cooldown = self._cfg()
        with self._lock:
            e = self._entry(key)
            remaining = 0.0
            if e["state"] == "open":
                remaining = max(0.0, cooldown - (now() - e["opened_at"]))
            return {"state": e["state"], "failures": e["failures"],
                    "opened_at": e["opened_at"], "cooldown_remaining": remaining}

    def states(self) -> Dict[str, dict]:
        with self._lock:
            return {k: self.state(k) for k in list(self._st)}


class FingerprintStore:
    """设备指纹持久化：同一账号永远返回同一套指纹，避免被判为新设备。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._ddl_done = False

    def _ensure(self) -> None:
        if self._ddl_done:
            return
        with self._lock:
            if not self._ddl_done:
                db.execute(_DDL_DEVICE, (), True)
                self._ddl_done = True

    def _build(self, account_id: str, channel: str, nonce: str = "") -> dict:
        """按稳定哈希生成指纹；nonce 非空时（轮换场景）引入随机性得到新指纹。"""
        seed = f"{account_id}:{nonce}" if nonce else account_id
        ua = _UA_POOL[_stable_index(seed, "ua", len(_UA_POOL))]
        device_id = hashlib.sha256(f"device:{seed}".encode("utf-8")).hexdigest()[:32]
        web_id = str(
            int(hashlib.sha256(f"web:{seed}".encode("utf-8")).hexdigest(), 16) % (10 ** 18)
        )
        return {
            "channel": channel,
            "user_agent": ua,
            "accept_language": _LANG_POOL[_stable_index(seed, "lang", len(_LANG_POOL))],
            "device_id": device_id,
            "web_id": web_id,
            "screen": _SCREEN_POOL[_stable_index(seed, "screen", len(_SCREEN_POOL))],
            "timezone": _TZ_POOL[_stable_index(seed, "tz", len(_TZ_POOL))],
            "platform": "MacIntel" if "Macintosh" in ua else "Win32",
        }

    def get_or_create(self, account_id: str, channel: str) -> dict:
        """首次生成并落库，之后固定返回同一份指纹。"""
        self._ensure()
        row = db.one("SELECT fingerprint_json FROM device_fingerprints WHERE account_id=?",
                     (account_id,))
        if row:
            data = jl(row.get("fingerprint_json"), None)
            if isinstance(data, dict) and data:
                return data
        fp = self._build(account_id, channel)
        db.execute(
            "INSERT INTO device_fingerprints(account_id,channel,fingerprint_json,created_at,"
            "rotated_at) VALUES(?,?,?,?,NULL)"
            " ON CONFLICT(account_id) DO UPDATE SET channel=excluded.channel,"
            " fingerprint_json=excluded.fingerprint_json",
            (account_id, channel, jd(fp), now()), True)
        return fp

    def rotate(self, account_id: str, channel: str = "") -> dict:
        """强制重新生成（用于登录态彻底失效后），保留原 created_at。"""
        self._ensure()
        old = db.one("SELECT channel, created_at FROM device_fingerprints WHERE account_id=?",
                     (account_id,))
        ch = channel or (old.get("channel") if old else "") or "unknown"
        created_at = old.get("created_at") if old else now()
        fp = self._build(account_id, ch, nonce=uuid.uuid4().hex)
        db.execute(
            "INSERT INTO device_fingerprints(account_id,channel,fingerprint_json,created_at,"
            "rotated_at) VALUES(?,?,?,?,?)"
            " ON CONFLICT(account_id) DO UPDATE SET channel=excluded.channel,"
            " fingerprint_json=excluded.fingerprint_json, rotated_at=excluded.rotated_at",
            (account_id, ch, jd(fp), created_at, now()), True)
        return fp


class ProxyBinder:
    """账号↔代理粘性绑定（一号一 IP），按稳定哈希从代理池选并固定。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._ddl_done = False

    def _ensure(self) -> None:
        if self._ddl_done:
            return
        with self._lock:
            if not self._ddl_done:
                db.execute(_DDL_PROXY, (), True)
                self._ddl_done = True

    def bind(self, account_id: str, channel: str, pool: List[str]) -> Optional[str]:
        """已绑定则返回原代理；否则从池中稳定选一个落库；池为空返回 None。"""
        self._ensure()
        row = db.one("SELECT proxy FROM proxy_bindings WHERE account_id=?", (account_id,))
        if row and row.get("proxy"):
            return row["proxy"]
        items = [p for p in (pool or []) if p]
        if not items:
            return None
        proxy = items[_stable_index(account_id, "proxy", len(items))]
        db.execute(
            "INSERT INTO proxy_bindings(account_id,channel,proxy,bound_at) VALUES(?,?,?,?)"
            " ON CONFLICT(account_id) DO UPDATE SET channel=excluded.channel,"
            " proxy=excluded.proxy, bound_at=excluded.bound_at",
            (account_id, channel, proxy, now()), True)
        return proxy

    def get(self, account_id: str) -> Optional[str]:
        self._ensure()
        row = db.one("SELECT proxy FROM proxy_bindings WHERE account_id=?", (account_id,))
        return row.get("proxy") if row else None

    def unbind(self, account_id: str) -> None:
        self._ensure()
        db.execute("DELETE FROM proxy_bindings WHERE account_id=?", (account_id,), True)


class AntiBanManager:
    """组合门面：熔断检查 → 限流等待 → 节流等待，集成方一处调用。"""

    def __init__(self, limiter: Optional[RateLimiter] = None,
                 throttle_: Optional[Throttle] = None,
                 breaker: Optional[CircuitBreaker] = None) -> None:
        self.limiter = limiter or rate_limiter
        self.throttle = throttle_ or throttle
        self.breaker = breaker or circuit

    @staticmethod
    def _key(channel: str, account_id: str) -> str:
        return f"{channel}:{account_id}"

    def before_request(self, channel: str, account_id: str, rpm_limit: int) -> dict:
        """返回 {allowed, waited, reason}；reason 为空表示放行。"""
        key = self._key(channel, account_id)
        if not self.breaker.allow(key):
            return {"allowed": False, "waited": 0.0, "reason": "circuit_open"}
        waited = 0.0
        wait = self.limiter.acquire(key, rpm_limit, 60.0)
        if wait > 0:
            time.sleep(wait)
            waited += wait
            self.limiter.acquire(key, rpm_limit, 60.0)  # 等待后重新登记名额
        min_interval = float(config.get("antiban", "min_interval", 1.0))
        jitter = float(config.get("antiban", "jitter", 0.5))
        waited += self.throttle.wait(f"{key}:throttle", min_interval, jitter)
        return {"allowed": True, "waited": waited, "reason": ""}

    def after_request(self, channel: str, account_id: str, ok: bool,
                      error_class: str = "") -> None:
        """把结果喂给熔断器：成功重置，风控/并发计失败。"""
        key = self._key(channel, account_id)
        self.breaker.record(key, "" if ok else (error_class or "upstream"))

    def status(self) -> dict:
        """汇总当前限流 / 熔断状态。"""
        return {
            "rate_limiter": self.limiter.snapshot_all(),
            "circuit": self.breaker.states(),
        }


# ---- 全局单例 ----
rate_limiter = RateLimiter()
throttle = Throttle()
circuit = CircuitBreaker()
fingerprints = FingerprintStore()
proxy_binder = ProxyBinder()
antiban = AntiBanManager()
