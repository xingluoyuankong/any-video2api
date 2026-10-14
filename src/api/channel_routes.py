# -*- coding: utf-8 -*-
"""渠道管理 API —— 平台"新增渠道"能力的对外接口。

    GET  /api/channels                     列出全部已注册渠道及能力
    GET  /api/channels/{name}              单渠道详情（描述符 + 能力 + 端点）
    GET  /api/channels/{name}/endpoints    上游端点映射（逆向结果落地点）
    GET  /api/channels/{name}/capabilities 原子能力
    POST /api/channels/{name}/verify       用指定账号验证渠道登录态
    POST /api/channels/{name}/models       拉取该渠道实时模型目录
    POST /api/channels/{name}/credits      拉取该渠道账号额度
    POST /api/channels/reload              热重载 config/channels/*.json（新增渠道后无需重启）
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from src.channels.registry import registry
from src.platform.accounts import manager

router = APIRouter()


class VerifyBody(BaseModel):
    account_id: str


class AccountScopedBody(BaseModel):
    account_id: Optional[str] = None


@router.get("/api/channels")
def list_channels():
    return {"count": len(registry.names()), "channels": registry.describe(),
            "load_errors": registry.errors()}


@router.get("/api/channels/{name}")
def get_channel(name: str):
    ch = registry.get(name)
    if ch is None:
        raise HTTPException(status_code=404, detail=f"渠道未注册: {name}")
    return {"name": name, "display_name": ch.spec.display_name or name, "kind": ch.spec.kind,
            "base_url": ch.spec.base_url, "transports": ch.spec.transports,
            "capabilities": ch.capabilities(), "models": ch.spec.models,
            "endpoints": ch.endpoint_map()}


@router.get("/api/channels/{name}/endpoints")
def channel_endpoints(name: str):
    ch = registry.get(name)
    if ch is None:
        raise HTTPException(status_code=404, detail=f"渠道未注册: {name}")
    return {"channel": name, "endpoints": ch.endpoint_map()}


@router.get("/api/channels/{name}/capabilities")
def channel_capabilities(name: str):
    ch = registry.get(name)
    if ch is None:
        raise HTTPException(status_code=404, detail=f"渠道未注册: {name}")
    return {"channel": name, "capabilities": ch.capabilities()}


@router.post("/api/channels/{name}/verify")
def channel_verify(name: str, body: VerifyBody):
    ch = registry.get(name)
    if ch is None:
        raise HTTPException(status_code=404, detail=f"渠道未注册: {name}")
    row = manager.raw(body.account_id)
    if not row:
        raise HTTPException(status_code=404, detail="账号不存在")
    res = ch.verify(manager.secret(body.account_id))
    manager.mark(body.account_id, status=res.status)
    return {"channel": name, "account_id": body.account_id, "ok": res.ok,
            "status": res.status, "message": res.message}


@router.post("/api/channels/{name}/models")
def channel_models(name: str, body: AccountScopedBody):
    ch = registry.get(name)
    if ch is None:
        raise HTTPException(status_code=404, detail=f"渠道未注册: {name}")
    aid = body.account_id or (manager.pick(name) or {}).get("id")
    if not aid:
        raise HTTPException(status_code=409, detail="没有可用账号")
    models = [{"id": m.id, "alias": m.alias, "kind": m.kind, "resolutions": m.resolutions}
              for m in ch.list_models(manager.secret(aid))]
    manager.set_capabilities(aid, models)
    return {"channel": name, "account_id": aid, "count": len(models), "models": models}


@router.post("/api/channels/{name}/credits")
def channel_credits(name: str, body: AccountScopedBody):
    ch = registry.get(name)
    if ch is None:
        raise HTTPException(status_code=404, detail=f"渠道未注册: {name}")
    aid = body.account_id or (manager.pick(name) or {}).get("id")
    if not aid:
        raise HTTPException(status_code=409, detail="没有可用账号")
    view = ch.credits(manager.secret(aid))
    saved = manager.save_credits(aid, view)
    return {"channel": name, "account_id": aid, "credit": saved}


@router.post("/api/channels/reload")
def reload_channels():
    registry.load()
    return {"reloaded": True, "channels": registry.names(), "errors": registry.errors()}
