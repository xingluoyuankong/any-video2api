# -*- coding: utf-8 -*-
"""OpenAI 兼容层：/v1/models, /v1/videos/generations, /v1/images/generations, /v1/files。

三项目对外契约在此收口为同一套；`channel` 字段选择渠道，缺省用默认渠道。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request

from src.channels.base import GenerateRequest
from src.channels.registry import registry
from src.core.models import ImageRequest, VideoRequest
from src.platform.jobs import store

router = APIRouter()


def _default_channel() -> str:
    names = registry.names()
    if not names:
        raise HTTPException(status_code=503, detail="没有可用渠道")
    return names[0]


def _channel_from_model(model: str) -> str:
    """从模型名解析渠道。约定格式 `channel:model_id`（如 pixverse:v5.5）。

    这是对外 API 的路由核心：/v1/models 里所有 id 都带渠道前缀，
    调用方传什么前缀就该路由到哪个渠道。忽略前缀会导致请求全部落到
    registry.names()[0]（实测是 jimeng），返回 "no eligible account"。
    """
    m = (model or "").strip()
    if ":" in m:
        head = m.split(":", 1)[0].strip()
        if head and registry.get(head) is not None:
            return head
    return ""


def _to_generate(req: VideoRequest | ImageRequest, kind: str) -> GenerateRequest:
    # 渠道优先级：显式 req.channel > 模型名前缀 > 兜底第一个渠道
    channel = (req.channel or "").strip() or _channel_from_model(req.model) or _default_channel()
    return GenerateRequest(
        channel=channel, kind=kind, model=req.model, prompt=req.prompt,
        mode=getattr(req, "mode", "t2v"), duration=getattr(req, "duration", 5),
        ratio=getattr(req, "ratio", "16:9"), resolution=getattr(req, "resolution", "720p"),
        size=getattr(req, "size", "") or "", images=list(req.images or []),
        params=dict(req.extra or {}), account_mode=req.account_mode,
        account_id=req.account_id, timeout=req.timeout,
        idempotency_key=getattr(req, "idempotency_key", "") or "")


def _job_view(job: dict) -> dict:
    """OpenAI 风格任务视图。"""
    status = job.get("status")
    oai_status = {"queued": "queued", "submitted": "queued", "processing": "in_progress",
                  "success": "succeeded", "failed": "failed", "cancelled": "cancelled"}.get(status, status)
    data = [{"url": m.get("url"), "local_path": m.get("local_path"), "cached": m.get("cached", False)}
            for m in (job.get("media") or [])]
    return {
        "id": job["id"], "object": "generation.task", "channel": job["channel"],
        "model": job.get("model"), "status": oai_status, "raw_status": status,
        "created": int(job.get("created_at") or 0), "data": data,
        "error": job.get("error") or None, "error_class": job.get("error_class") or None,
    }


# ---- 模型目录 ----
@router.get("/v1/models")
def list_models(channel: Optional[str] = Query(None)):
    out = []
    names = [channel] if channel else registry.names()
    for name in names:
        ch = registry.get(name)
        if not ch:
            continue
        for m in (ch.spec.models or {}).items():
            alias, mid = m
            out.append({"id": f"{name}:{alias}", "object": "model", "channel": name,
                        "owned_by": name, "upstream_id": mid})
    return {"object": "list", "data": out}


@router.get("/v1/models/{model_id}")
def get_model(model_id: str):
    for item in list_models()["data"]:
        if item["id"] == model_id or item["id"].split(":")[-1] == model_id:
            return item
    raise HTTPException(status_code=404, detail="model not found")


# ---- 视频 ----
@router.post("/v1/videos/generations")
def create_video(req: VideoRequest):
    gen = _to_generate(req, "video")
    if registry.get(gen.channel) is None:
        raise HTTPException(status_code=400, detail=f"未知渠道: {gen.channel}")
    job = store.create(gen.channel, gen, account_id=req.account_id)
    return _job_view(job)


@router.get("/v1/videos/generations/{task_id}")
def get_video(task_id: str):
    job = store.get(task_id)
    if not job:
        raise HTTPException(status_code=404, detail="task not found")
    return _job_view(job)


# ---- 图片 ----
@router.post("/v1/images/generations")
def create_image(req: ImageRequest):
    gen = _to_generate(req, "image")
    if registry.get(gen.channel) is None:
        raise HTTPException(status_code=400, detail=f"未知渠道: {gen.channel}")
    job = store.create(gen.channel, gen, account_id=req.account_id)
    return _job_view(job)


@router.get("/v1/images/generations/{task_id}")
def get_image(task_id: str):
    job = store.get(task_id)
    if not job:
        raise HTTPException(status_code=404, detail="task not found")
    return _job_view(job)


# ---- 素材上传（转发给渠道实现）----
@router.post("/v1/files")
async def upload_file(request: Request, channel: str = Query("jimeng")):
    ch = registry.get(channel)
    if ch is None:
        raise HTTPException(status_code=400, detail=f"未知渠道: {channel}")
    form = await request.form()
    upload = form.get("file")
    if upload is None:
        raise HTTPException(status_code=400, detail="缺少 file 字段")
    data = await upload.read()
    # 需要一个账号凭据
    from src.platform.accounts import manager
    row = manager.pick(channel, mode="auto")
    if not row:
        raise HTTPException(status_code=409, detail="没有可用账号")
    secret = manager.secret(row["id"])
    try:
        # 渠道 upload 是同步 requests 实现（可能耗时 120s+），
        # 直接在这里调用会阻塞事件循环、拖死整个服务 —— 必须丢线程池。
        from starlette.concurrency import run_in_threadpool
        ref = await run_in_threadpool(
            ch.upload, secret, data,
            getattr(upload, "filename", "asset.bin"),
            getattr(upload, "content_type", "application/octet-stream"))
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e))
    return {"object": "file", "channel": channel, "uri": ref.uri, "url": ref.url}
