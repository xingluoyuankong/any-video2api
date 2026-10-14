# -*- coding: utf-8 -*-
"""平台配置：单份 setting.toml + 每渠道一份 config/channels/<name>.json 描述符。"""
from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class Config:
    def __init__(self):
        self.path = Path(os.getenv("AV2A_CONFIG") or os.getenv("UNIFIED_CONFIG")
                         or (ROOT / "config" / "setting.toml"))
        self.channels_dir = Path(os.getenv("AV2A_CHANNELS_DIR") or os.getenv("UNIFIED_CHANNELS_DIR")
                                 or (ROOT / "config" / "channels"))
        self.reload()

    def reload(self) -> None:
        try:
            self.raw = tomllib.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.raw = {}

    def get(self, section: str, key: str, default=None):
        return self.raw.get(section, {}).get(key, default)

    # ---- server ----
    @property
    def host(self): return str(self.get("server", "host", "127.0.0.1"))
    @property
    def port(self): return int(self.get("server", "port", 8800))
    @property
    def public_base(self):
        return str(self.get("server", "public_base", f"http://127.0.0.1:{self.port}")).rstrip("/")

    # ---- 对外鉴权 ----
    @property
    def api_key(self):
        return os.getenv("AV2A_API_KEY") or os.getenv("UNIFIED_API_KEY") or str(self.get("api", "api_key", "")).strip()
    @property
    def auth_disabled(self):
        return bool(self.get("api", "auth_disabled", False))

    # ---- 存储 ----
    @property
    def database(self): return ROOT / str(self.get("storage", "database", "data/unified.db"))
    @property
    def media_dir(self): return ROOT / str(self.get("storage", "media_dir", "data/media"))
    @property
    def max_media_bytes(self): return int(self.get("storage", "max_media_bytes", 524288000))
    @property
    def media_chunk_bytes(self): return int(self.get("storage", "media_chunk_bytes", 1048576))
    @property
    def max_upload_bytes(self): return int(self.get("storage", "max_upload_bytes", 52428800))
    @property
    def profile_dir(self): return ROOT / str(self.get("storage", "profile_dir", "data/profiles"))

    # ---- 任务 ----
    @property
    def poll_interval(self): return float(self.get("job", "poll_interval", 6))
    @property
    def max_poll_interval(self): return float(self.get("job", "max_poll_interval", 15))
    @property
    def video_timeout(self): return int(self.get("job", "video_timeout", 1800))
    @property
    def max_network_retries(self): return int(self.get("job", "max_network_retries", 3))

    # ---- 防风控 ----
    @property
    def antiban_enabled(self): return bool(self.get("antiban", "enabled", True))
    @property
    def antiban_rpm_limit(self): return int(self.get("antiban", "rpm_limit", 60))
    @property
    def antiban_min_interval(self): return float(self.get("antiban", "min_interval", 1.0))
    @property
    def antiban_jitter(self): return float(self.get("antiban", "jitter", 0.5))
    @property
    def antiban_failure_threshold(self): return int(self.get("antiban", "failure_threshold", 3))
    @property
    def antiban_cooldown(self): return float(self.get("antiban", "cooldown", 120))
    @property
    def antiban_proxy_pool(self):
        v = self.get("antiban", "proxy_pool", []) or []
        return [str(x) for x in v if x]

    # ---- 代理（账号级固定出口）----
    @property
    def proxy_enabled(self): return bool(self.get("proxy", "enabled", True))
    @property
    def proxy_probe_timeout(self): return float(self.get("proxy", "probe_timeout", 15))
    @property
    def proxy_max_per_ip(self): return int(self.get("proxy", "max_per_ip", 3))

    # ---- 会话保活 ----
    @property
    def session_enabled(self): return bool(self.get("session", "enabled", True))
    @property
    def session_keepalive_interval(self): return float(self.get("session", "keepalive_interval", 1800))
    @property
    def session_lead_time(self): return float(self.get("session", "lead_time", 3600))
    @property
    def session_backup_keep(self): return int(self.get("session", "backup_keep", 5))
    @property
    def session_relogin_threshold(self): return int(self.get("session", "relogin_threshold", 3))

    # ---- 签到 ----
    @property
    def checkin_enabled(self): return bool(self.get("checkin", "enabled", True))
    @property
    def checkin_timezone(self): return str(self.get("checkin", "timezone", "Asia/Shanghai"))
    @property
    def checkin_primary(self): return str(self.get("checkin", "primary_time", "08:10"))
    @property
    def checkin_retry(self): return str(self.get("checkin", "retry_time", "20:10"))

    # ---- 渠道 ----
    def channel_specs(self) -> dict:
        """加载 config/channels/*.json，返回 {name: raw_dict}。"""
        out = {}
        if not self.channels_dir.exists():
            return out
        for f in sorted(self.channels_dir.glob("*.json")):
            try:
                out[f.stem] = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
        return out

    @property
    def enabled_channels(self):
        v = self.get("channels", "enabled", None)
        if isinstance(v, list):
            return [str(x) for x in v]
        return None  # None = 全部启用


config = Config()
