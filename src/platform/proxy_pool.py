# -*- coding: utf-8 -*-
"""账号级代理池 + 出口校验 + 订阅池（移植 workbuddy2api-panel 的核心设计）。

核心认知：全局共享代理不够用——风控按「同一 IP 短时多账号」聚号，真正该隔离的
粒度是 **一个账号 ↦ 一条固定出口代理**（出口 IP 恒定、不复用、地理自洽）。

但配了还不够，必须校验：配错 / 失效时代理照样「连得上」，出口 IP 却没变，
线上几乎不可观测。所以把「这条代理现在真的按声明 IP 出去吗」变成一等状态：

    unchecked     未校验
    ok            出口 IP 与声明一致（未声明则只要代理生效即 ok）
    not_effective 实测出口 IP == 本机直连出口 IP（代理没生效）
    mismatch      实测出口 IP != 声明的 expected_ip
    unreachable   代理连不上 / 缺少 socks 依赖
    disabled      绑定已禁用

同 IP 承载账号数（超限是风控靶心）由 stats() 统计。

自包含单文件，复用平台公共件（config / db / accounts 只读 / errors），
**不依赖** src.channels.* 与 src.api.*。表结构惰性创建，import 无副作用。

集成方调用点：
    传输层取代理   proxy_pool.proxy_for(account_id)   -> str | None（None=走默认出口）
    后台巡检       proxy_checker.check_all(channel)     -> 逐账号实测出口
    订阅池分配     sub_pool.save(realm, subs) / sub_pool.autobind(account_id)
"""
from __future__ import annotations

import base64
import ipaddress
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from src.core.config import config
from src.core.errors import redact_text
from src.platform.accounts import manager
from src.platform.db import db, jd, jl, now

# ---------------------------------------------------------------------------
# 惰性建表（不改 db.py 的 SCHEMA）
# ---------------------------------------------------------------------------
_DDL: Tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS account_proxies("
    "account_id TEXT PRIMARY KEY, channel TEXT, proxy TEXT, expected_ip TEXT, "
    "enabled INTEGER DEFAULT 1, auto INTEGER DEFAULT 0, label TEXT, note TEXT, "
    "created_at REAL, updated_at REAL)",
    "CREATE TABLE IF NOT EXISTS proxy_runtime("
    "account_id TEXT PRIMARY KEY, state TEXT, actual_ip TEXT, checked_at REAL, last_error TEXT)",
    "CREATE TABLE IF NOT EXISTS proxy_pools("
    "realm TEXT PRIMARY KEY, subs_json TEXT, refreshed_at REAL, last_error TEXT)",
    "CREATE TABLE IF NOT EXISTS proxy_pool_entries("
    "realm TEXT, proxy TEXT, added_at REAL, ok INTEGER DEFAULT 0, last_ok_at REAL, "
    "fail_count INTEGER DEFAULT 0, PRIMARY KEY(realm, proxy))",
)

_ddl_lock = threading.RLock()
_ddl_done = False


def _ensure_tables() -> None:
    """首次调用时惰性建表（幂等，线程安全）。"""
    global _ddl_done
    if _ddl_done:
        return
    with _ddl_lock:
        if _ddl_done:
            return
        for ddl in _DDL:
            db.execute(ddl, (), True)
        _ddl_done = True


# ---------------------------------------------------------------------------
# 配置读取（统一走 config.get("proxy", <key>, <default>)）
# ---------------------------------------------------------------------------
def _probe_timeout(default: float = 15.0) -> float:
    try:
        return float(config.get("proxy", "probe_timeout", default))
    except Exception:
        return default


def _max_per_ip() -> int:
    """同一出口 IP 允许承载的账号数上限（超过即风控靶心）。"""
    try:
        return max(1, int(config.get("proxy", "max_per_ip", 3)))
    except Exception:
        return 3


def _dead_threshold() -> int:
    """订阅池条目连续失败达到该值即视为 dead。"""
    try:
        return max(1, int(config.get("proxy", "dead_threshold", 3)))
    except Exception:
        return 3


# ---------------------------------------------------------------------------
# 代理链接脱敏：user:pass@ -> user:***@（供所有对外返回的 dict 使用）
# ---------------------------------------------------------------------------
_PROXY_CRED_RE = re.compile(
    r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)(?P<userinfo>[^@/]+)@(?P<rest>.+)$"
)


def mask_proxy(proxy: str) -> str:
    """把代理链接里的 user:pass 脱敏为 user:***@；无凭据则原样返回。"""
    if not proxy:
        return ""
    m = _PROXY_CRED_RE.match(proxy)
    if not m:
        return proxy
    userinfo = m.group("userinfo")
    if ":" in userinfo:
        userinfo = userinfo.split(":", 1)[0] + ":***"
    return f"{m.group('scheme')}{userinfo}@{m.group('rest')}"


# ---------------------------------------------------------------------------
# 出口 IP 探测
# ---------------------------------------------------------------------------
_ECHO_ENDPOINTS: Tuple[Tuple[str, str], ...] = (
    ("https://api.ipify.org?format=json", "json:ip"),
    ("https://httpbin.org/ip", "json:origin"),
    ("https://ifconfig.me/ip", "text"),
)
_PROBE_UA = "curl/8.4.0"
_DIRECT_TTL = 300.0  # 直连出口 IP 进程内缓存 5 分钟
_direct_lock = threading.Lock()
_direct_cache: Dict[str, Any] = {"ip": "", "ts": 0.0}


class _ProbeError(RuntimeError):
    """出口探测失败（内部使用，绝不外抛给调用方）。"""


def _is_socks(proxy: str) -> bool:
    return proxy.split("://", 1)[0].lower() in ("socks5", "socks5h", "socks4", "socks4a")


def _ensure_socks(proxy: str) -> None:
    """socks 代理需要 PySocks（requests[socks]）；缺失时给出明确错误以便降级。"""
    if not _is_socks(proxy):
        return
    try:
        import socks  # noqa: F401
    except ImportError as e:  # pragma: no cover - 取决于运行环境
        raise _ProbeError("缺少 PySocks 依赖（pip install requests[socks]），socks 代理不可用") from e


def _is_ip(v: str) -> bool:
    try:
        ipaddress.ip_address(str(v).strip())
        return True
    except Exception:
        return False


def _parse_ip(resp: requests.Response, kind: str) -> str:
    """按回显服务类型解析出出口 IP；解析不出返回空串。"""
    if kind == "json:ip":
        data = resp.json()
        val = data.get("ip") if isinstance(data, dict) else ""
    elif kind == "json:origin":
        data = resp.json()
        val = data.get("origin") if isinstance(data, dict) else ""
        if isinstance(val, str) and "," in val:
            val = val.split(",")[0].strip()
    else:
        val = (resp.text or "").strip().splitlines()[0] if (resp.text or "").strip() else ""
    val = str(val or "").strip()
    return val if _is_ip(val) else ""


def _probe_exit_ip(proxies: Optional[Dict[str, str]], timeout: float) -> str:
    """按顺序请求回显服务，返回首个可解析的出口 IP；全失败抛 _ProbeError。"""
    last_err = ""
    for url, kind in _ECHO_ENDPOINTS:
        try:
            r = requests.get(url, proxies=proxies, timeout=timeout,
                             headers={"User-Agent": _PROBE_UA})
            if r.status_code >= 400:
                last_err = f"{url} HTTP {r.status_code}"
                continue
            ip = _parse_ip(r, kind)
            if ip:
                return ip
            last_err = f"{url} 响应无法解析出 IP"
        except Exception as e:
            msg = str(e)
            if "SOCKS" in msg or "socks" in msg:
                last_err = "缺少 PySocks 依赖（pip install requests[socks]），socks 代理不可用"
            else:
                last_err = f"{url}: {redact_text(msg)[:200]}"
            continue
    raise _ProbeError(last_err or "所有回显服务均失败")


def _direct_exit_ip(timeout: float) -> str:
    """本机直连出口 IP（无代理），进程内缓存 5 分钟；失败返回空串（不抛）。"""
    with _direct_lock:
        ip = str(_direct_cache.get("ip") or "")
        ts = float(_direct_cache.get("ts") or 0.0)
        if ip and (time.time() - ts) < _DIRECT_TTL:
            return ip
    try:
        ip = _probe_exit_ip(None, timeout)
    except Exception:
        ip = ""
    with _direct_lock:
        if ip:
            _direct_cache["ip"] = ip
            _direct_cache["ts"] = time.time()
    return ip


def _decode_subscription(text: str) -> str:
    """订阅内容：一行一条代理，或整体 base64 编码；返回解码后的明文。"""
    s = (text or "").strip()
    if "://" in s:
        return s
    try:
        raw = base64.b64decode(s + "=" * (-len(s) % 4), validate=False)
        dec = raw.decode("utf-8", "ignore")
        if "://" in dec:
            return dec
    except Exception:
        pass
    return s


def _parse_line(line: str) -> Tuple[str, str, str, str, str]:
    """解析批量文本行 `account_id 代理链接 [声明IP] [备注]`。

    返回 (account_id, proxy, expected_ip, note, error)；error 非空表示该行无效。
    分隔符：空格 / Tab / 逗号 / 分号。
    """
    parts = [p for p in re.split(r"[\s,;]+", str(line).strip()) if p]
    if len(parts) < 2:
        return (parts[0] if parts else "", "", "", "", "格式错误：至少需要 account_id 与代理链接")
    aid, proxy = parts[0], parts[1]
    if "://" not in proxy:
        return (aid, proxy, "", "", "代理链接缺少协议头（http:// / https:// / socks5:// ...）")
    rest = parts[2:]
    expected = ""
    if rest and _is_ip(rest[0]):
        expected = rest[0]
        rest = rest[1:]
    return (aid, proxy, expected, " ".join(rest), "")


# ---------------------------------------------------------------------------
# 账号级代理绑定
# ---------------------------------------------------------------------------
class ProxyPool:
    """账号 ↦ 固定出口代理 的绑定与视图（含声明 IP / 状态 / 同 IP 统计）。"""

    # ---- 内部读取 ----
    def _binding(self, account_id: str) -> Optional[dict]:
        return db.one("SELECT * FROM account_proxies WHERE account_id=?", (account_id,))

    def _runtime(self, account_id: str) -> Optional[dict]:
        return db.one("SELECT * FROM proxy_runtime WHERE account_id=?", (account_id,))

    def _view(self, account: dict) -> dict:
        """组装单账号代理视图；proxy 字段已脱敏，bound 表示是否已绑定。"""
        aid = account.get("id")
        b = self._binding(aid) or {}
        rt = self._runtime(aid) or {}
        proxy = b.get("proxy") or ""
        bound = bool(proxy)
        return {
            "account_id": aid,
            "channel": account.get("channel") or b.get("channel") or "",
            "name": account.get("name") or aid,
            "proxy": mask_proxy(proxy),
            "expected_ip": b.get("expected_ip") or "",
            "enabled": bool(b.get("enabled")) if b else False,
            "auto": bool(b.get("auto")) if b else False,
            "label": b.get("label") or "",
            "note": b.get("note") or "",
            "state": rt.get("state") or "unchecked",
            "actual_ip": rt.get("actual_ip") or "",
            "checked_at": rt.get("checked_at") or 0.0,
            "last_error": rt.get("last_error") or "",
            "bound": bound,
        }

    # ---- 视图 ----
    def list(self, channel: Optional[str] = None) -> List[dict]:
        """全部账号（含未绑定）的代理视图；未绑定行 bound=False（走默认出口）。"""
        _ensure_tables()
        return [self._view(a) for a in manager.raws(channel)]

    def get(self, account_id: str) -> Optional[dict]:
        """单账号代理视图；账号不存在返回 None。"""
        _ensure_tables()
        a = manager.raw(account_id)
        return self._view(a) if a else None

    # ---- 写入 ----
    def set(self, account_id: str, channel: str = "", proxy: str = "", expected_ip: str = "",
            enabled: bool = True, label: str = "", note: str = "", auto: bool = False) -> dict:
        """新增 / 更新绑定（upsert）。channel 留空则取账号自身渠道。"""
        _ensure_tables()
        ch = channel or (manager.raw(account_id) or {}).get("channel") or ""
        t = now()
        created = (self._binding(account_id) or {}).get("created_at") or t
        db.execute(
            "INSERT INTO account_proxies(account_id,channel,proxy,expected_ip,enabled,auto,"
            "label,note,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(account_id) DO UPDATE SET channel=excluded.channel,proxy=excluded.proxy,"
            "expected_ip=excluded.expected_ip,enabled=excluded.enabled,auto=excluded.auto,"
            "label=excluded.label,note=excluded.note,updated_at=excluded.updated_at",
            (account_id, ch, proxy or "", expected_ip or "", 1 if enabled else 0,
             1 if auto else 0, label or "", note or "", created, t), True)
        a = manager.raw(account_id) or {"id": account_id, "channel": ch, "name": account_id}
        return self._view(a)

    def remove(self, account_id: str) -> bool:
        """直连：删除绑定及其运行态；返回此前是否存在绑定。"""
        _ensure_tables()
        existed = bool(self._binding(account_id))
        db.execute("DELETE FROM account_proxies WHERE account_id=?", (account_id,), True)
        db.execute("DELETE FROM proxy_runtime WHERE account_id=?", (account_id,), True)
        return existed

    def bulk(self, items: Optional[List[dict]] = None, text: str = "") -> dict:
        """批量绑定；逐条返回 {account_id, ok, error, line}，**不做整体回滚**。"""
        _ensure_tables()
        results: List[dict] = []

        for it in (items or []):
            if not isinstance(it, dict):
                results.append({"account_id": "", "ok": False, "error": "条目格式错误", "line": str(it)})
                continue
            aid = str(it.get("account_id") or "").strip()
            if not aid:
                results.append({"account_id": "", "ok": False, "error": "缺少 account_id", "line": jd(it)})
                continue
            try:
                self.set(aid, str(it.get("channel") or ""), str(it.get("proxy") or ""),
                         str(it.get("expected_ip") or ""), bool(it.get("enabled", True)),
                         str(it.get("label") or ""), str(it.get("note") or ""),
                         bool(it.get("auto", False)))
                results.append({"account_id": aid, "ok": True, "error": "", "line": jd(it)})
            except Exception as e:
                results.append({"account_id": aid, "ok": False,
                                "error": redact_text(str(e)), "line": jd(it)})

        for raw_line in str(text or "").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            aid, proxy, expected, note, err = _parse_line(line)
            if err:
                results.append({"account_id": aid, "ok": False, "error": err, "line": line})
                continue
            try:
                self.set(aid, proxy=proxy, expected_ip=expected, note=note)
                results.append({"account_id": aid, "ok": True, "error": "", "line": line})
            except Exception as e:
                results.append({"account_id": aid, "ok": False,
                                "error": redact_text(str(e)), "line": line})

        ok_n = sum(1 for r in results if r["ok"])
        return {"saved": ok_n, "total": len(results), "ok": ok_n,
                "failed": len(results) - ok_n, "results": results}

    # ---- 传输层接口 ----
    def proxy_for(self, account_id: str) -> Optional[str]:
        """返回该账号应使用的**完整**代理链接（未绑定 / 禁用返回 None = 走默认出口）。"""
        _ensure_tables()
        b = self._binding(account_id)
        if not b or not b.get("proxy") or not b.get("enabled"):
            return None
        return b["proxy"]

    # ---- 统计 ----
    def stats(self) -> dict:
        """{bound, unbound, by_state:{...}, ip_counts:{ip:账号数}, over_limit:[ip...]}。

        ip_counts 按「声明 IP（缺失时回退实测 IP）」聚合，over_limit 为超 max_per_ip 的 IP。
        """
        _ensure_tables()
        bound = 0
        unbound = 0
        by_state: Dict[str, int] = {}
        ip_counts: Dict[str, int] = {}
        for a in manager.raws(None):
            aid = a.get("id")
            b = self._binding(aid) or {}
            rt = self._runtime(aid) or {}
            proxy = b.get("proxy") or ""
            if proxy:
                bound += 1
                state = rt.get("state") or "unchecked"
                by_state[state] = by_state.get(state, 0) + 1
                ip = str(b.get("expected_ip") or rt.get("actual_ip") or "").strip()
                if ip:
                    ip_counts[ip] = ip_counts.get(ip, 0) + 1
            else:
                unbound += 1
        limit = _max_per_ip()
        over_limit = sorted([ip for ip, c in ip_counts.items() if c > limit])
        return {"bound": bound, "unbound": unbound, "by_state": by_state,
                "ip_counts": ip_counts, "over_limit": over_limit}


# ---------------------------------------------------------------------------
# 出口校验
# ---------------------------------------------------------------------------
class ProxyChecker:
    """实测账号代理的真实出口 IP，并与声明比对，写回 proxy_runtime。"""

    def _save_runtime(self, account_id: str, state: str, actual_ip: str, last_error: str) -> None:
        db.execute(
            "INSERT INTO proxy_runtime(account_id,state,actual_ip,checked_at,last_error) "
            "VALUES(?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET state=excluded.state,"
            "actual_ip=excluded.actual_ip,checked_at=excluded.checked_at,last_error=excluded.last_error",
            (account_id, state, actual_ip or "", now(), last_error or ""), True)

    def check(self, account_id: str, timeout: float = 15) -> dict:
        """用绑定代理请求出口 IP 回显服务，实测并比对，写回 proxy_runtime。

        任何异常都捕获并落 state=unreachable + last_error，**绝不抛出**。
        """
        _ensure_tables()
        t = float(timeout or _probe_timeout())
        b = db.one("SELECT * FROM account_proxies WHERE account_id=?", (account_id,))
        if not b or not b.get("proxy"):
            return {"account_id": account_id, "proxy": "", "expected_ip": "", "state": "unchecked",
                    "actual_ip": "", "checked_at": 0.0, "last_error": "未绑定代理"}

        proxy = b["proxy"]
        expected = str(b.get("expected_ip") or "").strip()
        if not b.get("enabled"):
            self._save_runtime(account_id, "disabled", "", "")
            return {"account_id": account_id, "proxy": mask_proxy(proxy), "expected_ip": expected,
                    "state": "disabled", "actual_ip": "", "checked_at": now(), "last_error": ""}

        try:
            _ensure_socks(proxy)
            actual = _probe_exit_ip({"http": proxy, "https": proxy}, t)
            direct = _direct_exit_ip(t)
            if direct and actual == direct:
                state, err = "not_effective", "实测出口 IP 与本机直连出口一致，代理未生效"
            elif expected and actual != expected:
                state, err = "mismatch", f"实测出口 {actual} != 声明 {expected}"
            else:
                state, err = "ok", ""
        except Exception as e:
            actual, state, err = "", "unreachable", redact_text(str(e))[:300]

        self._save_runtime(account_id, state, actual, err)
        return {"account_id": account_id, "proxy": mask_proxy(proxy), "expected_ip": expected,
                "state": state, "actual_ip": actual, "checked_at": now(), "last_error": err}

    def check_all(self, channel: Optional[str] = None, timeout: float = 15) -> dict:
        """串行校验全部已绑定账号，返回 {checked, ok, bad, results}。"""
        _ensure_tables()
        if channel:
            rows = db.all(
                "SELECT ap.account_id FROM account_proxies ap LEFT JOIN accounts a "
                "ON a.id=ap.account_id WHERE ap.proxy IS NOT NULL AND ap.proxy<>'' AND a.channel=?",
                (channel,))
        else:
            rows = db.all("SELECT account_id FROM account_proxies WHERE proxy IS NOT NULL AND proxy<>''")

        results: List[dict] = []
        ok = bad = 0
        for r in rows:
            res = self.check(r["account_id"], timeout)
            results.append(res)
            if res["state"] == "ok":
                ok += 1
            else:
                bad += 1
        return {"checked": len(results), "ok": ok, "bad": bad, "results": results}


# ---------------------------------------------------------------------------
# 订阅池
# ---------------------------------------------------------------------------
class SubPool:
    """realm（global/cn）级订阅池：拉取订阅 -> 解析代理条目 -> 供 autobind 分配。"""

    REALMS: Tuple[str, ...] = ("global", "cn")

    def _row(self, realm: str) -> Optional[dict]:
        return db.one("SELECT * FROM proxy_pools WHERE realm=?", (realm,))

    def _subs(self, realm: str) -> List[str]:
        return jl((self._row(realm) or {}).get("subs_json"), [])

    def _pool(self, realm: str) -> dict:
        entries = db.all("SELECT ok, fail_count FROM proxy_pool_entries WHERE realm=?", (realm,))
        total = len(entries)
        thr = _dead_threshold()
        ok = sum(1 for e in entries if e.get("ok"))
        dead = sum(1 for e in entries if not e.get("ok") and (e.get("fail_count") or 0) >= thr)
        return {"ok": ok, "cooling": max(0, total - ok - dead), "dead": dead, "total": total}

    def list(self) -> List[dict]:
        """每个 realm 一行：{realm, subs, subs_count, entries, refreshed_at, last_error, pool}。"""
        _ensure_tables()
        out: List[dict] = []
        for realm in self.REALMS:
            row = self._row(realm) or {}
            subs = jl(row.get("subs_json"), [])
            pool = self._pool(realm)
            out.append({
                "realm": realm,
                "subs": subs,
                "subs_count": len(subs),
                "entries": pool["total"],
                "refreshed_at": row.get("refreshed_at") or 0.0,
                "last_error": row.get("last_error") or "",
                "pool": pool,
            })
        return out

    def save(self, realm: str, subs: List[str], mode: str = "replace") -> dict:
        """保存订阅链接（replace 整体覆盖 / append 追加去重），保存后立即 refresh。"""
        _ensure_tables()
        realm = realm or "global"
        items = [str(s).strip() for s in (subs or []) if str(s).strip()]
        if mode == "append":
            merged = list(self._subs(realm))
            for s in items:
                if s not in merged:
                    merged.append(s)
        else:
            merged = items
        db.execute(
            "INSERT INTO proxy_pools(realm,subs_json,refreshed_at,last_error) VALUES(?,?,NULL,'') "
            "ON CONFLICT(realm) DO UPDATE SET subs_json=excluded.subs_json",
            (realm, jd(merged)), True)
        return {"realm": realm, "subs_count": len(merged), "refresh": self.refresh(realm)}

    def refresh(self, realm: str) -> dict:
        """拉取订阅内容（一行一条 / 整体 base64），解析代理条目写入 proxy_pool_entries。

        失败写 last_error，不抛。返回 {realm, added, parsed, total, error}。
        """
        _ensure_tables()
        realm = realm or "global"
        added = 0
        parsed = 0
        last_err = ""
        for sub in self._subs(realm):
            try:
                text = self._fetch(sub)
            except Exception as e:
                last_err = f"{sub}: {redact_text(str(e))[:200]}"
                continue
            for line in _decode_subscription(text).splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "://" not in line:
                    continue
                parsed += 1
                cur = db.execute(
                    "INSERT INTO proxy_pool_entries(realm,proxy,added_at,ok,last_ok_at,fail_count) "
                    "VALUES(?,?,?,0,NULL,0) ON CONFLICT(realm,proxy) DO NOTHING",
                    (realm, line, now()), True)
                if cur.rowcount and cur.rowcount > 0:
                    added += 1
        total = self._pool(realm)["total"]
        db.execute("UPDATE proxy_pools SET refreshed_at=?, last_error=? WHERE realm=?",
                   (now(), last_err, realm), True)
        return {"realm": realm, "added": added, "parsed": parsed, "total": total, "error": last_err}

    def _fetch(self, url: str) -> str:
        r = requests.get(url, timeout=_probe_timeout(20), headers={"User-Agent": _PROBE_UA})
        r.raise_for_status()
        return r.text

    def clear(self, realm: str) -> None:
        """清空某 realm 的订阅与条目。"""
        _ensure_tables()
        realm = realm or "global"
        db.execute("DELETE FROM proxy_pool_entries WHERE realm=?", (realm,), True)
        db.execute("DELETE FROM proxy_pools WHERE realm=?", (realm,), True)

    def pick(self, realm: str) -> Optional[str]:
        """挑一条当前最稳的（ok=1 优先、fail_count 最小、last_ok_at 最新）；无则 None。"""
        _ensure_tables()
        realm = realm or "global"
        rows = db.all("SELECT proxy, ok, fail_count, last_ok_at FROM proxy_pool_entries WHERE realm=?",
                      (realm,))
        if not rows:
            return None
        thr = _dead_threshold()
        alive = [r for r in rows if (r.get("fail_count") or 0) < thr]
        cands = alive or rows
        cands.sort(key=lambda r: (0 if r.get("ok") else 1,
                                  r.get("fail_count") or 0,
                                  -(r.get("last_ok_at") or 0)))
        return cands[0]["proxy"]

    def stats(self, realm: str) -> dict:
        """单 realm 的订阅 / 条目 / 池健康统计。"""
        _ensure_tables()
        realm = realm or "global"
        row = self._row(realm) or {}
        subs = jl(row.get("subs_json"), [])
        pool = self._pool(realm)
        return {"realm": realm, "subs": subs, "subs_count": len(subs), "entries": pool["total"],
                "pool": pool, "refreshed_at": row.get("refreshed_at") or 0.0,
                "last_error": row.get("last_error") or ""}

    def autobind(self, account_id: str, realm: str = "") -> dict:
        """从 realm 池挑一条绑定给该账号；realm 空则用账号 site_region（否则 global）。

        池内无可用条目时返回 {ok: False, error: '...'}。
        """
        _ensure_tables()
        if not realm:
            region = str((manager.raw(account_id) or {}).get("site_region") or "").lower()
            realm = region if region in self.REALMS else "global"
        proxy = self.pick(realm)
        if not proxy:
            return {"ok": False, "error": f"realm={realm} 订阅池内无可用代理"}
        proxy_pool.set(account_id, proxy=proxy, auto=True, label=f"sub:{realm}")
        return {"ok": True, "account_id": account_id, "realm": realm, "proxy": mask_proxy(proxy)}


# ---- 全局单例 ----
proxy_pool = ProxyPool()
proxy_checker = ProxyChecker()
sub_pool = SubPool()
