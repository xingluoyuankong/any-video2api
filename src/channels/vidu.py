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

import re
import ssl
from typing import Any, Dict, List, Optional

import requests
import urllib3
from requests.adapters import HTTPAdapter

from .base import AccountSecret, Channel, ChannelSpec, CreditView

urllib3.disable_warnings()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
BASE = "https://service.vidu.cn"
APP_VERSION = "2.18.169-cn"
_CN_MOBILE_RE = re.compile(r"^1[3-9]\d{9}$")


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
            "X-App-Version": APP_VERSION,
            "Accept-Language": "zh-CN",
            "Origin": "https://www.vidu.cn",
            "Referer": "https://www.vidu.cn/",
        }

    @staticmethod
    def _full_phone(phone: str, area_code: str = "86") -> str:
        """「13800138000」+「86」→ 前端同款「+8613800138000」。"""
        p = (phone or "").strip()
        if p.startswith("+"):
            return p
        return "+%s%s" % (area_code, p)

    # ---- 在线登录 -------------------------------------------------------
    def login_methods(self) -> List[str]:
        return ["sms"]

    def login_send_sms(self, phone: str, area_code: str = "86") -> Dict[str, Any]:
        """发送登录短信验证码（会真实下发短信）。

        实测（2026-10）：body {channel:"sms", receiver:"+86<11位手机号>", purpose:"login",
        locale:"zh"} 是唯一正确格式。receiver 必须是 **格式合法** 的大陆手机号
        （1[3-9] 开头 11 位，带 +86 前缀）—— proto 层直接校验，格式不合法一律
        返回 400 FieldInvalid fields="channel,receiver"（channel 只是陪绑报错）。
        """
        if not _CN_MOBILE_RE.match((phone or "").strip()):
            raise RuntimeError("手机号格式无效：%r（需 1[3-9] 开头的 11 位大陆手机号，"
                               "上游 proto 层对格式非法号码直接返回 FieldInvalid）" % phone)
        s = _tls13_only_session()
        body = {"channel": "sms", "receiver": self._full_phone(phone, area_code),
                "purpose": "login", "locale": "zh"}
        r = s.post(BASE + "/iam/v1/users/send-auth-code", json=body,
                   headers=self._headers(), timeout=20)
        try:
            j = r.json()
        except ValueError:
            raise RuntimeError("上游返回非 JSON（可能被 EdgeOne 拦）：%s" % r.text[:200])
        if r.status_code >= 400:
            reason = j.get("reason") or ""
            fields = (j.get("metadata") or {}).get("fields") or ""
            if reason == "FieldInvalid":
                raise RuntimeError("发码被拒（FieldInvalid %s）：receiver 必须为 +86+合法11位手机号"
                                   % fields)
            raise RuntimeError("发码失败 HTTP %s %s: %s" % (r.status_code, reason,
                                                           j.get("message") or j))
        return j

    def login_by_sms(self, phone: str, code: str, area_code: str = "86") -> Optional[AccountSecret]:
        s = _tls13_only_session()
        body = {"id_type": "phone", "identity": self._full_phone(phone, area_code),
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

    # ---- 额度 -----------------------------------------------------------
    def _cookie_of(self, secret: AccountSecret) -> str:
        if not secret:
            return ""
        if secret.extra.get("cookie"):
            return str(secret.extra["cookie"])
        return "; ".join(f"{k}={v}" for k, v in (secret.cookies or {}).items())

    def credits(self, secret: AccountSecret) -> CreditView:
        """GET /credit/v1/credits/me（总积分）+ /iam/v1/users/every-day-credit（每日免费额度）。

        必须 TLS1.3-only 指纹（绕 EdgeOne）；Cookie 鉴权（主会话字段 sid）。
        """
        cookie = self._cookie_of(secret)
        if not cookie:
            return CreditView(error="缺少凭据：需要 vidu.cn Cookie（短信登录或官网导入）")
        s = _tls13_only_session()
        out_err = ""
        payloads: Dict[str, Any] = {}
        for tag, path in (("credits", "/credit/v1/credits/me"),
                          ("daily", "/iam/v1/users/every-day-credit")):
            try:
                r = s.get(BASE + path, headers={**self._headers(), "Cookie": cookie},
                          timeout=20)
                if r.status_code in (401, 403):
                    out_err = "HTTP %s，Cookie 可能已失效" % r.status_code
                    break
                try:
                    payloads[tag] = r.json() if r.content else {}
                except ValueError:
                    payloads[tag] = {"_raw": r.text[:300]}
            except Exception as e:  # noqa: BLE001
                out_err = "网络请求失败: %s" % e
                break

        def num(x: Any) -> float:
            try:
                return float(x or 0)
            except Exception:  # noqa: BLE001
                return 0.0

        def dig(node: Any, *names: str) -> float:
            """深度优先找第一个数值型目标字段。"""
            if isinstance(node, dict):
                for n in names:
                    v = node.get(n)
                    if isinstance(v, (int, float)):
                        return float(v)
                for v in node.values():
                    got = dig(v, *names)
                    if got:
                        return got
            elif isinstance(node, list):
                for v in node:
                    got = dig(v, *names)
                    if got:
                        return got
            return 0.0

        cr = payloads.get("credits") or {}
        daily = payloads.get("daily") or {}
        total = dig(cr, "credit", "credits", "total", "balance", "remaining", "amount")
        free = dig(daily, "credit", "credits", "total", "balance", "remaining", "amount")
        view = CreditView(total=total, allocatable=total,
                          buckets={"credit": total, "every_day_free": free},
                          raw=payloads)
        if out_err and not total and not free:
            view.error = out_err
        view.message = ("来源: GET /credit/v1/credits/me + /iam/v1/users/every-day-credit"
                        "（credit=可用数）")
        return view

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
