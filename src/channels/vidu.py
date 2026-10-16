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

from .base import (AccountSecret, Channel, ChannelSpec, CreditView,
                   VerifyResult, sniff_account_type)

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


def _edgeone_guarded_request(session: requests.Session, method: str, url: str, *,
                             headers: Dict[str, str], max_retry: int = 2, **kw: Any):
    """带 EdgeOne 挑战重放的请求。

    EdgeOne 的 JS 盾（EO_Bot_Ssid/__tst_status）行为：首次请求返回 200 + 混淆 HTML
    （内容含 __tst_status=计算值），同时 Set-Cookie 下发挑战；浏览器执行 JS 设置
    cookie 后自动重载放行。requests 环境的对策：
      1. 若响应是 text/html 且含 "__tst_status"，从 HTML 里提取计算出的 cookie 值
         （脚本里 `document.cookie="__tst_status="+a(0)` 的 a(0) 是纯算术拼串，
         可直接按常量求值），附加到 Cookie 头后重放。
      2. 提取失败则原样返回（由调用方按非 JSON 处理）。
    """
    resp = session.request(method, url, headers=headers, timeout=kw.pop("timeout", 30), **kw)
    ct = (resp.headers.get("content-type") or "").lower()
    if "text/html" not in ct or "__tst_status" not in resp.text[:2000]:
        return resp
    import re as _re
    m = _re.search(r't\+=e\[_0x649a\("0x0"\)\],t\+=e\[_0x649a\("0x1"\)\],t=e\[_0x649a\("0x2"\)\]\(t,e\[_0x649a\("0x3"\)\])', resp.text)
    # 混淆常量表不可静态求值所有情形；退而求其次：直接用 Set-Cookie 里给的挑战值
    set_ck = resp.headers.get("set-cookie") or ""
    extra = ""
    for part in set_ck.split(","):
        if "__tst_status=" in part or "EO_Bot_Ssid" in part:
            kv = part.split(";")[0].strip()
            if "=" in kv:
                extra += ("; " if extra else "") + kv
    if extra:
        headers = dict(headers)
        headers["Cookie"] = (headers.get("Cookie", "") + "; " + extra).strip("; ")
        return session.request(method, url, headers=headers, timeout=30, **kw)
    return resp


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

    def verify(self, secret: AccountSecret) -> VerifyResult:
        """GET /credit/v1/credits/me —— Cookie 鉴权探测登录态。

        必须 TLS1.3-only 指纹（绕 EdgeOne）。
        判定口径（保守，避免保活误杀）：
          * 无 cookie          -> invalid，不发请求
          * HTTP 401 / 403     -> invalid（凭据确实失效）
          * 网络 / 超时 / 5xx / 429 -> risk（不降级，防止一次抖动误杀）
          * 其它未知           -> risk
        """
        cookie = self._cookie_of(secret)
        if not cookie:
            return VerifyResult(ok=False, status="invalid",
                                message="缺少凭据：需要 vidu.cn Cookie（短信登录或官网导入）")
        # 复用 credits() 的多接口聚合（TLS1.3-only + 结构解析都在里面）
        try:
            cv = self.credits(secret)
        except Exception as exc:  # noqa: BLE001
            return VerifyResult(ok=False, status="risk",
                                message="网络异常，暂不判定失效：%s" % exc)
        if cv.error:
            e = str(cv.error)
            if "401" in e or "403" in e or "失效" in e:
                return VerifyResult(ok=False, status="invalid",
                                    message="登录态已失效，请重新登录")
            return VerifyResult(ok=False, status="risk",
                                message="查询积分失败，暂不判定失效：%s" % e[:100])
        return VerifyResult(ok=True, status="active",
                            message="登录态有效，积分 %g" % cv.total,
                            raw={"credits": cv.total})

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
                # 必须走 EdgeOne 挑战重放：直接 s.get 撞上挑战页会拿到 HTML，
                # 轻则积分解析成 0，重则伴随 403 被 verify 误判"登录态失效"——
                # 实测这就是 vidu 凭证"反复失效"的根因。
                r = _edgeone_guarded_request(
                    s, "GET", BASE + path,
                    headers={**self._headers(), "Cookie": cookie}, timeout=20)
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

    def checkin(self, secret: AccountSecret) -> Dict[str, Any]:
        """签到/每日奖励探测。统一契约：{"ok", "message", "reward"}。"""
        cookie = self._cookie_of(secret)
        if not cookie:
            return {"ok": False, "message": "缺少凭据：需要 vidu.cn Cookie（请重新登录）"}
        s = _tls13_only_session()
        h = {**self._headers(), "Cookie": cookie}
        # 每日免费额度查询（自动发放类，无独立签到按钮）
        try:
            r = s.get(BASE + "/iam/v1/users/every-day-credit", headers=h, timeout=20)
            if r.status_code in (401, 403):
                return {"ok": False, "message": "登录态已失效，请重新登录"}
            try:
                j = r.json()
            except Exception:  # noqa: BLE001
                j = {}
            data = j.get("data") if isinstance(j, dict) else None
            got = ""
            if isinstance(data, dict):
                got = str(data.get("credit") or data.get("freeCredit") or data.get("amount") or "")
            return {"ok": True,
                    "message": ("Vidu 每日免费额度：%s（每日自动发放，无需手动签到）" % got) if got
                               else "Vidu 每日免费额度由系统自动发放（查询正常），无独立签到按钮",
                    "reward": got}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": "查询每日额度失败（网络异常）：%s" % exc}

    # ---- 必需 abstract（本渠道只做登录）--------------------------------
    def build_headers(self, secret: AccountSecret, path: str, body: Any = None,
                      extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h = {"User-Agent": UA, "Content-Type": "application/json"}
        if secret and secret.extra.get("cookie"):
            h["Cookie"] = secret.extra["cookie"]
        return h

    # ---- 生成（从独立项目移植，网页 Cookie 模式）-------------------------
    # 提交: POST /vidu/v1/tasks -> {id}；轮询: GET /vidu/v1/tasks/{id} -> {state, creations}
    _PENDING_STATES = {"created", "queueing", "preparation", "scheduling",
                       "processing", "waiting"}
    _SUCCESS_STATES = {"success", "done", "succeeded"}
    _FAILED_STATES = {"failed", "error", "cancelled", "canceled"}

    def submit(self, secret: AccountSecret, req: Any) -> Any:
        from .base import SubmitResult
        cookie = self._cookie_of(secret)
        if not cookie:
            raise RuntimeError("缺少凭据：需要 vidu.cn Cookie（短信登录或官网导入）")
        prompts: List[Dict[str, Any]] = []
        if getattr(req, "prompt", ""):
            prompts.append({"type": "text", "content": req.prompt})
        for img in (getattr(req, "images", None) or []):
            prompts.append({"type": "image", "content": img, "src_imgs": [img]})
        kind = "text2video" if (getattr(req, "kind", "video") or "video") == "video" else "text2image"
        # 实测实锤（登录态多轮试错）：
        #  * 不传 resolution  -> 200 成功（服务端按账号等级定，free 默认 512）
        #  * 传 resolution:720p -> 400 SubsplanNotSupportedForTask（free 订阅不支持）
        #  * 传 resolution:480p -> 400 invalid field settings
        # 所以 resolution 一律不下发，由上游按账号等级决定；其余参数照常传。
        settings: Dict[str, Any] = {
            "sample_count": 1,
            "schedule_mode": "normal",
            "model_version": str(getattr(req, "model", "") or "viduq2"),
            "style": "general",
            "use_trial": False,  # 实测 trial 通道新号报 ActivityError，走正式通道扣积分
        }
        if getattr(req, "duration", None):
            settings["duration"] = int(req.duration)
        if getattr(req, "ratio", ""):
            settings["aspect_ratio"] = req.ratio
        body = {"input": {"prompts": prompts, "editor_mode": "normal", "enhance": False},
                "type": kind, "settings": settings}

        s = _tls13_only_session()
        r = s.post(BASE + "/vidu/v1/tasks", json=body,
                   headers={**self._headers(), "Cookie": cookie}, timeout=60)
        if r.status_code in (401, 403):
            raise RuntimeError("登录态已失效（HTTP %d），请重新登录" % r.status_code)
        try:
            j = r.json() if r.content else {}
        except ValueError:
            raise RuntimeError("上游返回非 JSON（可能被 EdgeOne 拦）：%s" % r.text[:200])
        code = str(j.get("code", "0")) if isinstance(j, dict) else "0"
        if code not in ("0", "200", ""):
            raise RuntimeError("提交失败：%s %s" % (code, str(j.get("message") or j)[:150]))
        tid = (j or {}).get("id") or (j or {}).get("task_id") or ((j.get("data") or {}).get("id") if isinstance(j.get("data"), dict) else None)
        if not tid:
            raise RuntimeError("提交成功但未返回任务 id：%s" % str(j)[:250])
        return SubmitResult(upstream_task_id=str(tid), transport="requests",
                            raw={"channel": "vidu"})

    def poll(self, secret: AccountSecret, upstream_task_id: str,
                    req: Optional["GenerateRequest"] = None,
                    **kw: Any) -> Any:
        from .base import PollResult
        cookie = self._cookie_of(secret)
        if not cookie:
            return PollResult(status="failed", error="缺少凭据：需要 vidu.cn Cookie",
                              error_class="auth_or_risk", raw={"channel": "vidu"})
        try:
            s = _tls13_only_session()
            r = _edgeone_guarded_request(
                s, "GET", BASE + "/vidu/v1/tasks/" + str(upstream_task_id),
                headers={**self._headers(), "Cookie": cookie}, timeout=30)
            if r.status_code in (401, 403):
                return PollResult(status="failed", error="登录态已失效（HTTP %d）" % r.status_code,
                                  error_class="auth_or_risk", raw={"channel": "vidu"})
            j = r.json() if r.content else {}
        except Exception as exc:  # noqa: BLE001
            return PollResult(status="processing", raw={"channel": "vidu", "poll_error": str(exc)[:150]})

        state = str((j or {}).get("state") or (j or {}).get("status") or "").lower()
        creations = (j or {}).get("creations") or []
        urls, covers = [], []
        for c in creations:
            if isinstance(c, dict):
                u = c.get("url") or c.get("video_url") or c.get("uri")
                if u:
                    urls.append(u)
                if c.get("cover"):
                    covers.append(c["cover"])
        if state in self._FAILED_STATES:
            err_detail = str(j.get("err_msg") or j.get("err_code") or "")
            return PollResult(status="failed",
                              error="上游状态 %s%s" % (state, (": " + err_detail) if err_detail else ""),
                              error_class="upstream",
                              raw={"channel": "vidu", "state": state, "err_code": j.get("err_code")})
        if urls:
            return PollResult(status="success", media=urls, covers=covers,
                              raw={"channel": "vidu", "state": state})
        return PollResult(status="processing", raw={"channel": "vidu", "state": state or "pending"})
