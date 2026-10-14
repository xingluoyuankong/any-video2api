# -*- coding: utf-8 -*-
"""代理管理 API —— 账号出口代理的增删改查、连通性检测、批量导入、订阅池维护。

    GET  /api/proxy/accounts          账号代理列表（?channel= 可选过滤）
    POST /api/proxy/save              保存/更新单个账号的代理绑定
    POST /api/proxy/delete            删除账号代理绑定
    POST /api/proxy/direct            切直连（等价 remove）
    POST /api/proxy/check             检测单账号代理连通性（?timeout=）
    POST /api/proxy/check-all         批量检测（?channel=）
    POST /api/proxy/bulk              批量导入（items 结构化 / text 文本行）
    POST /api/proxy/autobind          按订阅池为账号自动分配代理
    GET  /api/proxy/stats             绑定数、状态分布、同 IP 计数、超限 IP
    GET  /api/proxy/pools             订阅池列表
    POST /api/proxy/pools/save        保存订阅池（realm + subs）
    POST /api/proxy/pools/refresh     刷新订阅池
    POST /api/proxy/pools/clear       清空订阅池
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from src.platform.proxy_pool import proxy_pool, proxy_checker, sub_pool

router = APIRouter()

# 订阅池作用域仅允许 global / cn
_REALMS = ("global", "cn")


def _check_realm(realm: str) -> str:
    """校验并归一化 realm，非法值抛 400。"""
    r = (realm or "").strip().lower()
    if r not in _REALMS:
        raise HTTPException(status_code=400, detail=f"realm 只能是 global / cn，收到: {realm!r}")
    return r


# ---- 请求体 ----
class SaveBody(BaseModel):
    account_id: str
    channel: Optional[str] = ""
    proxy: str
    expected_ip: Optional[str] = ""
    enabled: Optional[bool] = True
    label: Optional[str] = ""
    note: Optional[str] = ""
    auto: Optional[bool] = False


class AccountBody(BaseModel):
    account_id: str


class BulkBody(BaseModel):
    items: Optional[list] = None
    text: Optional[str] = ""


class AutobindBody(BaseModel):
    account_id: str
    realm: Optional[str] = ""


class PoolSaveBody(BaseModel):
    realm: str
    subs: list
    mode: Optional[str] = "replace"


class RealmBody(BaseModel):
    realm: str


# ---- 账号代理 ----
@router.get("/api/proxy/accounts")
def list_proxy_accounts(channel: Optional[str] = Query(None)):
    """账号代理列表，可按渠道过滤。"""
    return {"accounts": proxy_pool.list(channel=channel)}


@router.post("/api/proxy/save")
def save_proxy(body: SaveBody):
    """保存/更新单个账号的代理绑定。"""
    try:
        row = proxy_pool.set(
            body.account_id,
            channel=body.channel or "",
            proxy=body.proxy or "",
            expected_ip=body.expected_ip or "",
            enabled=bool(body.enabled),
            label=body.label or "",
            note=body.note or "",
            auto=bool(body.auto),
        )
    except Exception as exc:  # noqa: BLE001 —— 业务校验失败统一转 400
        raise HTTPException(status_code=400, detail=str(exc))
    if not row:
        raise HTTPException(status_code=400, detail="保存代理失败")
    return row


@router.post("/api/proxy/delete")
def delete_proxy(body: AccountBody):
    """删除账号的代理绑定。"""
    ok = proxy_pool.remove(body.account_id)
    if not ok:
        raise HTTPException(status_code=404, detail="代理绑定不存在")
    return {"ok": True, "account_id": body.account_id}


@router.post("/api/proxy/direct")
def direct_proxy(body: AccountBody):
    """切直连：等价于移除代理绑定。"""
    proxy_pool.remove(body.account_id)
    return {"ok": True, "account_id": body.account_id}


# ---- 连通性检测 ----
@router.post("/api/proxy/check")
def check_proxy(body: AccountBody, timeout: int = Query(15)):
    """检测单账号代理连通性。"""
    if not proxy_pool.get(body.account_id):
        raise HTTPException(status_code=404, detail="代理绑定不存在")
    try:
        return proxy_checker.check(body.account_id, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 —— 出口检测失败视为上游 502
        raise HTTPException(status_code=502, detail=f"代理检测失败: {exc}")


@router.post("/api/proxy/check-all")
def check_all_proxies(channel: Optional[str] = Query(None), timeout: int = Query(15)):
    """批量检测代理连通性，可按渠道过滤。"""
    try:
        return proxy_checker.check_all(channel=channel, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"批量检测失败: {exc}")


# ---- 批量导入 ----
@router.post("/api/proxy/bulk")
def bulk_proxy(body: BulkBody):
    """批量导入代理：支持结构化 items 或逐行文本 text。"""
    if not body.items and not (body.text or "").strip():
        raise HTTPException(status_code=400, detail="items 与 text 不能同时为空")
    try:
        return proxy_pool.bulk(items=body.items, text=body.text or "")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/api/proxy/autobind")
def autobind_proxy(body: AutobindBody):
    """按订阅池为账号自动分配代理。"""
    realm = _check_realm(body.realm) if (body.realm or "").strip() else ""
    try:
        res = sub_pool.autobind(body.account_id, realm=realm)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    if isinstance(res, dict) and not res.get("ok", True):
        raise HTTPException(status_code=400, detail=res.get("error") or "自动绑定失败")
    return res


# ---- 统计 ----
@router.get("/api/proxy/stats")
def proxy_stats():
    """同 IP 账号数、状态分布、超限 IP。"""
    return proxy_pool.stats()


# ---- 订阅池 ----
@router.get("/api/proxy/pools")
def list_pools():
    """订阅池列表（realms: global / cn）。"""
    return {"realms": sub_pool.list()}


@router.post("/api/proxy/pools/save")
def save_pool(body: PoolSaveBody):
    """保存订阅池（mode: replace | append）。"""
    realm = _check_realm(body.realm)
    mode = (body.mode or "replace").strip().lower()
    if mode not in ("replace", "append"):
        raise HTTPException(status_code=400, detail=f"mode 只能是 replace / append，收到: {body.mode!r}")
    try:
        return sub_pool.save(realm, body.subs, mode=mode)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/api/proxy/pools/refresh")
def refresh_pool(body: RealmBody):
    """刷新订阅池（拉取最新订阅内容）。"""
    realm = _check_realm(body.realm)
    try:
        return sub_pool.refresh(realm)
    except Exception as exc:  # noqa: BLE001 —— 订阅拉取失败视为上游 502
        raise HTTPException(status_code=502, detail=f"订阅刷新失败: {exc}")


@router.post("/api/proxy/pools/clear")
def clear_pool(body: RealmBody):
    """清空订阅池。"""
    realm = _check_realm(body.realm)
    sub_pool.clear(realm)
    return {"ok": True, "realm": realm}
