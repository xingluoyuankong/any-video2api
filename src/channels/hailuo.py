# -*- coding: utf-8 -*-
"""海螺 AI 渠道（hailuoai.video）。

逆向自 hailuoai.video 前端 JS，实现**手机号 + 短信验证码**原生登录：
    POST /v1/api/user/login/sms/send   {phone, region}
    POST /v1/api/user/login/phone      {phone, code, region}  -> _token
签名头 yy = md5(encodeURIComponent(完整URL含query) + "_" + body + md5(str(unix_ms)) + "ooui")
全程纯 HTTP，不启动浏览器、不读写本地浏览器 cookie。

注：本渠道目前只暴露「登录」能力；生成能力见独立项目 E:/API管理工具/hailuoai。
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import uuid
from typing import Any, Dict, List, Optional

import requests
import urllib3

from .base import AccountSecret, Channel, ChannelSpec

urllib3.disable_warnings()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36")
BASE = "https://hailuoai.video"


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _sign(full_path_with_query: str, body: str, ts_ms: int) -> str:
    """yy 签名（复刻前端拦截器）。"""
    enc = urllib.parse.quote(full_path_with_query, safe="")
    return _md5(enc + "_" + (body or "") + _md5(str(ts_ms)) + "ooui")


class HailuoChannel(Channel):
    """海螺 AI —— 手机号 + 短信登录。"""

    def __init__(self, spec: ChannelSpec, log=None):
        super().__init__(spec, log)
        self._device = {
            "device_platform": "web",
            "app_id": "3001",
            "version_code": "22203",
            "biz_id": "0",
            "lang": "en",
            "os_name": "Windows",
            "browser_name": "chrome",
            "browser_language": "en-US",
            "browser_platform": "Win32",
            "screen_width": 1920,
            "screen_height": 1080,
            "cpu_core_num": 8,
            "device_memory": 8,
        }

    # ---- 底层请求 -------------------------------------------------------
    def _post(self, path: str, body: Dict[str, Any], token: str = "") -> tuple:
        ts = int(time.time() * 1000)
        params = dict(self._device)
        params["uuid"] = str(uuid.uuid4())
        params["unix"] = ts
        full = path + "?" + urllib.parse.urlencode(params)
        body_str = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        headers = {
            "User-Agent": UA,
            "Content-Type": "application/json",
            "Origin": BASE,
            "Referer": BASE + "/",
            "yy": _sign(full, body_str, ts),
        }
        if token:
            headers["token"] = token
        resp = requests.post(BASE + full, data=body_str.encode("utf-8"),
                             headers=headers, timeout=30, verify=False)
        try:
            payload = resp.json() if resp.content else {}
        except ValueError:
            payload = {"_raw": resp.text[:300]}
        return payload, resp

    # ---- 在线登录 -------------------------------------------------------
    def login_methods(self) -> List[str]:
        return ["sms"]

    def login_send_sms(self, phone: str, area_code: str = "86") -> Dict[str, Any]:
        """发送登录短信验证码（会真实下发短信）。"""
        j, _ = self._post("/v1/api/user/login/sms/send",
                          {"phone": phone, "region": "CN"})
        if j.get("status") not in (0, None) and j.get("message"):
            # 上游业务错误
            raise RuntimeError(j.get("message") or j)
        return j.get("data") or {}

    def login_by_sms(self, phone: str, code: str, area_code: str = "86") -> Optional[AccountSecret]:
        """手机号 + 验证码登录，返回 _token 凭据。"""
        j, resp = self._post("/v1/api/user/login/phone",
                             {"phone": phone, "code": str(code), "region": "CN"})
        token = ""
        for c in resp.cookies:
            if c.name == "_token":
                token = c.value
        if not token:
            data = j.get("data") or {}
            token = data.get("token") or data.get("_token") or ""
        if not token:
            msg = j.get("message") or j.get("msg") or j
            raise RuntimeError("登录失败：%s" % msg)
        return AccountSecret(cookies={"_token": token}, oauth=None,
                             browser_state=None, extra={"token": token})

    # ---- 必需 abstract（本渠道只做登录，生成请用独立项目）----------------
    def build_headers(self, secret: AccountSecret, path: str, body: Any = None,
                      extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h = {"User-Agent": UA, "Content-Type": "application/json"}
        if secret and secret.extra.get("token"):
            h["token"] = secret.extra["token"]
        return h

    def submit(self, secret: AccountSecret, req: Any) -> Any:
        raise NotImplementedError("海螺渠道在本平台只暴露登录能力；生成请用 E:/API管理工具/hailuoai")

    def poll(self, secret: AccountSecret, upstream_task_id: str, **kw: Any) -> Any:
        raise NotImplementedError("海螺渠道在本平台只暴露登录能力；生成请用 E:/API管理工具/hailuoai")
