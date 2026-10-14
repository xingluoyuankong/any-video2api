# -*- coding: utf-8 -*-
"""可灵 AI 渠道（klingai.com）—— 快手 passport 原生登录。

逆向自 klingai.com 前端 JS：
    POST https://id.kuaishou.com/pass/kuaishou/sms/requestMobileCode  {mobile, sid, type}
    POST https://id.kuaishou.com/pass/kuaishou/login/phone/v2         {phone, code|password, sid}
sid = "kuaishou.ai.portal"（实测唯一合法值，其他值返回「请求不合法」）
全程纯 HTTP，不启动浏览器、不读写本地浏览器 cookie。

注：本渠道目前只暴露「登录」能力；生成能力见独立项目 E:/API管理工具/klingai。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests
import urllib3

from .base import AccountSecret, Channel, ChannelSpec, CreditView

urllib3.disable_warnings()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36")
PASSPORT = "https://id.kuaishou.com"
SID = "kuaishou.ai.portal"


class KlingChannel(Channel):
    """可灵 AI —— 手机号+短信 / 手机号+密码（快手 passport）。"""

    def _headers(self) -> Dict[str, str]:
        return {
            "User-Agent": UA,
            "Referer": "https://klingai.com/",
            "Origin": "https://klingai.com",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    def _cookie_string(self, session: requests.Session) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in session.cookies)

    # ---- 额度 -----------------------------------------------------------
    KLING_BASE = "https://klingai.com"

    def credits(self, secret: AccountSecret) -> CreditView:
        """GET /api/account/pointAndTicket -> 积分/券余额（网页版 Cookie 鉴权）。"""
        if not secret:
            return CreditView(error="缺少凭据：需要 klingai.com Cookie（含 kuaishou.ai.portal_st）")
        cookie = str(secret.extra.get("cookie") or "") or \
            "; ".join(f"{k}={v}" for k, v in (secret.cookies or {}).items())
        if not cookie:
            return CreditView(error="缺少凭据：需要 klingai.com Cookie（含 kuaishou.ai.portal_st）")
        headers = {
            "User-Agent": UA,
            "Referer": self.KLING_BASE + "/app/",
            "Origin": self.KLING_BASE,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Content-Type": "application/json",
            "Cookie": cookie,
        }
        try:
            resp = requests.get(self.KLING_BASE + "/api/account/pointAndTicket",
                                headers=headers, timeout=30, verify=False)
        except Exception as e:  # noqa: BLE001
            return CreditView(error="网络请求失败: %s" % e)
        try:
            body = resp.json() if resp.content else {}
        except ValueError:
            return CreditView(error="上游返回非 JSON (HTTP %s): %s" % (resp.status_code, resp.text[:200]))
        # 可灵统一信封 {status, result, data, error}
        if resp.status_code in (401, 403) or not isinstance(body, dict):
            return CreditView(error="HTTP %s，Cookie 可能已失效" % resp.status_code, raw=body)
        if body.get("status") not in (200, 0) or body.get("result") not in (1, True, None):
            err = body.get("error") or {}
            detail = err.get("detail") or err.get("brief") or body.get("message") or str(body)[:200]
            return CreditView(error="上游业务错误: %s" % detail, raw=body)
        data = body.get("data") or {}

        def num(x: Any) -> float:
            try:
                return float(x or 0)
            except Exception:  # noqa: BLE001
                return 0.0

        points = num(data.get("points"))
        view = CreditView(
            total=points,                       # 可用数 = 积分余额
            allocatable=points,
            buckets={
                "points": points,
                "future_points": num(data.get("futurePoints")),
                "tickets": num(data.get("tickets")),
            },
            raw=body,
        )
        view.message = "来源: GET /api/account/pointAndTicket（points=可用数）"
        return view

    # ---- 在线登录 -------------------------------------------------------
    def login_methods(self) -> List[str]:
        return ["sms", "pwd"]

    def login_send_sms(self, phone: str, area_code: str = "86") -> Dict[str, Any]:
        """发送快手 passport 短信验证码（会真实下发短信）。"""
        r = requests.post(PASSPORT + "/pass/kuaishou/sms/requestMobileCode",
                          data={"mobile": phone, "sid": SID, "type": 1},
                          headers=self._headers(), timeout=20, verify=False)
        j = r.json() if r.content else {}
        if j.get("error_msg"):
            raise RuntimeError(j["error_msg"])
        return j

    def _login(self, data: Dict[str, Any]) -> AccountSecret:
        s = requests.Session()
        r = s.post(PASSPORT + "/pass/kuaishou/login/phone/v2", data=data,
                   headers=self._headers(), timeout=20, verify=False)
        j = r.json() if r.content else {}
        if j.get("result") != 1:
            raise RuntimeError(j.get("error_msg") or j)
        cookie = self._cookie_string(s)
        if not cookie:
            raise RuntimeError("登录成功但未拿到 Cookie")
        return AccountSecret(cookies=dict(s.cookies), oauth=None,
                             browser_state=None, extra={"cookie": cookie})

    def login_by_sms(self, phone: str, code: str, area_code: str = "86") -> Optional[AccountSecret]:
        return self._login({"phone": phone, "code": str(code), "sid": SID})

    def login_by_password(self, account: str, password: str) -> Optional[AccountSecret]:
        return self._login({"phone": account, "password": password, "sid": SID})

    # ---- 必需 abstract（本渠道只做登录）--------------------------------
    def build_headers(self, secret: AccountSecret, path: str, body: Any = None,
                      extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h = {"User-Agent": UA, "Content-Type": "application/json"}
        if secret and secret.extra.get("cookie"):
            h["Cookie"] = secret.extra["cookie"]
        return h

    def submit(self, secret: AccountSecret, req: Any) -> Any:
        raise NotImplementedError("可灵渠道在本平台只暴露登录能力；生成请用 E:/API管理工具/klingai")

    def poll(self, secret: AccountSecret, upstream_task_id: str, **kw: Any) -> Any:
        raise NotImplementedError("可灵渠道在本平台只暴露登录能力；生成请用 E:/API管理工具/klingai")
