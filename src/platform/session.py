# -*- coding: utf-8 -*-
"""登录态生命周期与保活（session.py）。

把三项目各自散落的「cookie 过期判断 / 心跳保活 / 凭据备份」统一到平台层：

    解析过期 -> 健康评估 -> 多凭据冗余备份 -> 定时心跳 -> 连续失败升级为 relogin

设计约束：
- 只依赖 src.core / src.platform.db / src.channels.base，**绝不** import 账号池
  （src.platform.accounts）或渠道实现（src.channels.registry / src.channels.*），
  避免循环依赖。定时保活通过回调注入（lister + verifier）。
- 表惰性创建，import 本模块无任何副作用。
- 时间统一 time.time()（epoch 秒）；expires 若为毫秒级（>1e12）自动归一化。
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from src.core.config import config
from src.core.crypto import protect, unprotect
from src.channels.base import AccountSecret
from src.platform.db import db, jd, jl

# ---------------------------------------------------------------------------
# 惰性建表（不改 db.py）
# ---------------------------------------------------------------------------
SESSION_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS session_events("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT, kind TEXT, ok INTEGER,"
    " detail TEXT, ts REAL)",
    "CREATE INDEX IF NOT EXISTS idx_session_events_acc ON session_events(account_id, id)",
    "CREATE TABLE IF NOT EXISTS credential_backups("
    "backup_id TEXT PRIMARY KEY, account_id TEXT, tag TEXT, encrypted_secret TEXT,"
    " cookie_count INTEGER, created_at REAL)",
    "CREATE INDEX IF NOT EXISTS idx_cred_backups_acc ON credential_backups(account_id, created_at)",
    "CREATE TABLE IF NOT EXISTS session_state("
    "account_id TEXT PRIMARY KEY, last_keepalive_at REAL, last_refresh_at REAL,"
    " last_ok_at REAL, consecutive_failures INTEGER DEFAULT 0,"
    " total_ok INTEGER DEFAULT 0, total_fail INTEGER DEFAULT 0)",
)

_schema_lock = threading.Lock()
_schema_ready = False


def _ensure_schema() -> None:
    """惰性建表：首次触碰 DB 时执行一次，幂等。"""
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        for stmt in SESSION_SCHEMA:
            db.execute(stmt, (), True)
        _schema_ready = True


# ---------------------------------------------------------------------------
# Cookie / OAuth 过期解析
# ---------------------------------------------------------------------------
def _norm_epoch(value: Any) -> Optional[float]:
    """把过期值归一化为 epoch 秒；<=0（session cookie）或非法值返回 None。

    >1e12 视为毫秒（Cookie-Editor / 浏览器导出常给 ms），循环 /1000 归一化。
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    while v > 1e12:
        v /= 1000.0
    return v


def _cookie_items(cookies: Any) -> List[Any]:
    """把三种 cookie 形态统一成条目列表。

    - Cookie-Editor 数组：[{name,value,expires}, ...]
    - {"cookies":[...]} 包裹（Playwright storage_state 同形）
    - 扁平 {name: value}（无过期信息）
    """
    if isinstance(cookies, list):
        return cookies
    if isinstance(cookies, dict):
        inner = cookies.get("cookies")
        if isinstance(inner, list):
            return inner
        return [{"name": k, "value": v, "expires": None} for k, v in cookies.items()]
    return []


def _cookies_of(secret: AccountSecret) -> Any:
    """优先取原始 cookie 结构（含 expires，由 accounts.secret 注入 extra.cookies_raw）。"""
    raw = (getattr(secret, "extra", None) or {}).get("cookies_raw")
    if raw not in (None, {}, []):
        return raw
    return getattr(secret, "cookies", None)


def parse_cookie_expiry(cookies: Any) -> Optional[float]:
    """返回最早的有效 expires（epoch 秒）。

    全部为 session cookie（expires 为 -1/0/缺失）或无法解析时返回 None。
    """
    expiries: List[float] = []
    for item in _cookie_items(cookies):
        if isinstance(item, dict):
            e = _norm_epoch(item.get("expires"))
            if e is not None:
                expiries.append(e)
    return min(expiries) if expiries else None


def _oauth_expiry(oauth: Any) -> Optional[float]:
    """从 oauth 字典提取过期时间（epoch 秒）；支持 expires_in 相对秒。"""
    if not isinstance(oauth, dict):
        return None
    for key in ("token_expires_at", "expires_at", "expires", "expiry_at", "expire_at", "expiry"):
        if key in oauth:
            v = _norm_epoch(oauth.get(key))
            if v is not None:
                return v
    for key in ("expires_in", "expiresIn"):
        if key in oauth:
            try:
                secs = float(oauth.get(key))
            except (TypeError, ValueError):
                continue
            if secs > 0:
                return time.time() + secs
    return None


def _cookie_count(cookies: Any) -> int:
    """统计 cookie 条目数（三种形态通用）。"""
    return len(_cookie_items(cookies))


def _effective_expiry(secret: AccountSecret) -> Optional[float]:
    """权威过期时间：cookie 有 expires 用 cookie，否则退回 oauth 的 expires。"""
    ce = parse_cookie_expiry(_cookies_of(secret))
    if ce is not None:
        return ce
    return _oauth_expiry(secret.oauth)


def _serialize_secret(secret: AccountSecret) -> str:
    """AccountSecret -> 明文 JSON（落库前由 protect 加密）。

    cookies 存原始结构（保留 expires），确保快照恢复后过期解析仍然可用。
    """
    raw = (getattr(secret, "extra", None) or {}).get("cookies_raw")
    return jd({
        "cookies": raw if raw not in (None, {}, []) else (secret.cookies or {}),
        "browser_state": secret.browser_state or {},
        "oauth": secret.oauth or {},
        "extra": {k: v for k, v in (secret.extra or {}).items() if k != "cookies_raw"},
    })


def _deserialize_secret(payload: str) -> AccountSecret:
    """明文 JSON -> AccountSecret（缺失字段回退空 dict）。"""
    data = jl(payload, {}) or {}
    return AccountSecret(
        cookies=data.get("cookies") or {},
        browser_state=data.get("browser_state") or {},
        oauth=data.get("oauth") or {},
        extra=data.get("extra") or {},
    )


# ---------------------------------------------------------------------------
# SessionKeeper：登录态生命周期
# ---------------------------------------------------------------------------
class SessionKeeper:
    """登录态快照 / 刷新判断 / 保活节流 / 活动记账 / 多凭据备份恢复。"""

    # ---- 快照与健康评估 --------------------------------------------------
    def snapshot(self, account_id: str, secret: AccountSecret) -> dict:
        """计算登录态快照（不落库）。expires_at 为权威过期时间。"""
        _ensure_schema()
        t = time.time()
        cookie_exp = parse_cookie_expiry(_cookies_of(secret))
        oauth_exp = _oauth_expiry(secret.oauth)
        effective = cookie_exp if cookie_exp is not None else oauth_exp
        return {
            "account_id": account_id,
            "cookie_count": _cookie_count(_cookies_of(secret)),
            "expires_at": effective,
            "expires_in": (effective - t) if effective is not None else None,
            "is_session_cookie": cookie_exp is None,
            "has_oauth": bool(secret.oauth),
            "oauth_expires_at": oauth_exp,
            "health": self._health_state(effective, t),
        }

    @staticmethod
    def _health_state(effective: Optional[float], t: float) -> str:
        """由权威过期时间推导健康态。"""
        if effective is None:
            return "unknown"
        if effective <= t:
            return "expired"
        lead = float(config.get("session", "lead_time", 3600) or 3600)
        if effective - t < lead:
            return "expiring"
        return "healthy"

    def needs_refresh(self, account_id: str, secret: AccountSecret, lead_time: float = 3600) -> bool:
        """距过期 < lead_time 或已过期 -> True；无任何过期信息 -> False。"""
        _ensure_schema()
        effective = _effective_expiry(secret)
        if effective is None:
            return False
        return (effective - time.time()) < lead_time

    def needs_keepalive(self, account_id: str, secret: Optional[AccountSecret] = None,
                        interval: float = 1800) -> bool:
        """距上次尝试超过 interval（或从未保活）-> True。

        secret 仅作接口占位（保活判断只看 session_state），可为 None。

        连续失败会触发两级保护，避免把上游打死：
          1) 指数退避：连续失败 n 次 -> 间隔 × 2^n（上限 6 小时）
          2) 熔断：连续失败达 relogin_threshold×2 次 -> 不再保活，
             等用户重新登录（否则高频重试会触发风控，把临时失败变成真失效）
        """
        _ensure_schema()
        st = db.one("SELECT last_keepalive_at, consecutive_failures FROM session_state "
                    "WHERE account_id=?", (account_id,))
        last = (st or {}).get("last_keepalive_at")
        fails = int((st or {}).get("consecutive_failures") or 0)
        threshold = int(config.get("session", "relogin_threshold", 3) or 3)

        # 熔断：连续失败过多，停止保活（等重登），不再打上游
        if fails >= max(threshold * 2, 6):
            return False
        if not last:
            return True
        # 指数退避
        eff = interval * (2 ** min(fails, 4)) if fails > 0 else interval
        eff = min(eff, 21600.0)
        return (time.time() - float(last)) >= eff

    # ---- 活动记账 --------------------------------------------------------
    def record_activity(self, account_id: str, kind: str = "keepalive",
                        ok: bool = True, detail: str = "") -> None:
        """写一条活动记录，并增量更新 session_state（成功刷新对应时间戳）。"""
        _ensure_schema()
        t = time.time()
        db.execute(
            "INSERT INTO session_events(account_id,kind,ok,detail,ts) VALUES(?,?,?,?,?)",
            (account_id, kind, 1 if ok else 0, detail, t), True)

        row = db.one("SELECT account_id FROM session_state WHERE account_id=?", (account_id,))
        if row is None:
            db.execute(
                "INSERT INTO session_state(account_id,last_keepalive_at,last_refresh_at,last_ok_at,"
                "consecutive_failures,total_ok,total_fail) VALUES(?,?,?,?,?,?,?)",
                (account_id,
                 t if (ok and kind == "keepalive") else None,
                 t if (ok and kind == "refresh") else None,
                 t if ok else None,
                 0 if ok else 1,
                 1 if ok else 0,
                 0 if ok else 1), True)
            return

        sets = ["total_ok=total_ok+?", "total_fail=total_fail+?"]
        args: List[Any] = [1 if ok else 0, 0 if ok else 1]
        if ok:
            sets += ["consecutive_failures=0", "last_ok_at=?"]
            args.append(t)
            if kind == "keepalive":
                sets.append("last_keepalive_at=?")
                args.append(t)
            elif kind == "refresh":
                sets.append("last_refresh_at=?")
                args.append(t)
        else:
            sets.append("consecutive_failures=consecutive_failures+1")
            # 失败也推进 last_keepalive_at（语义变为「最后一次尝试时间」）。
            # 否则失败账号的该时间戳永远停在旧值，needs_keepalive 每轮都判 True，
            # 30 分钟一轮无限重试 —— 实测把 coze/tiktok/jimeng 打到 70+ 次失败，
            # 高频请求触发上游风控，反而把本来只是临时抖动的凭证彻底打死。
            if kind == "keepalive":
                sets.append("last_keepalive_at=?")
                args.append(t)
            elif kind == "refresh":
                sets.append("last_refresh_at=?")
                args.append(t)
        args.append(account_id)
        db.execute("UPDATE session_state SET " + ",".join(sets) + " WHERE account_id=?", tuple(args), True)

    def health(self, account_id: str) -> dict:
        """返回最近活动、成功/失败计数、连续失败与建议动作（reuse|refresh|relogin）。"""
        _ensure_schema()
        st = db.one("SELECT * FROM session_state WHERE account_id=?", (account_id,)) or {}
        last = db.one(
            "SELECT kind,ok,detail,ts FROM session_events WHERE account_id=? ORDER BY id DESC LIMIT 1",
            (account_id,))
        consec = int(st.get("consecutive_failures") or 0)
        threshold = int(config.get("session", "relogin_threshold", 3) or 3)
        if consec >= threshold:
            action = "relogin"
        elif consec > 0:
            action = "refresh"
        else:
            action = "reuse"
        return {
            "account_id": account_id,
            "last_event": last,
            "last_keepalive_at": st.get("last_keepalive_at"),
            "last_refresh_at": st.get("last_refresh_at"),
            "last_ok_at": st.get("last_ok_at"),
            "total_ok": int(st.get("total_ok") or 0),
            "total_fail": int(st.get("total_fail") or 0),
            "consecutive_failures": consec,
            "action": action,
        }

    def status(self) -> dict:
        """全部账号的会话健康汇总。"""
        _ensure_schema()
        rows = db.all("SELECT account_id FROM session_state ORDER BY account_id")
        accounts = [self.health(r["account_id"]) for r in rows]
        by_action: Dict[str, int] = {"reuse": 0, "refresh": 0, "relogin": 0}
        for a in accounts:
            by_action[a["action"]] = by_action.get(a["action"], 0) + 1
        return {
            "total": len(accounts),
            "by_action": by_action,
            "accounts": accounts,
            "generated_at": time.time(),
        }

    # ---- 多凭据冗余备份 --------------------------------------------------
    def backup(self, account_id: str, secret: AccountSecret, tag: str = "auto") -> dict:
        """把当前凭据加密落库，保留最近 N 份（config session.backup_keep，默认 5）。"""
        _ensure_schema()
        t = time.time()
        bid = "bk_" + uuid.uuid4().hex[:16]
        cookie_count = _cookie_count(_cookies_of(secret))
        db.execute(
            "INSERT INTO credential_backups(backup_id,account_id,tag,encrypted_secret,cookie_count,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (bid, account_id, tag, protect(_serialize_secret(secret)), cookie_count, t), True)
        keep = int(config.get("session", "backup_keep", 5) or 5)
        self.prune(account_id, keep)
        self.record_activity(account_id, "backup", True, f"tag={tag} id={bid}")
        return {"backup_id": bid, "created_at": t, "cookie_count": cookie_count, "tag": tag}

    def list_backups(self, account_id: str) -> List[dict]:
        """返回快照元数据（不含明文密文）。"""
        _ensure_schema()
        return db.all(
            "SELECT backup_id,account_id,tag,cookie_count,created_at FROM credential_backups"
            " WHERE account_id=? ORDER BY created_at DESC, rowid DESC", (account_id,))

    def restore(self, account_id: str, backup_id: Optional[str] = None) -> Optional[AccountSecret]:
        """恢复指定（或最近一份）快照；无可用快照返回 None。

        返回的 AccountSecret 含 browser_state，cookie 失效时可由集成方用
        browser_state 重新签发 cookie 兜底。
        """
        _ensure_schema()
        if backup_id:
            row = db.one("SELECT * FROM credential_backups WHERE backup_id=? AND account_id=?",
                         (backup_id, account_id))
        else:
            row = db.one("SELECT * FROM credential_backups WHERE account_id=?"
                         " ORDER BY created_at DESC, rowid DESC LIMIT 1", (account_id,))
        if not row:
            return None
        secret = _deserialize_secret(unprotect(row.get("encrypted_secret", "")))
        self.record_activity(account_id, "restore", True, f"id={row.get('backup_id')}")
        return secret

    def prune(self, account_id: str, keep: int = 5) -> int:
        """裁剪旧快照，仅保留最近 keep 份，返回删除条数。"""
        _ensure_schema()
        rows = db.all(
            "SELECT backup_id FROM credential_backups WHERE account_id=?"
            " ORDER BY created_at DESC, rowid DESC", (account_id,))
        stale = rows[max(0, int(keep)):]
        for r in stale:
            db.execute("DELETE FROM credential_backups WHERE backup_id=?", (r["backup_id"],), True)
        return len(stale)


# ---------------------------------------------------------------------------
# KeepaliveScheduler：定时心跳保活线程（回调注入，避免循环依赖）
# ---------------------------------------------------------------------------
class KeepaliveScheduler:
    """定时对「需要保活」的账号发心跳。

    集成方注入两个回调：
        lister()            -> list[dict]，元素含 id / channel
        verifier(account_id)-> bool，账号是否仍有效（内部调用渠道 verify）
    """

    def __init__(self, lister: Callable[[], List[dict]],
                 verifier: Callable[[str], bool], interval: float = 1800,
                 benefits: Optional[Callable[[str], Optional[dict]]] = None) -> None:
        self.lister = lister
        self.verifier = verifier
        # benefits(account_id) -> {claimed, detail}：verify 成功后领每日权益（签到）。
        self.benefits = benefits
        self.interval = float(interval)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- 生命周期 --------------------------------------------------------
    def start(self) -> None:
        """启动守护线程（幂等）。"""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="session-keepalive", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """用 Event 唤醒并停止线程。"""
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=5)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                pass
            self._stop.wait(self.interval)

    # ---- 单轮执行 --------------------------------------------------------
    def run_once(self) -> dict:
        """执行一轮心跳，返回 {checked, ok, failed, skipped}。"""
        _ensure_schema()
        result = {"checked": 0, "ok": 0, "failed": 0, "skipped": 0}
        try:
            accounts = self.lister() or []
        except Exception:
            return result
        for acct in accounts:
            aid = str((acct or {}).get("id") or "")
            if not aid:
                continue
            if not keeper.needs_keepalive(aid, None, self.interval):
                result["skipped"] += 1
                continue
            result["checked"] += 1
            detail = ""
            try:
                ok = bool(self.verifier(aid))
            except Exception as exc:
                ok = False
                detail = f"{type(exc).__name__}: {exc}"
            keeper.record_activity(aid, "keepalive", ok, detail)
            result["ok" if ok else "failed"] += 1
            # verify 成功后顺手领每日权益（签到类，正常用户行为）。
            # 由集成方注入 benefits 回调（session 层不依赖 channels，防循环引用）。
            if ok and self.benefits is not None:
                try:
                    res = self.benefits(aid) or {}
                    if res.get("claimed"):
                        keeper.record_activity(aid, "benefits", True,
                                               str(res.get("detail"))[:80])
                except Exception:  # noqa: BLE001 签到失败不影响保活状态
                    pass
        return result


# 模块底部单例
keeper = SessionKeeper()
