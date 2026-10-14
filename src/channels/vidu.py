# -*- coding: utf-8 -*-
"""Vidu 渠道（vidu.cn）—— 手机号 + 短信验证码原生登录。

逆向自 www.vidu.cn 前端 JS（/iam/v1/*）：
    POST https://service.vidu.cn/iam/v1/users/send-auth-code  {channel, receiver, purpose}
    POST https://service.vidu.cn/iam/v1/users/login           {id_type, identity, auth_type, credential}
关键：EdgeOne 看 TLS ClientHello 指纹 —— 只 offer 单一 TLS 版本才放行（默认 1.2+1.3 会被拦）。
全程纯 HTTP，不启动浏览器、不读写本地浏览器 cookie。

注：本渠道目前只暴露「登录」能力；生成能力见独立项目 E:/API管理工具/vidu。
"""
from __future__ import annotations

import ssl
from typing import Any, Dict, List, Optional

import requests
import urllib3
from requests.adapters import HTTPAdapter

from .base import AccountSecret, Channel, ChannelSpec

urllib3.disable_warnings()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36")
BASE = "https://service.vidu.cn"


def _tls13_only_session() -> requests.Session:
    """注入 TLS1.3-only SSLContext —— 绕过腾讯 EdgeOne 的 TLS 指纹挑战。

    EdgeOne 只看 TLS ClientHello 指纹：同时 offer 1.2+1.3 → 100% 挑战页；
    只 offer 单一版本 → 放行。必须同时覆盖 poolmanager 与 proxy_manager。
    """
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    class _Adapter(HTTPAdapter):
        def __init__(self, ssl_context: ssl.SSLContext, **kwargs: Any):
            self._ssl_context = ssl_context
            super().__init__(**kwargs)

        def init_poolmanager(self, *a: Any, **kw: Any) -> None:
            kw["ssl_context"] = self._ssl_context
            return super().init_poolmanager(*a, **kw)

        def proxy_manager_for(self, *a: Any, **kw: Any) -> Any:
            kw["ssl_context"] = self._ssl_context
            return super().proxy_manager_for(*a, **kw)

    s = requests.Session()
    s.mount("https://", _Adapter(ctx))
    return s


class ViduChannel(Channel):
    """Vidu —— 手机号 + 短信验证码。"""

    def _headers(self) -> Dict[str, str]:
        return {
            "User-Agent": UA,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Platform": "web",
            "Accept-Language": "zh-CN",
            "Origin": "https://www.vidu.cn",
            "Referer": "https://www.vidu.cn/",
        }

    # ---- 在线登录 -------------------------------------------------------
    def login_methods(self) -> List[str]:
        return ["sms"]

    def login_send_sms(self, phone: str, area_code: str = "86") -> Dict[str, Any]:
        """发送登录短信验证码（会真实下发短信）。"""
        s = _tls13_only_session()
        body = {"channel": "sms", "receiver": "+%s%s" % (area_code, phone),
                "purpose": "login", "locale": "zh"}
        r = s.post(BASE + "/iam/v1/users/send-auth-code", json=body,
                   headers=self._headers(), timeout=20)
        try:
            return r.json()
        except ValueError:
            raise RuntimeError("上游返回非 JSON（可能被 EdgeOne 拦）：%s" % r.text[:200])

    def login_by_sms(self, phone: str, code: str, area_code: str = "86") -> Optional[AccountSecret]:
        s = _tls13_only_session()
        body = {"id_type": "phone", "identity": phone,
                "auth_type": "authcode", "credential": str(code)}
        r = s.post(BASE + "/iam/v1/users/login", json=body,
                   headers=self._headers(), timeout=20)
        try:
            j = r.json()
        except ValueError:
            raise RuntimeError("上游返回非 JSON（可能被 EdgeOne 拦）：%s" % r.text[:200])
        cookie = "; ".join(f"{c.name}={c.value}" for c in s.cookies)
        if not cookie:
            raise RuntimeError("登录失败：%s" % j)
        return AccountSecret(cookies=dict(s.cookies), oauth=None,
                             browser_state=None, extra={"cookie": cookie})

    # ---- 必需 abstract（本渠道只做登录）--------------------------------
    def build_headers(self, secret: AccountSecret, path: str, body: Any = None,
                      extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h = {"User-Agent": UA, "Content-Type": "application/json"}
        if secret and secret.extra.get("cookie"):
            h["Cookie"] = secret.extra["cookie"]
        return h

    def submit(self, secret: AccountSecret, req: Any) -> Any:
        raise NotImplementedError("Vidu 渠道在本平台只暴露登录能力；生成请用 E:/API管理工具/vidu")

    def poll(self, secret: AccountSecret, upstream_task_id: str, **kw: Any) -> Any:
        raise NotImplementedError("Vidu 渠道在本平台只暴露登录能力；生成请用 E:/API管理工具/vidu")
