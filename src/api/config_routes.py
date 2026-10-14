# -*- coding: utf-8 -*-
"""配置管理 API —— setting.toml 与渠道描述符的读取 / 白名单更新 / 热重载。

    GET  /api/config                    当前配置（api_key 等敏感项脱敏）
    POST /api/config                    白名单节键更新并写回 setting.toml（保留注释）
    GET  /api/config/channels           渠道描述符列表
    GET  /api/config/channels/{name}    单个渠道描述符原文
    POST /api/config/channels/{name}    写入/更新渠道描述符并热重载注册表
    POST /api/config/reload             重载配置 + 渠道注册表

写回 setting.toml 采用逐行替换：只改目标键所在行的值，原文件注释、空行、顺序全部保留；
节内找不到的键追加到该节末尾。文件写入为原子操作（临时文件 + os.replace）。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from src.channels.registry import registry
from src.core.config import config

router = APIRouter(prefix="/api/config")

#: 可写白名单：节 -> 允许更新的键
WRITABLE: Dict[str, set] = {
    "antiban": {"rpm_limit", "min_interval", "jitter", "failure_threshold",
                "cooldown", "fingerprint", "enabled"},
    "session": {"enabled", "keepalive_interval", "lead_time", "backup_keep", "relogin_threshold"},
    "proxy": {"enabled", "probe_timeout", "max_per_ip"},
    "job": {"poll_interval", "max_poll_interval", "video_timeout", "max_network_retries"},
    "checkin": {"enabled", "timezone", "primary_time", "retry_time"},
    "api": {"auth_disabled"},
}

#: 敏感键识别（命中即脱敏为 ***）
_SENSITIVE_HINTS = ("api_key", "apikey", "secret", "token", "cookie",
                    "password", "passwd", "authorization", "credential")

_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_KV_RE = re.compile(r"^(\s*)([A-Za-z0-9_.\-]+)\s*=")


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------
def _is_sensitive(key: Any) -> bool:
    k = str(key).lower()
    return any(h in k for h in _SENSITIVE_HINTS)


def _mask(value: Any) -> Any:
    """递归脱敏：敏感键的取值统一替换为 ***（空值保持空）。"""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if _is_sensitive(k):
                out[k] = "***" if v not in (None, "", {}, []) else v
            else:
                out[k] = _mask(v)
        return out
    if isinstance(value, list):
        return [_mask(v) for v in value]
    return value


def _effective() -> dict:
    """当前生效值（含默认值回填，与 config 属性一致）。"""
    return {
        "server": {"host": config.host, "port": config.port},
        "antiban": {
            "enabled": config.antiban_enabled,
            "rpm_limit": config.antiban_rpm_limit,
            "min_interval": config.antiban_min_interval,
            "jitter": config.antiban_jitter,
            "failure_threshold": config.antiban_failure_threshold,
            "cooldown": config.antiban_cooldown,
            "fingerprint": config.get("antiban", "fingerprint", True),
        },
        "session": {
            "enabled": config.session_enabled,
            "keepalive_interval": config.session_keepalive_interval,
            "lead_time": config.session_lead_time,
            "backup_keep": config.session_backup_keep,
            "relogin_threshold": config.session_relogin_threshold,
        },
        "proxy": {
            "enabled": config.proxy_enabled,
            "probe_timeout": config.proxy_probe_timeout,
            "max_per_ip": config.proxy_max_per_ip,
        },
    }


# ---------------------------------------------------------------------------
# TOML 逐行写回
# ---------------------------------------------------------------------------
def _fmt_toml_value(value: Any) -> str:
    """按 TOML 语法格式化：bool -> true/false，str 加双引号，int/float 原样。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_fmt_toml_value(v) for v in value) + "]"
    raise HTTPException(status_code=400, detail=f"不支持的配置值类型: {type(value).__name__}")


def _extract_comment(rest: str) -> str:
    """从 `值` 之后的文本中提取行内注释（跳过字符串内部的 #）。"""
    in_str = False
    esc = False
    for i, ch in enumerate(rest):
        if esc:
            esc = False
            continue
        if in_str and ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if ch == "#" and not in_str:
            return " " + rest[i:].rstrip()
    return ""


def _replace_line_value(line: str, key: str, value_str: str) -> Optional[str]:
    """把一行 `key = old` 的值替换为 value_str，保留行内注释。"""
    m = re.match(r"^(\s*" + re.escape(key) + r"\s*=\s*)(.*)$", line)
    if not m:
        return None
    prefix, rest = m.group(1), m.group(2)
    return prefix + value_str + _extract_comment(rest)


def _atomic_write(path, text: str) -> None:
    """原子写：先写同目录临时文件，再 os.replace 覆盖（newline="" 不做换行翻译）。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(str(tmp), str(path))


def _write_toml_updates(section: str, values: Dict[str, Any]) -> None:
    """逐行替换写回 setting.toml，保留原注释与格式；缺失键追加到节末。"""
    path = config.path
    text = path.read_text(encoding="utf-8", newline="") if path.exists() else ""
    nl = "\r\n" if "\r\n" in text else "\n"   # 保留原文件换行风格（CRLF/LF）
    lines = text.splitlines()

    # 定位目标节的范围 [start, end)
    start: Optional[int] = None
    end = len(lines)
    for i, ln in enumerate(lines):
        m = _SECTION_RE.match(ln)
        if not m:
            continue
        if start is None:
            if m.group(1).strip() == section:
                start = i
        else:
            end = i
            break

    if start is None:
        # 节不存在：在文件末尾新建
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"[{section}]")
        for k, v in values.items():
            lines.append(f"{k} = {_fmt_toml_value(v)}")
    else:
        remaining = dict(values)
        for i in range(start + 1, end):
            m = _KV_RE.match(lines[i])
            if not m:
                continue
            key = m.group(2)
            if key in remaining:
                new_line = _replace_line_value(lines[i], key, _fmt_toml_value(remaining[key]))
                if new_line is not None:
                    lines[i] = new_line
                    remaining.pop(key)
        if remaining:
            insert_at = end
            while insert_at > start + 1 and not lines[insert_at - 1].strip():
                insert_at -= 1
            lines[insert_at:insert_at] = [f"{k} = {_fmt_toml_value(v)}"
                                          for k, v in remaining.items()]

    out = nl.join(lines)
    if text.endswith(("\n", "\r")) or not text:
        out += nl
    _atomic_write(path, out)


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------
class ConfigUpdate(BaseModel):
    """配置更新请求：section + 待写入的键值。"""

    section: str
    values: Dict[str, Any]


class ChannelDescriptor(BaseModel):
    """渠道描述符（任意字段透传，adapter 可选，缺省按约定推导）。"""

    model_config = ConfigDict(extra="allow")

    adapter: Optional[str] = None


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
@router.get("")
def get_config():
    """当前配置：原始节（脱敏）+ 生效值。"""
    return {"path": str(config.path), "sections": _mask(config.raw), "effective": _effective()}


@router.post("")
def update_config(body: ConfigUpdate):
    """按白名单更新 setting.toml 并热生效。"""
    allowed = WRITABLE.get(body.section)
    if allowed is None:
        raise HTTPException(status_code=400, detail={
            "error": f"不允许修改的配置节: {body.section}",
            "allowed_sections": sorted(WRITABLE),
        })
    if not body.values:
        raise HTTPException(status_code=400, detail="values 不能为空")
    invalid = sorted(k for k in body.values if k not in allowed)
    if invalid:
        raise HTTPException(status_code=400, detail={
            "error": f"不允许的配置键: {invalid}",
            "allowed_keys": sorted(allowed),
        })
    # 先校验值类型可格式化，避免写坏文件
    for k, v in body.values.items():
        _fmt_toml_value(v)
    _write_toml_updates(body.section, body.values)
    config.reload()
    return {"ok": True, "path": str(config.path), "section": body.section,
            "updated": body.values, "effective": _effective()}


@router.post("/reload")
def reload_config():
    """重载 setting.toml 与渠道注册表。"""
    config.reload()
    registry.load()
    return {"ok": True, "channels": registry.names(), "errors": registry.errors()}


# ---------------------------------------------------------------------------
# 渠道描述符
# ---------------------------------------------------------------------------
def _descriptor_path(name: str):
    if not _NAME_RE.match(name or ""):
        raise HTTPException(status_code=400, detail="非法渠道名")
    return config.channels_dir / f"{name}.json"


@router.get("/channels")
def list_channel_descriptors():
    """渠道描述符列表（含加载失败的渠道）。"""
    out: List[dict] = []
    for d in registry.describe():
        out.append({
            "name": d.get("name"),
            "display_name": d.get("display_name") or d.get("name"),
            "kind": d.get("kind", ""),
            "base_url": d.get("base_url", ""),
            "capabilities": d.get("capabilities", {}),
            "model_count": d.get("model_count", 0),
            "load_error": d.get("error"),
        })
    return {"channels": out}


@router.get("/channels/{name}")
def get_channel_descriptor(name: str):
    """单个渠道描述符原文 JSON。"""
    path = _descriptor_path(name)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"渠道描述符不存在: {name}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"描述符解析失败: {exc}")


@router.post("/channels/{name}")
def save_channel_descriptor(name: str, body: ChannelDescriptor):
    """写入渠道描述符并热重载注册表。"""
    path = _descriptor_path(name)
    data = body.model_dump(exclude_none=True)
    if not isinstance(data, dict) or not data:
        raise HTTPException(status_code=400, detail="描述符不能为空")
    adapter = data.get("adapter")
    if adapter is not None:
        if not isinstance(adapter, str) or ":" not in adapter:
            raise HTTPException(status_code=400,
                                detail="adapter 必须为 'module.path:ClassName' 形式")
    # 未写 adapter 时按约定推导：src.channels.<name>:<Name>Channel
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    registry.load()
    return {"ok": True, "channels": registry.names(), "errors": registry.errors()}
