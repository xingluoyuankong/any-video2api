# -*- coding: utf-8 -*-
"""海螺 AI 渠道（hailuoai.video）。

逆向自 hailuoai.video 前端 JS，实现**手机号 + 短信验证码**原生登录：
    POST /v1/api/user/login/sms/send   {phone, region}
    POST /v1/api/user/login/phone      {phone, code, region}  -> _token
签名头 yy = md5(encodeURIComponent(完整URL含query) + "_" + body + md5(str(unix_ms)) + "ooui")
全程纯 HTTP，不启动浏览器、不读写本地浏览器 cookie。

注：本渠道目前只暴露「登录」能力；生成能力见独立项目 E:/API管理工具/hailuoai。
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

from .base import (AccountSecret, Channel, ChannelSpec, CreditView, VerifyResult,
                   sniff_account_type, verify_from_exception)
from src.core.errors import redact

urllib3.disable_warnings()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36")
BASE = "https://hailuoai.video"


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _sign(full_path_with_query: str, body: str, ts_ms: int) -> str:
    """yy 签名（复刻前端拦截器）。"""
    enc = urllib.parse.quote(full_path_with_query, safe="")
    return _md5(enc + "_" + (body or "") + _md5(str(ts_ms)) + "ooui")


class HailuoChannel(Channel):
    """海螺 AI —— 手机号 + 短信登录。"""

    def __init__(self, spec: ChannelSpec, log=None):
        super().__init__(spec, log)
        self._device = {
            "device_platform": "web",
            "app_id": "3001",
            "version_code": "22203",
            "biz_id": "0",
            "lang": "en",
            "os_name": "Windows",
            "browser_name": "chrome",
            "browser_language": "en-US",
            "browser_platform": "Win32",
            "screen_width": 1920,
            "screen_height": 1080,
            "cpu_core_num": 8,
            "device_memory": 8,
        }

    # ---- 底层请求 -------------------------------------------------------
    def _post(self, path: str, body: Dict[str, Any], token: str = "") -> tuple:
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

    def _get(self, path: str, token: str = "", extra_params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """GET 请求（签名体与前端一致：GET 时 bodyToYY 为 "{}"）。"""
        ts = int(time.time() * 1000)
        params = dict(self._device)
        params["uuid"] = str(uuid.uuid4())
        params["unix"] = ts
        if extra_params:
            params.update({k: v for k, v in extra_params.items() if v is not None})
        full = path + "?" + urllib.parse.urlencode(params)
        headers = {
            "User-Agent": UA,
            "Accept": "application/json",
            "Origin": BASE,
            "Referer": BASE + "/",
            "yy": _sign(full, "{}", ts),   # GET 空体按 "{}" 参与签名
        }
        if token:
            headers["token"] = token
        resp = requests.get(BASE + full, headers=headers, timeout=30, verify=False)
        if resp.status_code == 401:
            raise RuntimeError("未登录或 cookie(_token) 失效 (HTTP 401)")
        try:
            payload = resp.json() if resp.content else {}
        except ValueError:
            raise RuntimeError("上游返回非 JSON: %s" % resp.text[:200])
        info = payload.get("statusInfo") or {}
        code = info.get("code", 0)
        if code:
            raise RuntimeError("海螺业务错误 %s: %s" % (code, info.get("message") or code))
        return payload.get("data") or {}

    # ---- 额度 -----------------------------------------------------------
    def _token_of(self, secret: AccountSecret) -> str:
        if not secret:
            return ""
        return str(secret.extra.get("token") or secret.cookies.get("_token") or "")

    # ---- 登录态校验 -----------------------------------------------------
    def verify(self, secret: AccountSecret) -> VerifyResult:
        """两段式：/v1/api/user/subscription（账号类型）→ 兜底 /v1/api/billing/credit。

        判定口径（保守，避免保活误杀）：
         * 无 token            -> invalid，**不发请求**
         * HTTP 401 / 未登录   -> invalid（凭据确实失效）
         * 网络 / 超时 / 限流  -> risk
         * 其它未知            -> risk
        """
        token = self._token_of(secret)
        if not token:
            return VerifyResult(ok=False, status="invalid",
                                message="缺少凭据：需要海螺 token（短信登录，或官网 Cookie 里的 _token）")
        # 第一段：subscription（顺带拿 free/会员）
        try:
            sub = self._get("/v1/api/user/subscription", token=token)
            if isinstance(sub, dict):
                atype = "free" if sub.get("isFree") else ("member" if sub.get("subscription") else "")
                # 再查 equity 拿额度数字（失败不影响校验结果）
                credit_txt = ""
                try:
                    eq = self._get("/v1/api/user/equity", token=token)
                    if isinstance(eq, dict) and eq.get("totalCredits") is not None:
                        credit_txt = "，贝壳额度 %s" % eq.get("totalCredits")
                except Exception:  # noqa: BLE001
                    pass
                return VerifyResult(ok=True, status="active", account_type=atype,
                                    message="登录态有效%s" % credit_txt,
                                    raw=redact(sub))
        except Exception as exc:  # noqa: BLE001
            s = str(exc)
            if "401" in s or "未登录" in s or "login" in s.lower() or "失效" in s:
                return VerifyResult(ok=False, status="invalid",
                                    message="登录态已失效：%s" % s)
            self._warn("海螺 verify(subscription) 失败，转 billing/credit 兜底: %s", exc)
        # 第二段兜底：billing/credit
        try:
            data = self._get("/v1/api/billing/credit", token=token)
        except Exception as exc:  # noqa: BLE001
            self._warn("海螺 verify 失败: %s", exc, exc_info=True)
            return verify_from_exception(exc, what="海螺")
        if not isinstance(data, dict) or not data:
            return VerifyResult(ok=False, status="risk",
                                message="海螺额度接口未返回数据，暂不判定失效")
        return VerifyResult(ok=True, status="active",
                            message="登录态有效",
                            account_type=sniff_account_type(data),
                            raw=redact(data) if isinstance(data, dict) else {})

    def credits(self, secret: AccountSecret) -> CreditView:
        """多接口聚合额度：equity（总览）+ subscription（free/付费）+ user/info（VIP）。"""
        token = self._token_of(secret)
        if not token:
            return CreditView(error="缺少凭据：需要 _token（短信登录或官网 Cookie 导入）")

        def num(x: Any) -> float:
            try:
                return float(x or 0)
            except Exception:  # noqa: BLE001
                return 0.0

        total = 0.0
        buckets: Dict[str, float] = {}
        notes: List[str] = []
        atype = ""

        # --- 权益接口：总览额度 + 会员等级 + 新用户试用 ---
        try:
            eq = self._get("/v1/api/user/equity", token=token)
            if isinstance(eq, dict):
                total = num(eq.get("totalCredits"))
                if eq.get("videoCost"):
                    buckets["videoCost"] = num(eq.get("videoCost"))
                if eq.get("queueLength"):
                    buckets["queueLength"] = num(eq.get("queueLength"))
                mn = str(eq.get("memberName") or "")
                if mn and mn.lower() not in ("free", ""):
                    notes.append("会员等级: %s" % mn)
                    atype = "member"
                elif mn.lower() == "free":
                    notes.append("会员等级: Free")
                    atype = "free" if not atype else atype
                if eq.get("isNewUser"):
                    notes.append("新用户")
                if eq.get("trialText"):
                    notes.append("试用: %s" % eq.get("trialText"))
                if eq.get("memberText"):
                    notes.append(str(eq.get("memberText")))
        except Exception as e:  # noqa: BLE001
            notes.append("equity 查询失败: %s" % str(e)[:60])

        # --- 订阅接口：确认 free / 付费 ---
        try:
            sub = self._get("/v1/api/user/subscription", token=token)
            if isinstance(sub, dict):
                if sub.get("isFree"):
                    atype = atype or "free"
                elif sub.get("subscription"):
                    atype = atype or "member"
                if not total:
                    total = num(sub.get("credit"))
        except Exception:  # noqa: BLE001
            pass

        # --- 用户信息接口：VIP 标识兜底 ---
        try:
            info = self._get("/v1/api/user/info", token=token)
            ui = (info or {}).get("userInfo") or {}
            vip = ui.get("vipInfo") or {}
            if num(vip.get("type")) > 0:
                atype = "vip"
            if ui.get("name"):
                notes.append("账号: %s" % ui.get("name"))
        except Exception:  # noqa: BLE001
            pass

        # --- 旧贝壳接口兜底（equity 拿不到数时）---
        if not total:
            try:
                cr = self._get("/v1/api/billing/credit", token=token)
                if isinstance(cr, dict):
                    total = num(cr.get("total_credit"))
                    for i, sub2 in enumerate(cr.get("sub_credits") or []):
                        if isinstance(sub2, dict):
                            buckets[str(sub2.get("type") or "sub_%d" % i)] = num(
                                sub2.get("credit") or sub2.get("amount"))
            except Exception:  # noqa: BLE001
                pass

        view = CreditView(total=total, allocatable=total, buckets=buckets,
                          account_type=atype,
                          message="; ".join(notes)[:250],
                          raw={"total": total, "account_type": atype})
        return view

    # ---- 海螺业务结构解析（statusInfo）----------------------------------
    # 常见错误码中文映射（实跑沉淀）
    _BIZ_ZH = {
        2400008: "贝壳余额不足，请充值或去官网参加活动获取",
        2400001: "参数错误或模型不存在",
        32: "短信发送过于频繁，请稍后再试",
        1200009: "验证码错误或已过期",
        1200013: "手机号格式无效",
    }

    @classmethod
    def _biz_msg(cls, j: Any) -> str:
        """提取 statusInfo 里的错误：debugInfo(msg=) > 中文映射 > message(英文)。"""
        try:
            info = (j or {}).get("statusInfo") or {}
            if not info:
                return ""
            code = info.get("code") or 0
            # debugInfo 形如 "biz error: code=32, msg=短信发送过于频繁，请稍后再试"
            debug = str(info.get("debugInfo") or "")
            if "msg=" in debug:
                return debug.split("msg=", 1)[1]
            if code in cls._BIZ_ZH:
                return cls._BIZ_ZH[code]
            msg = str(info.get("message") or "")
            if msg:
                zh = cls._BIZ_ZH.get(code)
                return ("%s（%s）" % (zh, msg)) if zh else msg
            return cls._BIZ_ZH.get(code) or ("业务错误码 %s" % code)
        except Exception:  # noqa: BLE001
            return ""

    def _biz_check(self, j: Any) -> None:
        """statusInfo.code != 0 -> 抛 RuntimeError（中文信息）。"""
        try:
            code = ((j or {}).get("statusInfo") or {}).get("code", 0) or 0
        except Exception:  # noqa: BLE001
            code = 0
        if code:
            raise RuntimeError(self._biz_msg(j) or ("业务错误码 %s" % code))

    def checkin(self, secret: AccountSecret) -> Dict[str, Any]:
        """海螺无传统每日签到（实测 99 个接口确认）。
        改为检查可领福利：月礼包资格（monthly_gift_eligible）+ 促销活动。"""
        token = self._token_of(secret)
        if not token:
            return {"ok": False, "message": "缺少凭据：需要海螺 token（请重新登录）"}
        # 1) 月礼包领取资格
        try:
            d = self._get("/v1/api/charge/monthly_gift_eligible", token=token)
            if isinstance(d, dict) and d.get("isEligible"):
                return {"ok": True,
                        "message": "检测到可领取的礼包资格，请到官网 hailuoai.video 个人中心领取",
                        "reward": "月卡礼包资格"}
        except RuntimeError as e:
            s = str(e)
            if "401" in s or "失效" in s:
                return {"ok": False, "message": "登录态已失效，请重新登录"}
            return {"ok": False, "message": "查询礼包资格失败：%s" % s}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": "查询礼包资格失败：%s" % e}
        # 2) 无每日签到
        return {"ok": False, "message": "海螺没有每日签到功能；贝壳通过充值或官方活动获取"}

    # ---- 在线登录 -------------------------------------------------------
    def login_methods(self) -> List[str]:
        return ["sms"]

    def login_send_sms(self, phone: str, area_code: str = "86") -> Dict[str, Any]:
        """发送登录短信验证码（会真实下发短信）。"""
        j, _ = self._post("/v1/api/user/login/sms/send",
                          {"phone": phone, "region": "CN"})
        self._biz_check(j)   # 不查的话"发送过于频繁"等错误会被吞成空 {}
        return j.get("data") or {}

    def login_by_sms(self, phone: str, code: str, area_code: str = "86") -> Optional[AccountSecret]:
        """手机号 + 验证码登录，返回 _token 凭据。"""
        j, resp = self._post("/v1/api/user/login/phone",
                             {"phone": phone, "code": str(code), "region": "CN"})
        self._biz_check(j)
        token = ""
        for c in resp.cookies:
            if c.name == "_token":
                token = c.value
        if not token:
            data = j.get("data") or {}
            token = data.get("token") or data.get("_token") or ""
        if not token:
            raise RuntimeError("登录失败：%s" % (self._biz_msg(j) or j))
        return AccountSecret(cookies={"_token": token}, oauth=None,
                             browser_state=None, extra={"token": token})

    # ---- 必需 abstract（本渠道只做登录，生成请用独立项目）----------------
    def build_headers(self, secret: AccountSecret, path: str, body: Any = None,
                      extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h = {"User-Agent": UA, "Content-Type": "application/json"}
        if secret and secret.extra.get("token"):
            h["token"] = secret.extra["token"]
        return h

    # ---- 生成（移植自 hailuoai 独立项目 generate_video/get_asset）---------
    # 上游状态: 1=生成中 11/12=排队 2=成功 10=申诉通过 3/5/7/14=失败/敏感
    _PENDING = (1, 11, 12)
    _DONE = (2, 10)

    def submit(self, secret: AccountSecret, req: Any) -> Any:
        """POST /v2/api/multimodal/generate/video -> data.id（轮询用）。"""
        from .base import SubmitResult
        token = self._token_of(secret)
        if not token:
            raise RuntimeError("缺少凭据：需要海螺 token（短信登录获取）")
        body = {
            "quantity": 1,
            "parameter": {
                "modelID": str(req.model or "23000"),
                "desc": req.prompt,
                "fileList": [],
                "useOriginPrompt": True,
                "resolution": "720",
                "duration": int(req.duration or 6),
                "aspectRatio": req.ratio or "16:9",
            },
            "projectID": "0",
        }
        j, _ = self._post("/v2/api/multimodal/generate/video", body, token=token)
        self._biz_check(j)
        data = j.get("data") or {}
        vid = data.get("id") or data.get("videoID") or j.get("id")
        if not vid:
            raise RuntimeError("提交成功但响应里没有 id：%s" % str(j)[:300])
        return SubmitResult(upstream_task_id=str(vid), transport="requests", raw={"channel": "hailuo"})

    def poll(self, secret: AccountSecret, upstream_task_id: str,
             req: Optional["GenerateRequest"] = None, **kw: Any) -> Any:
        """GET /public/v3/api/multimodal/video/detail?id=<videoID>。"""
        from .base import PollResult
        token = self._token_of(secret)
        try:
            data = self._get("/public/v3/api/multimodal/video/detail",
                             token=token, extra_params={"id": str(upstream_task_id)})
        except RuntimeError as e:
            s = str(e)
            if "401" in s or "失效" in s:
                return PollResult(status="failed", error=s, error_class="auth_or_risk",
                                  raw={"channel": "hailuo"})
            return PollResult(status="processing", raw={"channel": "hailuo", "poll_error": s[:200]})
        video = data.get("video") or data or {}
        asset = video.get("videoAsset") or {}
        status = asset.get("status")
        urls = asset.get("videoURLs") or {}
        url = asset.get("videoURL") or urls.get("feedURL") or ""
        download = (asset.get("downloadURL") or urls.get("downloadURLWithoutWatermark")
                    or urls.get("downloadURLWithWatermark") or url)
        if status in self._PENDING:
            return PollResult(status="processing", raw={"channel": "hailuo", "upstream_status": status})
        if status in self._DONE:
            if not download:
                return PollResult(status="failed", error="生成完成但未拿到视频地址",
                                  raw={"channel": "hailuo", "asset": str(asset)[:300]})
            return PollResult(status="success", media=[download],
                              covers=[asset.get("coverURL") or ""],
                              raw={"channel": "hailuo", "upstream_status": status})
        return PollResult(status="failed",
                          error="上游状态 %s（3=失败 5=敏感 7=拒绝 14=敏感失败）" % status,
                          raw={"channel": "hailuo", "upstream_status": status})
