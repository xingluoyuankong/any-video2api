# -*- coding: utf-8 -*-
"""海螺 AI 国内版渠道（hailuoai.com，MiniMax 海螺）。

逆向自 hailuoai.com 前端 JS（Next.js chunks，webpack 未混淆）：
    POST /v1/api/user/login/sms/send   {phone, region}          -> 下发短信
    POST /v1/api/user/login/phone      {phone, code, region}    -> 登录态
    POST /v1/api/user/login/email      {email, password}        -> 邮箱登录（端点确认，字段未实测）
    POST /v2/api/multimodal/generate/video                        -> 视频生成提交
    POST /api/feed/creation/my                                    -> 作品列表（轮询用）
    GET  /v1/api/user/info                                        -> 会话校验

实测结论（无账号盲测）：
  * 国内版与海外版 hailuoai.video **同一套 yy 签名**（盐 ooui）——已实测：
    携带签名打 /v1/api/user/login/sms/send 返回业务层错误 1200013「请输入正确的手机号」，
    证明签名通过、进入业务校验层。
  * 响应包裹 {statusInfo:{code,message}, data}，与海外版 {status,message} 不同。
全程纯 HTTP，不启动浏览器。

注：海外版登录见 channels/hailuo.py（hailuoai.video）。
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

from .base import (AccountSecret, Channel, ChannelSpec, CreditView,
                   GenerateRequest, PollResult, SubmitResult)

urllib3.disable_warnings()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36")
BASE = "https://hailuoai.com"


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _sign(full_path_with_query: str, body: str, ts_ms: int) -> str:
    """yy 签名（与海外版 hailuoai.video 完全一致，实测通过）。"""
    enc = urllib.parse.quote(full_path_with_query, safe="")
    return _md5(enc + "_" + (body or "") + _md5(str(ts_ms)) + "ooui")


def _unwrap(payload: Dict[str, Any]) -> Dict[str, Any]:
    """国内版响应包裹：{statusInfo:{code,message,...}, data:{...}}。"""
    si = payload.get("statusInfo") or {}
    code = si.get("code", payload.get("status", 0))
    # 成功码：statusInfo.code 常见为 1（0/None 兼容海外风格）；非成功即抛
    if code not in (0, 1, None) and si.get("message"):
        raise RuntimeError("hailuo_cn code=%s: %s" % (code, si.get("message")))
    return payload.get("data") or {}


class HailuoCnChannel(Channel):
    """海螺 AI 国内版（hailuoai.com）—— 手机号短信登录 + 视频生成。"""

    def __init__(self, spec: ChannelSpec, log=None):
        super().__init__(spec, log)
        self._device = {
            "device_platform": "web",
            "app_id": "3001",
            "version_code": "22203",
            "biz_id": "0",
            "lang": "zh",
            "os_name": "Windows",
            "browser_name": "chrome",
            "browser_language": "zh-CN",
            "browser_platform": "Win32",
            "screen_width": 1920,
            "screen_height": 1080,
            "cpu_core_num": 8,
            "device_memory": 8,
        }

    # ---- 底层请求 -------------------------------------------------------
    def _post(self, path: str, body: Dict[str, Any],
              token: str = "") -> tuple:
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

    def _get(self, path: str, token: str = "") -> Dict[str, Any]:
        ts = int(time.time() * 1000)
        params = dict(self._device)
        params["uuid"] = str(uuid.uuid4())
        params["unix"] = ts
        full = path + "?" + urllib.parse.urlencode(params)
        headers = {
            "User-Agent": UA,
            "Origin": BASE,
            "Referer": BASE + "/",
            "yy": _sign(full, "", ts),
        }
        if token:
            headers["token"] = token
        resp = requests.get(BASE + full, headers=headers, timeout=30, verify=False)
        try:
            return resp.json()
        except ValueError:
            return {"_raw": resp.text[:300]}

    # ---- 在线登录 -------------------------------------------------------
    def login_methods(self) -> List[str]:
        return ["sms", "pwd"]

    def login_send_sms(self, phone: str, area_code: str = "86") -> Dict[str, Any]:
        """发送登录短信验证码（会真实下发短信）。实测：签名通过，进入业务层校验。"""
        j, _ = self._post("/v1/api/user/login/sms/send",
                          {"phone": phone, "region": "CN"})
        return _unwrap(j)

    def login_by_sms(self, phone: str, code: str, area_code: str = "86") -> Optional[AccountSecret]:
        """手机号 + 验证码登录。国内版登录态同样走 token（响应 data / cookie）。"""
        j, resp = self._post("/v1/api/user/login/phone",
                             {"phone": phone, "code": str(code), "region": "CN"})
        data = _unwrap(j)
        token = ""
        for c in resp.cookies:
            if c.name in ("_token", "token"):
                token = c.value
        if not token:
            token = (data.get("token") or data.get("_token") or
                     data.get("access_token") or "") if isinstance(data, dict) else ""
        if not token:
            raise RuntimeError("登录成功但未提取到 token: %s" % (data or j))
        return AccountSecret(cookies={"_token": token}, oauth=None,
                             browser_state=None, extra={"token": token})

    def login_by_password(self, account: str, password: str) -> Optional[AccountSecret]:
        """邮箱账密登录（/v1/api/user/login/email，端点已逆向确认）。"""
        j, resp = self._post("/v1/api/user/login/email",
                             {"email": account, "password": password})
        data = _unwrap(j)
        token = ""
        for c in resp.cookies:
            if c.name in ("_token", "token"):
                token = c.value
        if not token and isinstance(data, dict):
            token = data.get("token") or data.get("_token") or ""
        if not token:
            raise RuntimeError("登录成功但未提取到 token: %s" % (data or j))
        return AccountSecret(cookies={"_token": token}, oauth=None,
                             browser_state=None, extra={"token": token})

    # ---- 会话 / 额度 ----------------------------------------------------
    def build_headers(self, secret: AccountSecret, path: str, body: Any = None,
                      extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h = {"User-Agent": UA, "Content-Type": "application/json"}
        if secret and secret.extra.get("token"):
            h["token"] = secret.extra["token"]
        h.update(extra or {})
        return h

    def credits(self, secret: AccountSecret) -> CreditView:
        try:
            j, _ = self._post("/v1/api/user/equity", {}, token=self._tok(secret))
            data = _unwrap(j)
        except Exception as exc:  # noqa: BLE001
            return CreditView(error=str(exc))
        total = 0.0
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, (int, float)) and "credit" in str(k).lower():
                    total = max(total, float(v))
        return CreditView(total=total, allocatable=total, raw=data)

    @staticmethod
    def _tok(secret: Optional[AccountSecret]) -> str:
        return (secret.extra.get("token") if secret and secret.extra else "") or ""

    def verify(self, secret: AccountSecret):
        from .base import VerifyResult
        j = self._get("/v1/api/user/info", token=self._tok(secret))
        si = j.get("statusInfo") or {}
        if si.get("code") in (0, 1, None) and j.get("data"):
            return VerifyResult(ok=True, status="active", raw=j)
        return VerifyResult(ok=False, status="invalid",
                            message=si.get("message") or "not login")

    # ---- 生成 -----------------------------------------------------------
    def submit(self, secret: AccountSecret, req: GenerateRequest) -> SubmitResult:
        """POST /v2/api/multimodal/generate/video（端点已逆向，payload 需登录态实测）。"""
        payload: Dict[str, Any] = {
            "prompt": req.prompt,
            "model": req.model or "",
            "duration": int(req.duration or 6),
        }
        if req.mode == "i2v" and req.images:
            payload["first_frame_image"] = req.images[0]
        payload.update(req.params or {})
        j, _ = self._post("/v2/api/multimodal/generate/video", payload,
                          token=self._tok(secret))
        data = _unwrap(j)
        task_id = ""
        if isinstance(data, dict):
            for key in ("video_id", "taskId", "task_id", "id", "sl_id"):
                if data.get(key):
                    task_id = str(data[key])
                    break
        if not task_id:
            return SubmitResult(accepted=False, error="未提取到任务 ID: %s" % data, raw=data)
        return SubmitResult(upstream_task_id=task_id, transport="requests", raw=data)

    def poll(self, secret: AccountSecret, upstream_task_id: str,
             req: Optional[GenerateRequest] = None) -> PollResult:
        """作品列表轮询：POST /api/feed/creation/my。"""
        j, _ = self._post("/api/feed/creation/my", {"limit": 20},
                          token=self._tok(secret))
        try:
            data = _unwrap(j)
        except Exception as exc:  # noqa: BLE001
            return PollResult(status="failed", error=str(exc), raw=j)
        items: List[Dict[str, Any]] = []
        if isinstance(data, dict):
            for key in ("creations", "list", "items", "videos"):
                if isinstance(data.get(key), list):
                    items = data[key]
                    break
        target = None
        for it in items:
            if str(it.get("id") or it.get("video_id") or "") == str(upstream_task_id):
                target = it
                break
        if target is None:
            return PollResult(status="processing", raw={"found": False})
        st = str(target.get("status") or "").lower()
        from .base import norm_status
        status = norm_status(st) if st else "processing"
        media = [u for u in (target.get("video_url"), target.get("url")) if u]
        covers = [u for u in (target.get("cover_url"), target.get("image_url")) if u]
        return PollResult(status=status, media=[str(u) for u in media],
                          covers=[str(u) for u in covers], raw=target)
