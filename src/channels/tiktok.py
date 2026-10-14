# -*- coding: utf-8 -*-
"""TikTok Symphony Creative Studio（ads.tiktok.com CUE）渠道适配器。

移植自 tiktok2api：Cookie + CSRF 透传（无请求签名），统一走 BFF
/creative_bff_i18n/api/cue/*；任务以 task_id 轮询 check_gen_task。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.channels.base import (AccountSecret, Channel, ChannelSpec, CreditView,
                               GenerateRequest, ModelSpec, PollResult, SubmitResult,
                               VerifyResult, fingerprint_headers, sniff_account_type)
from src.core.errors import UpstreamError, classify_error, find_values, redact
from src.platform.transport import HttpClient

BASE = "https://ads.tiktok.com"
BFF = f"{BASE}/creative_bff_i18n/api/cue"
AUTH_EXPIRED_CODES = {"10001106", "10001107"}

IMAGE_MODELS = {"gemini": "gemini", "nano": "gemini", "nano_banana": "gemini",
                "kontext": "kontext", "flux": "kontext", "flux_kontext": "kontext"}
I2V_MODELS = {"seedance": "4000003", "seedance2": "4000005", "goku": "4000001",
              "veo": "4000002", "runway": "4000004"}
T2V_MODELS = {"seedance": "5000003", "seedance2": "5000005", "goku": "5000001",
              "veo": "5000002", "sora": "5000004"}
R2V_MODELS = {"seedance": "2000003", "seedance2": "2000004", "sora": "2000001", "veo": "2000002"}


class TiktokChannel(Channel):
    name = "tiktok"

    def __init__(self, spec: ChannelSpec, log=None):
        super().__init__(spec, log)
        self._clients: Dict[str, HttpClient] = {}

    def _client(self, account_id: str = "") -> HttpClient:
        key = account_id or "default"
        if key not in self._clients:
            self._clients[key] = HttpClient(
                base_url=BASE, proxy=self.spec.proxy or None, timeout=self.spec.timeout,
                user_agent=self.spec.user_agent, channel=self.name, account_id=account_id)
        return self._clients[key]

    # ---- 认证 ----
    def build_headers(self, secret: AccountSecret, path: str, body: Any = None,
                      extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        cookies = secret.cookies or {}
        csrf = cookies.get("csrftoken") or cookies.get("passport_csrf_token") or ""
        h = {
            "User-Agent": self.spec.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": BASE,
            "Referer": f"{BASE}/creative/creativestudio/create",
            "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items() if v is not None),
            "X-csrftoken": csrf,
            "X-CSRFToken": csrf,
            "x-creative-source": "cue/p2v",
            "Agw-Js-Conv": "str",
        }
        creative_csrf = cookies.get("x-creative-csrf-token")
        if creative_csrf:
            h["x-creative-csrf-token"] = creative_csrf
        fpid = cookies.get("FPID")
        if fpid:
            h["x-fp-id"] = fpid
        h.update(fingerprint_headers(secret))  # 固定 UA / 语言头，防风控
        if extra:
            h.update(extra)
        return h

    def _call(self, secret: AccountSecret, name: str, body: Any, account_id: str = "") -> Any:
        ep = (self.spec.endpoints or {}).get(name, {})
        method = ep.get("method", "POST")
        path = ep.get("path", "")
        headers = self.build_headers(secret, path, body)
        r = self._client(account_id).request(method, path, body=body, headers=headers)
        code = str(r.get("code") or r.get("status_code") or "") if isinstance(r, dict) else ""
        if code in AUTH_EXPIRED_CODES:
            raise UpstreamError("tiktok session expired", "auth_or_risk", payload=redact(r))
        return r

    def verify(self, secret: AccountSecret) -> VerifyResult:
        try:
            r = self._call(secret, "verify", {"permission": ["CREATE"]})
            ok = str(r.get("code", "0")) in ("0", "") if isinstance(r, dict) else False
            return VerifyResult(ok=ok, status="active" if ok else "invalid",
                                account_type=sniff_account_type(r) if ok else "", raw=redact(r))
        except UpstreamError as e:
            s = str(e)
            # 只有明确鉴权失败才算凭证失效；网络/限流/风控算临时错误（risk，不降级）
            if "401" in s or "未登录" in s or "not logged" in s.lower():
                return VerifyResult(ok=False, status="invalid", message=s)
            return VerifyResult(ok=False, status="risk", message=s)

    def list_models(self, secret: AccountSecret) -> List[ModelSpec]:
        out = []
        for alias, mid in {**T2V_MODELS, **I2V_MODELS}.items():
            if alias == mid:
                continue
            out.append(ModelSpec(id=mid, alias=alias, display_name=f"tiktok:{alias}",
                                 kind="video", raw={}))
        for alias, mid in IMAGE_MODELS.items():
            out.append(ModelSpec(id=mid, alias=alias, display_name=f"tiktok:{alias}", kind="image", raw={}))
        return out

    def credits(self, secret: AccountSecret) -> CreditView:
        return CreditView(error="tiktok 无独立额度端点")

    # ---- 生成 ----
    def submit(self, secret: AccountSecret, req: GenerateRequest) -> SubmitResult:
        if req.kind == "image":
            mid = IMAGE_MODELS.get(req.model, req.model)
            body = {"model": mid, "prompt": req.prompt, "images": req.images,
                    "settings": {"aspectRatio": req.ratio} if req.ratio else {}}
            ep_name = "image_submit"
        else:
            table = {"t2v": T2V_MODELS, "i2v": I2V_MODELS, "r2v": R2V_MODELS}.get(req.mode, T2V_MODELS)
            mid = table.get(req.model, req.model)
            body = {"model": mid, "prompt": req.prompt, "duration": req.duration,
                    "images": req.images,
                    "settings": {"aspectRatio": req.ratio} if req.ratio else {}}
            ep_name = {"t2v": "t2v_submit", "i2v": "i2v_submit", "r2v": "i2v_submit"}.get(req.mode, "t2v_submit")
        body.update(req.params or {})
        r = self._call(secret, ep_name, body)
        tid = find_values(r, {"task_id", "taskid", "id"})
        return SubmitResult(upstream_task_id=str(tid[0]) if tid else "", transport="requests", raw=redact(r))

    def poll(self, secret: AccountSecret, upstream_task_id: str,
             req: Optional[GenerateRequest] = None) -> PollResult:
        r = self._call(secret, "poll", {"task_id": str(upstream_task_id)})
        data = r.get("data") if isinstance(r, dict) else {}
        data = data if isinstance(data, dict) else {}
        status_code = data.get("status", r.get("status"))
        urls = []
        for k in ("video_url", "videoUrl", "play_url", "playUrl", "url"):
            if isinstance(data.get(k), str) and data[k].startswith("http"):
                urls.append(data[k])
        urls += [u for u in find_values(r, {"download_url", "downloadUrl"}) if isinstance(u, str) and u.startswith("http")]
        urls = list(dict.fromkeys(urls))
        if urls:
            status = "success"
        elif str(status_code) in ("3", "4", "failed", "error"):
            status = "failed"
        elif str(status_code) in ("0", "1", "2", "processing", "running", ""):
            status = "processing"
        else:
            status = "processing"
        err = str(data.get("message") or r.get("message") or "") if status == "failed" else ""
        return PollResult(status=status, media=urls, error=err,
                          error_class=classify_error(status_code, err) if status == "failed" else "",
                          progress=float(data.get("progress") or 0), raw=redact(r))

    def classify_error(self, payload: Any) -> str:
        if isinstance(payload, dict):
            code = str(payload.get("code") or payload.get("status_code") or "")
            if code in AUTH_EXPIRED_CODES:
                return "auth_or_risk"
            return classify_error(code, str(payload.get("message") or ""))
        return "upstream"
