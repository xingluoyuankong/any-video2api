# -*- coding: utf-8 -*-
"""扣子（coze.cn）渠道适配器。

移植自 coze2api：OAuth(PKCE) 或 Cookie 双模认证；工作流提交/轮询走协议接口
（/api/workflow/run、/api/workflow_api/test_run、.../get_process），零浏览器依赖。
"""
from __future__ import annotations

import hashlib
import base64
import os
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from src.channels.base import (AccountSecret, Channel, ChannelSpec, CreditView,
                               GenerateRequest, ModelSpec, PollResult, SubmitResult,
                               VerifyResult, fingerprint_headers)
from src.core.errors import UpstreamError, classify_error, redact
from src.platform.transport import HttpClient

WEB_BASE = "https://code.coze.cn"
WORKFLOW_BASE = "https://www.coze.cn"
OAUTH_AUTHORIZE = "https://www.coze.cn/api/permission/oauth2/authorize"
OAUTH_TOKEN = "https://api.coze.cn/api/permission/oauth2/token"


class CozeChannel(Channel):
    name = "coze"

    def __init__(self, spec: ChannelSpec, log=None):
        super().__init__(spec, log)
        self._clients: Dict[str, HttpClient] = {}

    def _client(self, account_id: str = "") -> HttpClient:
        key = account_id or "default"
        if key not in self._clients:
            self._clients[key] = HttpClient(
                base_url=WORKFLOW_BASE, proxy=self.spec.proxy or None, timeout=self.spec.timeout,
                user_agent=self.spec.user_agent, channel=self.name, account_id=account_id)
        return self._clients[key]

    # ---- 认证 ----
    def build_headers(self, secret: AccountSecret, path: str, body: Any = None,
                      extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h = {"Accept": "application/json", "Referer": "https://code.coze.cn/"}
        oauth = secret.oauth or {}
        token = oauth.get("access_token")
        if token:
            h["Authorization"] = f"Bearer {token}"
        cookies = secret.cookies or {}
        if cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items() if v is not None)
        h.update(fingerprint_headers(secret))  # 固定 UA / 语言头，防风控
        if extra:
            h.update(extra)
        return h

    # ---- OAuth 辅助 ----
    @staticmethod
    def pkce_pair() -> tuple:
        verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        return verifier, challenge

    def oauth_start(self, client_id: str, redirect_uri: str, state: str = "") -> dict:
        verifier, challenge = self.pkce_pair()
        q = urlencode({"client_id": client_id, "redirect_uri": redirect_uri,
                       "response_type": "code", "state": state,
                       "code_challenge": challenge, "code_challenge_method": "S256"})
        return {"authorization_url": f"{OAUTH_AUTHORIZE}?{q}", "code_verifier": verifier}

    def oauth_exchange(self, client_id: str, client_secret: str, code: str,
                       redirect_uri: str, code_verifier: str) -> dict:
        body = {"grant_type": "authorization_code", "client_id": client_id, "code": code,
                "redirect_uri": redirect_uri, "code_verifier": code_verifier}
        headers = {"content-type": "application/json"}
        if client_secret:
            headers["authorization"] = f"Bearer {client_secret}"
        return self._client().request("POST", OAUTH_TOKEN, body=body, headers=headers)

    def refresh(self, secret: AccountSecret) -> Optional[AccountSecret]:
        oauth = dict(secret.oauth or {})
        rt = oauth.get("refresh_token")
        if not rt:
            return None
        client_id = oauth.get("client_id") or os.getenv("COZE_CLIENT_ID", "")
        client_secret = oauth.get("client_secret") or os.getenv("COZE_CLIENT_SECRET", "")
        if not client_id:
            return None
        body = {"grant_type": "refresh_token", "client_id": client_id, "refresh_token": rt}
        headers = {"content-type": "application/json"}
        if client_secret:
            headers["authorization"] = f"Bearer {client_secret}"
        r = self._client().request("POST", OAUTH_TOKEN, body=body, headers=headers)
        oauth["access_token"] = r.get("access_token", oauth.get("access_token"))
        oauth["refresh_token"] = r.get("refresh_token", rt)
        oauth["token_expires_at"] = time.time() + float(r.get("expires_in") or 3600)
        return AccountSecret(cookies=secret.cookies, browser_state=secret.browser_state, oauth=oauth)

    def verify(self, secret: AccountSecret) -> VerifyResult:
        headers = self.build_headers(secret, "/space-intro/api/user-info")
        try:
            r = self._client().request("GET", "https://www.coze.cn/space-intro/api/user-info",
                                       headers=headers)
            ok = int(r.get("code", -1)) == 0 if isinstance(r, dict) else False
            return VerifyResult(ok=ok, status="active" if ok else "invalid", raw=redact(r))
        except UpstreamError as e:
            s = str(e)
            # 只有明确鉴权失败才算凭证失效；网络/限流/风控算临时错误（risk，不降级）
            if "401" in s or "未登录" in s or "not logged" in s.lower():
                return VerifyResult(ok=False, status="invalid", message=s)
            return VerifyResult(ok=False, status="risk", message=s)

    def list_models(self, secret: AccountSecret) -> List[ModelSpec]:
        headers = self.build_headers(secret, "/v1/api/project/model_list")
        try:
            r = self._client().request("POST", WEB_BASE + "/v1/api/project/model_list",
                                       body={"include_inaccessible_models": "true"}, headers=headers)
        except UpstreamError:
            return []
        body = r.get("data") if isinstance(r.get("data"), dict) else r
        out = []
        for item in (body.get("model_list") or []):
            name = str(item.get("name") or item.get("display_name") or "")
            if name:
                out.append(ModelSpec(id=name, alias=name, display_name=str(item.get("display_name") or name),
                                     kind="video", raw=redact(item)))
        return out

    def credits(self, secret: AccountSecret) -> CreditView:
        headers = self.build_headers(secret, "/api/marketplace/trade/credit/balance/check")
        try:
            r = self._client().request("GET", WEB_BASE + "/api/marketplace/trade/credit/balance/check",
                                       headers=headers)
        except UpstreamError as e:
            return CreditView(error=str(e))
        data = r.get("data") if isinstance(r.get("data"), dict) else {}
        return CreditView(total=0.0, allocatable=0.0,
                          buckets={"sufficient": 1.0 if data.get("pass") else 0.0},
                          raw=redact(r))

    def checkin(self, secret: AccountSecret) -> Dict[str, Any]:
        url = os.getenv("COZE_CHECKIN_URL", "")
        if not url:
            return {"status": "unsupported", "reason": "no_confirmed_protocol_checkin_action"}
        headers = self.build_headers(secret, "/checkin")
        try:
            r = self._client().request("POST", url, body={}, headers=headers)
            return {"status": str(r.get("status") or "success"), "raw": redact(r)}
        except UpstreamError as e:
            return {"status": "failed", "message": str(e)}

    # ---- 生成（工作流） ----
    def submit(self, secret: AccountSecret, req: GenerateRequest) -> SubmitResult:
        params = dict(req.params or {})
        workflow_id = params.get("workflow_id") or req.model
        project_id = params.get("project_id")
        space_id = params.get("space_id")
        headers = self.build_headers(secret, "/api/workflow_api/test_run")
        if project_id and space_id:
            body = {"workflow_id": str(workflow_id), "input": params.get("parameters") or {"prompt": req.prompt},
                    "space_id": str(space_id), "commit_id": str(params.get("commit_id") or ""),
                    "project_id": str(project_id)}
            r = self._client().request("POST", WORKFLOW_BASE + "/api/workflow_api/test_run",
                                       body=body, headers=headers)
            if int(r.get("code", 0)) != 0:
                raise UpstreamError(str(r.get("msg") or "coze workflow failed"),
                                    self.classify_error(r), payload=redact(r))
            item = r.get("data") or {}
            return SubmitResult(upstream_task_id=str(item.get("execute_id") or ""),
                                transport="requests", raw=redact(r))
        raise UpstreamError("coze 需要 project_id/space_id/workflow_id 参数（放入 params）", "upstream")

    def poll(self, secret: AccountSecret, upstream_task_id: str,
             req: Optional[GenerateRequest] = None) -> PollResult:
        params = (req.params if req else {}) or {}
        workflow_id = params.get("workflow_id") or (req.model if req else "")
        project_id = params.get("project_id", "")
        space_id = params.get("space_id", "")
        headers = self.build_headers(secret, "/api/workflow_api/get_process")
        q = urlencode({"workflow_id": str(workflow_id), "space_id": str(space_id),
                       "execute_id": str(upstream_task_id), "need_async": "true"})
        r = self._client().request("GET", WORKFLOW_BASE + "/api/workflow_api/get_process?" + q,
                                   headers=headers)
        process = r.get("data") or {}
        status_code = int(process.get("executeStatus") or 0)
        media = []
        for node in (process.get("nodeResults") or []):
            out = node.get("output")
            if isinstance(out, str) and out.strip().startswith("{"):
                import json as _json
                try:
                    parsed = _json.loads(out)
                    for v in parsed.values():
                        if isinstance(v, str) and v.startswith("http"):
                            media.append(v)
                except Exception:
                    pass
        node_errors = [str(n.get("errorInfo") or "") for n in (process.get("nodeResults") or []) if n.get("errorInfo")]
        if status_code == 2:
            status = "success"
        elif status_code == 3 or node_errors:
            status = "failed"
        else:
            status = "processing"
        return PollResult(status=status, media=list(dict.fromkeys(media)),
                          error=(node_errors[0] if node_errors else "")[:500],
                          error_class="upstream" if status == "failed" else "",
                          progress=float(process.get("rate") or 0), raw=redact(r))

    def classify_error(self, payload: Any) -> str:
        if isinstance(payload, dict):
            return classify_error(payload.get("code"), str(payload.get("msg") or payload.get("message") or ""))
        return "upstream"
