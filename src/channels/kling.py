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

from .base import (AccountSecret, Channel, ChannelSpec, CreditView, VerifyResult,
                   looks_login_expired, sniff_account_type, verify_from_exception)

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

    # ---- 凭据 / 登录态 --------------------------------------------------
    @staticmethod
    def _cookie_of(secret: Optional[AccountSecret]) -> str:
        """Cookie 串：优先 extra['cookie']，否则拼接 cookies 字典。"""
        if not secret:
            return ""
        return (str(secret.extra.get("cookie") or "") or
                "; ".join(f"{k}={v}" for k, v in (secret.cookies or {}).items()))

    def _point_headers(self, cookie: str) -> Dict[str, str]:
        return {
            "User-Agent": UA,
            "Referer": self.KLING_BASE + "/app/",
            "Origin": self.KLING_BASE,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Content-Type": "application/json",
            "Cookie": cookie,
        }

    def verify(self, secret: AccountSecret) -> VerifyResult:
        """GET /api/account/pointAndTicket —— Cookie 鉴权探测登录态。

        实测（空 Cookie / 伪造 Cookie / 伪造 token 均一致）：上游返回
        HTTP 200 + ``{"status":500,"result":1,"data":null,"error":{"brief":"服务开小差了…"}}``。
        该响应「未登录」与「上游临时故障」**同形**，无法区分，因此按保守口径
        判 **risk** 而不是 invalid——保活任务不会因一次上游抖动误杀账号。
        只有 HTTP 401/403，或信封里出现明确的未登录字样，才判 invalid。
        """
        cookie = self._cookie_of(secret)
        if not cookie:
            return VerifyResult(ok=False, status="invalid",
                                message="缺少凭据：需要 klingai.com Cookie（含 kuaishou.ai.portal_st）")
        try:
            resp = requests.get(self.KLING_BASE + "/api/account/pointAndTicket",
                                headers=self._point_headers(cookie), timeout=30, verify=False)
        except Exception as exc:  # noqa: BLE001
            self._warn("可灵 verify 异常: %s", exc, exc_info=True)
            return verify_from_exception(exc, what="可灵")
        if resp.status_code in (401, 403):
            return VerifyResult(ok=False, status="invalid",
                                message="可灵登录态已失效（上游返回未授权），请重新登录导入 Cookie",
                                raw={"http_status": resp.status_code})
        if resp.status_code == 429 or resp.status_code >= 500:
            return VerifyResult(ok=False, status="risk",
                                message=f"可灵上游限流或服务异常（HTTP {resp.status_code}），暂不判定失效",
                                raw={"http_status": resp.status_code})
        try:
            body = resp.json() if resp.content else {}
        except ValueError:
            return VerifyResult(ok=False, status="risk",
                                message="可灵上游返回非 JSON（疑似风控拦截页），暂不判定失效",
                                raw={"_raw": resp.text[:300]})
        if not isinstance(body, dict):
            return VerifyResult(ok=False, status="risk",
                                message="可灵上游返回格式异常，暂不判定失效")
        err = body.get("error") or {}
        err_txt = " ".join(str(x) for x in (err.get("brief"), err.get("detail"),
                                            err.get("type"), body.get("message")) if x)
        if looks_login_expired(err_txt):
            return VerifyResult(ok=False, status="invalid",
                                message="可灵登录态已失效，请重新登录导入 Cookie", raw=body)
        data = body.get("data")
        if body.get("status") in (200, 0) and isinstance(data, dict) and data:
            try:
                points_txt = "%g" % float(data.get("points") or 0)
            except (TypeError, ValueError):
                points_txt = str(data.get("points"))
            self._info("可灵 verify 通过，points=%s", points_txt)
            return VerifyResult(ok=True, status="active",
                                message=f"登录态有效，积分余额 {points_txt}",
                                account_type=sniff_account_type(body), raw=body)
        # 信封非 200 / data 为空：可能是未登录，也可能是上游抽风 —— 一律 risk
        self._warn("可灵 verify 无法判定：status=%s data=%s err=%s",
                   body.get("status"), type(data).__name__, err_txt[:120])
        return VerifyResult(ok=False, status="risk",
                            message="可灵上游返回通用错误（未登录或上游异常均如此），暂不判定失效",
                            raw=body)
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

    def _passport_session(self) -> requests.Session:
        """预热 session：必须先访问站点拿 passport 需要的 cookie（did 等）。

        实测教训：裸 requests.post 发码会被快手判 「请输入有效的手机号」
        （错误码 100110011）——任何号段都一样，真实原因是缺 session cookie。
        """
        s = requests.Session()
        s.headers.update(self._headers())
        for url in ("https://klingai.com/", "https://klingai.com/app/", PASSPORT + "/"):
            try:
                s.get(url, timeout=15, verify=False)
            except Exception:  # noqa: BLE001
                pass
        return s

    def login_send_sms(self, phone: str, area_code: str = "86") -> Dict[str, Any]:
        """发送快手 passport 短信验证码（会真实下发短信）。"""
        s = self._passport_session()
        r = s.post(PASSPORT + "/pass/kuaishou/sms/requestMobileCode",
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

    def checkin(self, secret: AccountSecret) -> Dict[str, Any]:
        """签到/每日奖励探测。统一契约：{"ok", "message", "reward"}。"""
        cookie = self._cookie_of(secret)
        if not cookie:
            return {"ok": False, "message": "缺少凭据：需要 klingai.com Cookie（请重新登录）"}
        import requests as _rq
        s = _rq.Session()
        s.headers.update({"User-Agent": self.UA if hasattr(self, "UA") else
                          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/135.0.0.0",
                          "Referer": "https://klingai.com/", "Origin": "https://klingai.com"})
        for ck in cookie.split("; "):
            if "=" in ck:
                k, v = ck.split("=", 1)
                s.cookies.set(k.strip(), v.strip())
        # 试常见签到端点（401/参数错=存在，404=不存在）
        for method, path, body in (("POST", "/api/task/sign", {}),
                                   ("GET", "/api/account/sign/status", None),
                                   ("POST", "/api/account/sign", {}),
                                   ("GET", "/api/account/daily/reward", None)):
            try:
                if method == "GET":
                    r = s.get(self.KLING_BASE + path, timeout=15, verify=False)
                else:
                    r = s.post(self.KLING_BASE + path, json=body, timeout=15, verify=False)
                if r.status_code == 404:
                    continue
                try:
                    j = r.json()
                except Exception:  # noqa: BLE001
                    continue
                # 信封里出现「成功/已签到/奖励」即认为命中
                txt = str(j)
                if r.status_code in (200, 201) and ("sign" in txt.lower() or "积分" in txt or "point" in txt.lower()):
                    return {"ok": True,
                            "message": "可灵签到接口命中（%s %s），返回 %s" % (method, path, txt[:120]),
                            "reward": path}
            except Exception:  # noqa: BLE001
                continue
        return {"ok": False, "message": "可灵上游未提供公开的每日签到接口（已探测 4 个候选路径均不存在）"}

    # ---- 必需 abstract（本渠道只做登录）--------------------------------
    def build_headers(self, secret: AccountSecret, path: str, body: Any = None,
                      extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h = {"User-Agent": UA, "Content-Type": "application/json"}
        if secret and secret.extra.get("cookie"):
            h["Cookie"] = secret.extra["cookie"]
        return h

    def submit(self, secret: AccountSecret, req: Any) -> Any:
        raise NotImplementedError("可灵渠道在本平台只暴露登录能力；生成请用 E:/API管理工具/klingai")

    def poll(self, secret: AccountSecret, upstream_task_id: str,
                    req: Optional["GenerateRequest"] = None,
                    **kw: Any) -> Any:
        raise NotImplementedError("可灵渠道在本平台只暴露登录能力；生成请用 E:/API管理工具/klingai")
