# -*- coding: utf-8 -*-
"""统一传输层（requests 为主，Playwright 为可选兜底）。

渠道只需声明 headers/body，传输、重试、审计、错误分类、**防风控**交给本模块：
请求前做 熔断检查 -> 限流 -> 人类化节流；请求后把结果喂回熔断器。
`throttle=False` 可关闭防风控（用于心跳保活等无需限流的轻量请求）。
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional
from urllib.parse import urljoin

import requests

from src.core.config import config
from src.core.errors import UpstreamError, classify_error
from src.platform import audit
from src.platform.antiban import antiban as _antiban


class HttpClient:
    def __init__(self, base_url: str = "", proxy: Optional[str] = None, timeout: int = 120,
                 user_agent: str = "Mozilla/5.0", channel: str = "", account_id: str = "",
                 retries: Optional[int] = None, fingerprint: Optional[Dict[str, Any]] = None):
        self.base_url = (base_url or "").rstrip("/")
        self.channel = channel
        self.account_id = account_id
        self.timeout = int(timeout or 120)
        self.retries = config.max_network_retries if retries is None else retries
        self.fingerprint = fingerprint or {}
        self.session = requests.Session()
        # 指纹优先：同一账号始终使用同一 UA / Accept-Language，避免被判为新设备
        ua = self.fingerprint.get("user_agent") or user_agent or "Mozilla/5.0"
        headers = {"User-Agent": ua, "Accept": "application/json, text/plain, */*"}
        if self.fingerprint.get("accept_language"):
            headers["Accept-Language"] = self.fingerprint["accept_language"]
        self.session.headers.update(headers)
        # 账号级固定出口优先（防风控核心）：一个账号 ↦ 一条固定代理，优先于渠道默认出口；
        # 未绑定则回落渠道默认。延迟 import 避免与 accounts 的初始化顺序耦合。
        if config.proxy_enabled and account_id:
            try:
                from src.platform.proxy_pool import proxy_pool
                bound = proxy_pool.proxy_for(account_id)
                if bound:
                    proxy = bound
            except Exception:
                pass
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

    def _url(self, path: str) -> str:
        if str(path).startswith("http"):
            return path
        return urljoin(self.base_url + "/", str(path).lstrip("/"))

    def _guard(self, throttle: bool) -> bool:
        """是否需要过防风控。无账号绑定或总开关关闭时不过。"""
        return bool(throttle and config.antiban_enabled and self.account_id)

    def request(self, method: str, path: str, body: Any = None, params: Optional[dict] = None,
                headers: Optional[dict] = None, timeout: Optional[int] = None,
                throttle: bool = True) -> Any:
        url = self._url(path)
        attempts = max(1, self.retries)
        last = None
        guard = self._guard(throttle)

        if guard:
            decision = _antiban.before_request(self.channel, self.account_id, config.antiban_rpm_limit)
            if not decision.get("allowed"):
                # 熔断打开：直接失败，交由 Worker 冷却该账号，不发上游
                raise UpstreamError(f"antiban blocked: {decision.get('reason') or 'circuit_open'}",
                                    "auth_or_risk")

        for i in range(attempts):
            try:
                audit.write("request", channel=self.channel, account_id=self.account_id,
                            method=method, url=url, body=body, headers=headers)
                r = self.session.request(method.upper(), url, json=body, params=params,
                                         headers=headers, timeout=timeout or self.timeout)
                try:
                    payload = r.json()
                except Exception:
                    payload = {"text": r.text[:2000]}
                audit.write("response", channel=self.channel, account_id=self.account_id,
                            method=method, url=url, status=r.status_code, body=payload, headers=dict(r.headers))
                if r.status_code >= 500 and i < attempts - 1:
                    time.sleep(0.5 * (i + 1))
                    continue
                if r.status_code >= 400:
                    ec = classify_error(r.status_code, str(payload))
                    if guard:
                        _antiban.after_request(self.channel, self.account_id, False, ec)
                    raise UpstreamError(f"upstream HTTP {r.status_code}", ec, r.status_code, payload)
                if guard:
                    _antiban.after_request(self.channel, self.account_id, True)
                return payload
            except UpstreamError:
                raise
            except requests.RequestException as e:
                last = e
                audit.write("error", channel=self.channel, account_id=self.account_id,
                            method=method, url=url, body=body, headers=headers)
                if i < attempts - 1:
                    time.sleep(0.5 * (i + 1))
                    continue
                if guard:
                    _antiban.after_request(self.channel, self.account_id, False, "timeout")
                raise UpstreamError(f"upstream network error: {e}", "timeout") from e
        raise UpstreamError(f"upstream network error: {last}", "timeout")


def make_client(spec, secret=None, account_id: str = "", proxy: Optional[str] = None) -> HttpClient:
    """按渠道描述符构造 HTTP 客户端；若 secret 携带指纹则用于统一 UA。"""
    fingerprint = {}
    if secret is not None:
        fingerprint = (getattr(secret, "extra", None) or {}).get("fingerprint") or {}
    return HttpClient(base_url=spec.base_url, proxy=proxy or spec.proxy or None,
                      timeout=spec.timeout, user_agent=spec.user_agent,
                      channel=spec.name, account_id=account_id, fingerprint=fingerprint)
