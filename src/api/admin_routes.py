# -*- coding: utf-8 -*-
"""管理 API：账号 CRUD、任务、密钥、审计、签到、健康、汇总。"""
from __future__ import annotations

import json
import uuid
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel

from src.channels.registry import registry
from src.core.models import AccountImport, AccountPatch, KeyCreate
from src.platform import audit
from src.platform.accounts import manager
from src.platform.antiban import antiban, circuit, fingerprints, proxy_binder, rate_limiter
from src.platform.db import db, now
from src.platform.jobs import store, worker
from src.platform.keys import keys
from src.platform.session import keeper

router = APIRouter()


def _parse_cookies(raw) -> dict:
    """兼容 Cookie-Editor 数组 / {cookies:[...]} / 扁平 {name:value}。"""
    if raw is None:
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return {}
    if isinstance(raw, list):
        return {c["name"]: c.get("value", "") for c in raw if isinstance(c, dict) and "name" in c}
    if isinstance(raw, dict):
        if isinstance(raw.get("cookies"), list):
            return {c["name"]: c.get("value", "") for c in raw["cookies"]
                    if isinstance(c, dict) and "name" in c}
        return {str(k): ("" if v is None else str(v)) for k, v in raw.items()
                if isinstance(v, (str, int, float, bool)) or v is None}
    return {}


# ---- 健康 / 汇总 ----
@router.get("/health")
def health():
    return {"status": "ok", "channels": registry.names(), "channel_errors": registry.errors(),
            "accounts": manager.summary(), "pending_jobs": len(store.pending())}


@router.get("/api/summary")
def summary():
    total = db.one("SELECT COUNT(*) c FROM jobs") or {"c": 0}
    by_status = db.all("SELECT status, COUNT(*) c FROM jobs GROUP BY status")
    return {"accounts": manager.summary(), "jobs_total": total["c"],
            "jobs_by_status": {r["status"]: r["c"] for r in by_status},
            "channels": registry.describe()}


# ---- 账号 ----
@router.get("/api/accounts")
def list_accounts(channel: Optional[str] = Query(None)):
    return {"accounts": manager.list(channel)}


@router.get("/api/accounts/{aid}")
def get_account(aid: str):
    acc = manager.get(aid)
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    return acc


# ---- 在线登录（把渠道的 login_* 暴露成 HTTP，供控制台「导入账号」使用）----

class LoginSmsSendBody(BaseModel):
    channel: str
    phone: str
    area_code: str = "86"


class LoginSmsBody(BaseModel):
    channel: str
    phone: str
    code: str
    area_code: str = "86"
    name: str = ""
    proxy: str = ""
    note: str = ""


class LoginPasswordBody(BaseModel):
    channel: str
    account: str
    password: str
    name: str = ""
    proxy: str = ""
    note: str = ""


def _channel_or_404(name: str):
    ch = registry.get(name)
    if ch is None:
        raise HTTPException(status_code=404, detail=f"渠道不存在：{name}")
    return ch


@router.get("/api/accounts/login/methods")
def login_methods(channel: str = Query(...)):
    """返回该渠道声明的在线登录方式。"""
    ch = _channel_or_404(channel)
    try:
        methods = ch.login_methods()
    except Exception:  # noqa: BLE001
        methods = []
    return {"channel": channel, "methods": methods}


@router.post("/api/accounts/login/sms/send")
def login_sms_send(body: LoginSmsSendBody):
    ch = _channel_or_404(body.channel)
    try:
        return {"ok": True, "result": ch.login_send_sms(body.phone, body.area_code)}
    except NotImplementedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"发送验证码失败：{exc}")


@router.post("/api/accounts/login/sms")
def login_sms(body: LoginSmsBody):
    ch = _channel_or_404(body.channel)
    try:
        secret = ch.login_by_sms(body.phone, body.code, body.area_code)
    except NotImplementedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"登录失败：{exc}")
    if secret is None:
        raise HTTPException(status_code=502, detail="登录失败：渠道未返回凭据")
    return manager.add(body.channel, name=body.name or body.phone,
                       cookies=secret.cookies, oauth=secret.oauth,
                       browser_state=secret.browser_state, extra=secret.extra,
                       proxy=body.proxy or None, note=body.note)


@router.post("/api/accounts/login/password")
def login_password(body: LoginPasswordBody):
    ch = _channel_or_404(body.channel)
    try:
        secret = ch.login_by_password(body.account, body.password)
    except NotImplementedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"登录失败：{exc}")
    if secret is None:
        raise HTTPException(status_code=502, detail="登录失败：渠道未返回凭据")
    return manager.add(body.channel, name=body.name or body.account,
                       cookies=secret.cookies, oauth=secret.oauth,
                       browser_state=secret.browser_state, extra=secret.extra,
                       proxy=body.proxy or None, note=body.note)


@router.get("/api/accounts/login/oauth")
def login_oauth(channel: str = Query(...)):
    """返回授权链接 / 书签脚本（不启动浏览器）。"""
    ch = _channel_or_404(channel)
    try:
        return ch.login_oauth()
    except NotImplementedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"生成授权链接失败：{exc}")


@router.post("/api/accounts/import")
def import_account(body: AccountImport):
    if registry.get(body.channel) is None:
        raise HTTPException(status_code=400, detail=f"未知渠道: {body.channel}")
    acc = manager.add(body.channel, name=body.name, cookies=body.cookies,
                      browser_state=body.browser_state or {}, oauth=body.oauth or {},
                      profile_path=body.profile_path or "", proxy=body.proxy,
                      site_region=body.site_region, note=body.note)
    return acc


@router.post("/api/accounts/import-file")
async def import_account_file(channel: str = Form("jimeng"), name: str = Form(""),
                              proxy: str = Form(""), file: UploadFile = File(...)):
    if registry.get(channel) is None:
        raise HTTPException(status_code=400, detail=f"未知渠道: {channel}")
    raw = await file.read()
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="文件不是合法 JSON")
    cookies = data
    acc = manager.add(channel, name=name or file.filename, cookies=cookies, proxy=proxy)
    return acc


@router.patch("/api/accounts/{aid}")
def patch_account(aid: str, body: AccountPatch):
    acc = manager.update(aid, body.model_dump(exclude_none=True))
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    return acc


@router.delete("/api/accounts/{aid}")
def delete_account(aid: str):
    manager.delete(aid)
    return {"deleted": aid}


@router.post("/api/accounts/{aid}/cookies")
def set_cookies(aid: str, cookies: dict = None):
    if not manager.raw(aid):
        raise HTTPException(status_code=404, detail="账号不存在")
    manager.set_secrets(aid, cookies=cookies or {})
    return manager.get(aid)


@router.post("/api/accounts/{aid}/check")
def check_account(aid: str):
    row = manager.raw(aid)
    if not row:
        raise HTTPException(status_code=404, detail="账号不存在")
    ch = registry.get(row["channel"])
    if ch is None:
        raise HTTPException(status_code=400, detail=f"渠道未注册: {row['channel']}")
    res = ch.verify(manager.secret(aid))
    manager.mark(aid, status=res.status)
    return {"account_id": aid, "ok": res.ok, "status": res.status, "message": res.message}


@router.post("/api/accounts/check-all")
def check_all(channel: Optional[str] = Query(None)):
    out = []
    for row in manager.raws(channel):
        ch = registry.get(row["channel"])
        if ch is None:
            out.append({"account_id": row["id"], "ok": False, "status": "no_channel"})
            continue
        res = ch.verify(manager.secret(row["id"]))
        manager.mark(row["id"], status=res.status)
        out.append({"account_id": row["id"], "channel": row["channel"], "ok": res.ok, "status": res.status})
    return {"results": out}


# ---- 任务 ----
@router.get("/api/jobs")
def list_jobs(status: Optional[str] = Query(None), channel: Optional[str] = Query(None),
              limit: int = Query(100)):
    return {"jobs": store.list(status=status, channel=channel, limit=limit)}


@router.get("/api/jobs/{jid}")
def get_job(jid: str):
    job = store.get(jid)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


@router.delete("/api/jobs/{jid}")
def cancel_job(jid: str):
    job = store.cancel(jid)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


# ---- 密钥 ----
@router.get("/api/keys")
def list_keys():
    return {"keys": keys.list()}


@router.post("/api/keys")
def create_key(body: KeyCreate):
    return keys.create(body.name, body.note, body.rpm_limit, body.max_concurrent,
                       body.allowed_channels)


@router.get("/api/keys/{kid}/reveal")
def reveal_key(kid: str):
    val = keys.reveal(kid)
    if not val:
        raise HTTPException(status_code=404, detail="密钥不存在")
    return {"id": kid, "api_key": val}


@router.post("/api/keys/{kid}/enable")
def enable_key(kid: str):
    return keys.set_enabled(kid, True) or {}


@router.post("/api/keys/{kid}/disable")
def disable_key(kid: str):
    return keys.set_enabled(kid, False) or {}


@router.delete("/api/keys/{kid}")
def delete_key(kid: str):
    keys.delete(kid)
    return {"deleted": kid}


# ---- 审计 ----
@router.get("/api/audit")
def get_audit(limit: int = Query(100), channel: str = Query("")):
    return {"events": audit.recent(limit=limit, channel=channel)}


# ---- 签到 ----
@router.post("/api/accounts/{aid}/checkin")
def checkin_account(aid: str):
    row = manager.raw(aid)
    if not row:
        raise HTTPException(status_code=404, detail="账号不存在")
    ch = registry.get(row["channel"])
    if ch is None:
        raise HTTPException(status_code=400, detail=f"渠道未注册: {row['channel']}")
    rid = "ci_" + uuid.uuid4().hex[:12]
    started = now()
    result = ch.checkin(manager.secret(aid))
    status = result.get("status", "unknown")
    db.execute("INSERT INTO checkin_runs(id,channel,account_id,started_at,finished_at,status,message)"
               " VALUES(?,?,?,?,?,?,?)",
               (rid, row["channel"], aid, started, now(), status, str(result.get("message") or "")[:500]), True)
    db.execute("UPDATE accounts SET last_checkin_at=?,last_checkin_status=? WHERE id=?",
               (now(), status, aid), True)
    return {"account_id": aid, "channel": row["channel"], "status": status, "result": result}


@router.get("/api/checkins")
def list_checkins(limit: int = Query(100)):
    return {"checkins": db.all("SELECT * FROM checkin_runs ORDER BY started_at DESC LIMIT ?", (limit,))}


# ---- 防风控 ----
@router.get("/api/antiban/status")
def antiban_status():
    # proxy_bindings 表由 antiban.ProxyBinder 惰性创建，首次调用前可能不存在
    try:
        bindings = db.all("SELECT account_id,channel,proxy,bound_at FROM proxy_bindings")
    except Exception:
        bindings = []
    return {"rate_limiter": rate_limiter.snapshot_all(), "circuit": circuit.states(),
            "proxy_bindings": bindings}


@router.post("/api/antiban/reset")
def antiban_reset(account_id: Optional[str] = Query(None), channel: Optional[str] = Query(None)):
    """重置熔断器：指定账号、指定渠道，或全部。"""
    if account_id and channel:
        circuit.record(f"{channel}:{account_id}", "success")
        return {"reset": f"{channel}:{account_id}"}
    circuit._st.clear()  # noqa: SLF001  (显式全量重置)
    return {"reset": "all"}


# ---- 会话保活 / 登录态 ----
@router.get("/api/sessions")
def sessions():
    return keeper.status()


@router.get("/api/accounts/{aid}/session")
def account_session(aid: str):
    snap = manager.session_snapshot(aid)
    if not snap:
        raise HTTPException(status_code=404, detail="账号不存在")
    return snap


@router.get("/api/accounts/{aid}/backups")
def account_backups(aid: str):
    if not manager.raw(aid):
        raise HTTPException(status_code=404, detail="账号不存在")
    return {"account_id": aid, "backups": keeper.list_backups(aid)}


@router.post("/api/accounts/{aid}/backups")
def account_backup_now(aid: str, tag: str = Query("manual")):
    if not manager.raw(aid):
        raise HTTPException(status_code=404, detail="账号不存在")
    return manager.backup_now(aid, tag=tag)


@router.post("/api/accounts/{aid}/restore")
def account_restore(aid: str, backup_id: Optional[str] = Query(None)):
    if not manager.raw(aid):
        raise HTTPException(status_code=404, detail="账号不存在")
    ok = manager.restore_secret(aid, backup_id)
    if not ok:
        raise HTTPException(status_code=404, detail="没有可用快照")
    return {"account_id": aid, "restored": True, "backup_id": backup_id or "latest"}


@router.post("/api/accounts/{aid}/fingerprint/rotate")
def account_rotate_fingerprint(aid: str):
    if not manager.raw(aid):
        raise HTTPException(status_code=404, detail="账号不存在")
    return {"account_id": aid, "fingerprint": manager.rotate_fingerprint(aid)}


@router.post("/api/keepalive/run")
def keepalive_run():
    """手动触发一轮会话保活（便于验证与运维）。"""
    from main import keepalive
    return keepalive.run_once()
