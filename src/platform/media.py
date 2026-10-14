# -*- coding: utf-8 -*-
"""统一媒体存储（合并 jimeng2api.media_store + tiktok2api.media_store）。

分桶：videos / images / covers / assets / tmp。按"去掉短时签名参数的稳定 URL"去重，
流式写入临时文件后原子替换，受 max_media_bytes 限制。
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import requests

from src.core.config import config

MEDIA_DIR = Path(config.media_dir)
BUCKETS = ("videos", "images", "covers", "assets", "tmp")
for _b in BUCKETS:
    (MEDIA_DIR / _b).mkdir(parents=True, exist_ok=True)

_SIGN_KEYS = {"x-expires", "x-signature", "sign", "token", "expires", "expire", "auth_key",
              "x-signature-expires", "x-signature-nonce", "x-signature-version", "policy"}


def stable_key(url: str) -> str:
    """去掉短时签名参数，得到稳定 URL 键，用于去重。"""
    try:
        p = urlparse(str(url))
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if k.lower() not in _SIGN_KEYS]
        return urlunparse((p.scheme, p.netloc, p.path, "", urlencode(q), ""))
    except Exception:
        return str(url)


def guess_ext(url: str, content_type: str = "") -> str:
    ct_map = {"video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
              "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
    for k, v in ct_map.items():
        if k in (content_type or ""):
            return v
    path = urlparse(str(url)).path
    m = re.search(r"\.(mp4|webm|mov|m4v|jpg|jpeg|png|webp|gif)$", path, re.I)
    return ("." + m.group(1).lower()) if m else ".bin"


def local_path(bucket: str, name: str) -> Path:
    return MEDIA_DIR / bucket / name


def public_url(bucket: str, name: str) -> str:
    return f"{config.public_base}/media/{bucket}/{name}"


def fetch_to_bucket(url: str, bucket: str = "videos", prefix: str = "",
                    proxy: Optional[str] = None, timeout: int = 120) -> Optional[dict]:
    """流式下载到本地，返回 {local_path, url, bytes, sha1}；失败返回 None。"""
    if bucket not in BUCKETS:
        bucket = "videos"
    key = stable_key(url)
    sha1 = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        with requests.get(url, stream=True, timeout=timeout, proxies=proxies) as r:
            r.raise_for_status()
            ext = guess_ext(url, r.headers.get("content-type", ""))
            name = f"{prefix}{sha1}{ext}" if prefix else f"{sha1}{ext}"
            target = local_path(bucket, name)
            if target.exists() and target.stat().st_size > 0:
                return {"local_path": str(target), "url": public_url(bucket, name),
                        "bytes": target.stat().st_size, "sha1": sha1, "cached": True}
            tmp_fd, tmp_name = tempfile.mkstemp(dir=str(MEDIA_DIR / "tmp"))
            size = 0
            with os.fdopen(tmp_fd, "wb") as fh:
                for chunk in r.iter_content(chunk_size=config.media_chunk_bytes):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > config.max_media_bytes:
                        raise ValueError("media exceeds max_media_bytes")
                    fh.write(chunk)
            os.replace(tmp_name, target)
            return {"local_path": str(target), "url": public_url(bucket, name),
                    "bytes": size, "sha1": sha1, "cached": False}
    except Exception:
        return None


def save_bytes(data: bytes, bucket: str = "assets", name: str = "") -> dict:
    if bucket not in BUCKETS:
        bucket = "assets"
    if not name:
        name = hashlib.sha1(data).hexdigest()[:16] + ".bin"
    target = local_path(bucket, name)
    target.write_bytes(data)
    return {"local_path": str(target), "url": public_url(bucket, name), "bytes": len(data)}


def resolve_local(bucket: str, name: str) -> Optional[Path]:
    """带路径穿越防护的解析。"""
    try:
        target = (MEDIA_DIR / bucket / name).resolve()
        target.relative_to((MEDIA_DIR / bucket).resolve())
        return target if target.is_file() else None
    except Exception:
        return None
