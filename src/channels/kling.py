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

from .base import AccountSecret, Channel, ChannelSpec

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
