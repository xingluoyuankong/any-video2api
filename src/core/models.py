# -*- coding: utf-8 -*-
"""统一请求/响应模型（合并三项目的 pydantic schema）。

设计原则：平台层用一套字段收口，渠道私有参数通过 `extra` / `params` 透传，
避免每加一个渠道就要改一次对外 API 契约。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class VideoRequest(BaseModel):
    channel: str = "jimeng"
    model: str = ""
    prompt: str = ""
    mode: str = "t2v"                     # t2v|i2v|r2v|...
    duration: int = 5
    ratio: str = "16:9"
    resolution: str = "720p"
    size: Optional[str] = None
    image: Optional[str] = None
    images: List[str] = Field(default_factory=list)
    first_frame: Optional[str] = None
    end_frame: Optional[str] = None
    n: int = 1
    wait: bool = False
    timeout: int = 600
    response_format: str = "url"          # url|b64_json|both
    delivery: str = "cache"               # cache|remote
    account_mode: str = "auto"
    account_id: Optional[str] = None
    account_ids: List[str] = Field(default_factory=list)
    concurrency: int = 1
    idempotency_key: str = ""
    extra: Dict[str, Any] = Field(default_factory=dict)


class ImageRequest(BaseModel):
    channel: str = "jimeng"
    model: str = ""
    prompt: str = ""
    n: int = 1
    size: str = "1024x1024"
    ratio: str = "1:1"
    resolution: str = "2k"
    negative_prompt: str = ""
    images: List[str] = Field(default_factory=list)
    wait: bool = False
    timeout: int = 300
    response_format: str = "url"
    delivery: str = "cache"
    account_mode: str = "auto"
    account_id: Optional[str] = None
    account_ids: List[str] = Field(default_factory=list)
    concurrency: int = 1
    idempotency_key: str = ""
    extra: Dict[str, Any] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    channel: str = "coze"
    model: str = ""
    messages: List[Dict[str, Any]] = Field(default_factory=list)
    prompt: str = ""
    account_mode: str = "auto"
    account_id: Optional[str] = None
    extra: Dict[str, Any] = Field(default_factory=dict)


class AccountPatch(BaseModel):
    name: Optional[str] = None
    status: Optional[str] = None
    site_region: Optional[str] = None
    proxy: Optional[str] = None
    profile_path: Optional[str] = None
    note: Optional[str] = None


class AccountImport(BaseModel):
    channel: str = "jimeng"
    name: str = ""
    cookies: Optional[Any] = None
    browser_state: Optional[Any] = None
    oauth: Optional[Dict[str, Any]] = None
    profile_path: Optional[str] = None
    proxy: str = ""
    site_region: str = "cn"
    note: str = ""


class KeyCreate(BaseModel):
    name: str = "client"
    note: str = ""
    rpm_limit: int = 120
    max_concurrent: int = 8
    allowed_channels: List[str] = Field(default_factory=list)
