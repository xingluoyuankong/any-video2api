# -*- coding: utf-8 -*-
"""批量运维 + 模型目录 API。

批量运维（/api/ops/*）：串行执行，单个账号失败不影响后续，逐条返回结果。
    POST /api/ops/checkin-all      逐账号签到
    POST /api/ops/keepalive-all    触发一轮会话保活
    POST /api/ops/balance-all      逐账号刷新额度
    POST /api/ops/verify-all       逐账号检查登录态
    POST /api/ops/antiban-reset    重置熔断
    GET  /api/ops/status           运行态汇总

模型目录（/api/models*）：描述符静态映射 + 账号实时目录。
    GET  /api/models                    聚合全部渠道模型
    GET  /api/models/{channel}          单渠道模型明细
    POST /api/models/{channel}/probe    探测实时目录并写入能力表
    GET  /api/models/{channel}/specs    模型规格与能力
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from src.channels.registry import registry
from src.platform.accounts import manager
from src.platform.antiban import circuit, rate_limiter
from src.platform.db import db, now
from src.platform.jobs import store
from src.platform.session import keeper

router = APIRouter()


# ---------------------------------------------------------------------------
# 批量运维
# ---------------------------------------------------------------------------
@router.post("/api/ops/checkin-all")
def checkin_all(channel: Optional[str] = Query(None)):
    """逐账号签到；串行执行，单账号失败不中断。"""
    results: List[dict] = []
    ok = failed = 0
    for row in manager.raws(channel):
        aid, ch_name = row["id"], row["channel"]
        ch = registry.get(ch_name)
        if ch is None:
            status, message = "no_channel", f"渠道未注册: {ch_name}"
        else:
            try:
                res = ch.checkin(manager.secret(aid))
                status = str(res.get("status") or "unknown")
                message = str(res.get("message") or res.get("reason") or "")
            except Exception as exc:
                status, message = "error", f"{type(exc).__name__}: {exc}"
        if status == "success":
            ok += 1
        else:
            failed += 1
        # 落签到记录（失败不影响返回）
        try:
            db.execute("INSERT INTO checkin_runs(id,channel,account_id,started_at,finished_at,status,message)"
                       " VALUES(?,?,?,?,?,?,?)",
                       ("ci_" + uuid.uuid4().hex[:12], ch_name, aid, now(), now(),
                        status, message[:500]), True)
            db.execute("UPDATE accounts SET last_checkin_at=?,last_checkin_status=? WHERE id=?",
                       (now(), status, aid), True)
        except Exception:
            pass
        results.append({"account_id": aid, "channel": ch_name, "status": status, "message": message})
    return {"total": len(results), "ok": ok, "failed": failed, "results": results}


@router.post("/api/ops/keepalive-all")
def keepalive_all():
    """手动触发一轮会话保活。"""
    import main  # 延迟导入：main 是项目根模块，避免循环依赖
    return main.keepalive.run_once()


@router.post("/api/ops/balance-all")
def balance_all(channel: Optional[str] = Query(None)):
    """逐账号刷新额度（credits -> save_credits）；串行执行，单账号失败不中断。"""
    results: List[dict] = []
    ok = failed = 0
    for row in manager.raws(channel):
        aid, ch_name = row["id"], row["channel"]
        ch = registry.get(ch_name)
        if ch is None:
            results.append({"account_id": aid, "channel": ch_name, "status": "no_channel",
                            "message": f"渠道未注册: {ch_name}"})
            failed += 1
            continue
        try:
            view = ch.credits(manager.secret(aid))
            saved = manager.save_credits(aid, view)
            err = str(getattr(view, "error", "") or "")
            status = "error" if err else "ok"
            message = err or f"total={saved.get('total')} allocatable={saved.get('allocatable')}"
            results.append({"account_id": aid, "channel": ch_name, "status": status,
                            "message": message, "total": saved.get("total"),
                            "allocatable": saved.get("allocatable")})
        except Exception as exc:
            results.append({"account_id": aid, "channel": ch_name, "status": "error",
                            "message": f"{type(exc).__name__}: {exc}"})
        if results[-1]["status"] == "ok":
            ok += 1
        else:
            failed += 1
    return {"total": len(results), "ok": ok, "failed": failed, "results": results}


@router.post("/api/ops/verify-all")
def verify_all(channel: Optional[str] = Query(None)):
    """逐账号检查登录态（verify -> manager.mark）；串行执行，单账号失败不中断。"""
    results: List[dict] = []
    ok = failed = 0
    for row in manager.raws(channel):
        aid, ch_name = row["id"], row["channel"]
        ch = registry.get(ch_name)
        if ch is None:
            results.append({"account_id": aid, "channel": ch_name, "status": "no_channel",
                            "message": f"渠道未注册: {ch_name}"})
            failed += 1
            continue
        try:
            res = ch.verify(manager.secret(aid))
            manager.mark(aid, status=res.status)
            results.append({"account_id": aid, "channel": ch_name, "status": res.status,
                            "message": res.message, "ok": bool(res.ok)})
        except Exception as exc:
            results.append({"account_id": aid, "channel": ch_name, "status": "error",
                            "message": f"{type(exc).__name__}: {exc}", "ok": False})
        if results[-1].get("ok"):
            ok += 1
        else:
            failed += 1
    return {"total": len(results), "ok": ok, "failed": failed, "results": results}


@router.post("/api/ops/antiban-reset")
def antiban_reset(account_id: Optional[str] = Query(None), channel: Optional[str] = Query(None)):
    """重置熔断：指定账号、指定渠道，或全部。"""
    if account_id and channel:
        key = f"{channel}:{account_id}"
        circuit.record(key, "success")
        return {"reset": [key]}
    if channel:
        keys = [k for k in circuit.states() if k.startswith(f"{channel}:")]
        for k in keys:
            circuit.record(k, "success")
        return {"reset": keys}
    circuit._st.clear()  # noqa: SLF001  (显式全量重置)
    return {"reset": "all"}


@router.get("/api/ops/status")
def ops_status():
    """运行态汇总：任务 / 账号 / 防风控 / 会话。"""
    rows = db.all("SELECT status, COUNT(*) c FROM jobs GROUP BY status")
    return {
        "jobs": {"pending": len(store.pending()),
                 "by_status": {r["status"]: r["c"] for r in rows}},
        "accounts": manager.summary(),
        "antiban": {"rate_limiter": rate_limiter.snapshot_all(), "circuit": circuit.states()},
        "sessions": keeper.status(),
    }


# ---------------------------------------------------------------------------
# 模型目录
# ---------------------------------------------------------------------------
class ProbeBody(BaseModel):
    """模型探测请求：可指定账号，缺省用该渠道首个可用账号。"""

    account_id: Optional[str] = None


def _require_channel(name: str):
    ch = registry.get(name)
    if ch is None:
        raise HTTPException(status_code=404, detail=f"渠道未注册: {name}")
    return ch


def _first_account(name: str) -> Optional[str]:
    """取该渠道首个可用账号（只读，不改变调度游标）。"""
    rows = manager.active(name)
    return rows[0]["id"] if rows else None


def _spec_rows(ch, available: bool = False) -> List[dict]:
    """由描述符 spec.models（别名 -> 上游 ID）生成统一行。

    available：渠道是否有可用账号（无凭据 = 不可用）。
    price：渠道 config extra.model_prices[alias] 若配置了则下发。
    """
    prices = (ch.spec.extra or {}).get("model_prices") or {}
    out = []
    for alias, upstream in (ch.spec.models or {}).items():
        out.append({"channel": ch.spec.name, "id": str(alias), "alias": str(alias),
                    "display_name": str(alias), "kind": ch.spec.kind,
                    "upstream_id": str(upstream),
                    "available": bool(available),
                    "price": str(prices.get(alias, ""))})
    return out


def _live_rows(ch, aid: str, available: bool = True) -> List[dict]:
    """由账号实时 list_models() 生成统一行。"""
    prices = (ch.spec.extra or {}).get("model_prices") or {}
    out = []
    for m in ch.list_models(manager.secret(aid)):
        out.append({"channel": ch.spec.name, "id": m.id, "alias": m.alias or m.id,
                    "display_name": m.display_name or m.id, "kind": m.kind,
                    "upstream_id": (ch.spec.models or {}).get(m.id, m.id),
                    "available": bool(available),
                    "price": str(prices.get(m.alias or m.id, ""))})
    return out


@router.get("/api/models")
def list_models(live: bool = Query(True)):
    """聚合全部渠道模型：描述符静态映射 + （有账号时）实时目录。

    每行带 available（渠道是否有可用账号）与 price（如配置了 model_prices）。
    """
    models: List[dict] = []
    by_channel: Dict[str, int] = {}
    available_map: Dict[str, bool] = {}
    for name in registry.names():
        ch = registry.get(name)
        if ch is None:
            continue
        # 渠道有 load 错误或没有 active 账号 -> 不可用
        has_account = bool(_first_account(name))
        available = has_account
        available_map[name] = available
        rows = _spec_rows(ch, available=available)
        if live and has_account:
            aid = _first_account(name)
            if aid:
                try:
                    rows += _live_rows(ch, aid, available=available)
                except Exception:
                    pass
        by_channel[name] = len(rows)
        models += rows
    return {"models": models, "by_channel": by_channel, "available_map": available_map}


@router.get("/api/models/{channel}")
def channel_models(channel: str):
    """单渠道模型明细（描述符映射 + 实时目录）。"""
    ch = _require_channel(channel)
    aid = _first_account(channel)
    live = []
    if aid:
        try:
            live = ch.list_models(manager.secret(aid))
        except Exception:
            live = []
    return {
        "channel": channel,
        "display_name": ch.spec.display_name or channel,
        "kind": ch.spec.kind,
        "account_id": aid,
        "spec_models": ch.spec.models or {},
        "spec": _spec_rows(ch),
        "live": [{"id": m.id, "alias": m.alias, "display_name": m.display_name, "kind": m.kind,
                  "resolutions": m.resolutions, "durations": m.durations, "ratios": m.ratios,
                  "supports_image_input": m.supports_image_input,
                  "supports_long_video": m.supports_long_video} for m in live],
    }


@router.post("/api/models/{channel}/probe")
def probe_models(channel: str, body: ProbeBody):
    """用指定/默认账号探测实时模型目录，并写入账号能力表。"""
    ch = _require_channel(channel)
    aid = body.account_id or _first_account(channel)
    if not aid:
        raise HTTPException(status_code=400, detail="没有可用账号，无法探测实时模型目录")
    if not manager.raw(aid):
        raise HTTPException(status_code=404, detail="账号不存在")
    try:
        specs = ch.list_models(manager.secret(aid))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"上游模型目录探测失败: {exc}")
    models = [{"id": m.id, "alias": m.alias, "display_name": m.display_name,
               "kind": m.kind, "available": True} for m in specs]
    manager.set_capabilities(aid, models)
    return {"channel": channel, "account_id": aid, "count": len(models), "models": models}


@router.get("/api/models/{channel}/specs")
def model_specs(channel: str):
    """模型规格与能力：spec.models + spec.supports + spec.endpoints 汇总。"""
    ch = _require_channel(channel)
    spec = ch.spec
    return {
        "channel": channel,
        "display_name": spec.display_name or channel,
        "kind": spec.kind,
        "supports": spec.supports or {},
        "endpoints": spec.endpoints or {},
        "models": [{"id": str(a), "upstream_id": str(u)}
                   for a, u in (spec.models or {}).items()],
        "count": len(spec.models or {}),
    }
