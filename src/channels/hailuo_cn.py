# -*- coding: utf-8 -*-
"""海螺 AI 国内版渠道（hailuoai.com，MiniMax 海螺）。

逆向自 hailuoai.com 前端 JS（Next.js chunks，webpack 未混淆）：
    POST /v1/api/user/login/sms/send   {phone, region}          -> 下发短信
    POST /v1/api/user/login/phone      {phone, code, region}    -> 登录态
    POST /v1/api/user/login/email      {email, password}        -> 邮箱登录（端点确认，字段未实测）
    POST /v2/api/multimodal/generate/video                        -> 视频生成提交
    POST /api/feed/creation/my                                    -> 作品列表（轮询用）
    GET  /v1/api/user/info                                        -> 会话校验

实测结论（无账号盲测）：
  * 国内版与海外版 hailuoai.video **同一套 yy 签名**（盐 ooui）——已实测：
    携带签名打 /v1/api/user/login/sms/send 返回业务层错误 1200013「请输入正确的手机号」，
    证明签名通过、进入业务校验层。
  * 响应包裹 {statusInfo:{code,message}, data}，与海外版 {status,message} 不同。
全程纯 HTTP，不启动浏览器。

注：海外版登录见 channels/hailuo.py（hailuoai.video）。
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

from .base import (AccountSecret, Channel, ChannelSpec, CreditView,
                   GenerateRequest, PollResult, SubmitResult, VerifyResult,
                   looks_login_expired, sniff_account_type, verify_from_exception)

urllib3.disable_warnings()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36")
BASE = "https://hailuoai.com"


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _sign(full_path_with_query: str, body: str, ts_ms: int) -> str:
    """yy 签名（与海外版 hailuoai.video 完全一致，实测通过）。"""
    enc = urllib.parse.quote(full_path_with_query, safe="")
    return _md5(enc + "_" + (body or "") + _md5(str(ts_ms)) + "ooui")


def _unwrap(payload: Dict[str, Any]) -> Dict[str, Any]:
    """国内版响应包裹：{statusInfo:{code,message,...}, data:{...}}。"""
    si = payload.get("statusInfo") or {}
    code = si.get("code", payload.get("status", 0))
    # 成功码：statusInfo.code 常见为 1（0/None 兼容海外风格）；非成功即抛
    if code not in (0, 1, None) and si.get("message"):
        raise RuntimeError("hailuo_cn code=%s: %s" % (code, si.get("message")))
    return payload.get("data") or {}


class HailuoCnChannel(Channel):
    """海螺 AI 国内版（hailuoai.com）—— 手机号短信登录 + 视频生成。"""

    def __init__(self, spec: ChannelSpec, log=None):
        super().__init__(spec, log)
        self._device = {
            "device_platform": "web",
            "app_id": "3001",
            "version_code": "22203",
            "biz_id": "0",
            "lang": "zh-Hans",
            "os_name": "Windows",
            "browser_name": "chrome",
            "browser_language": "zh-CN",
            "browser_platform": "Win32",
            "screen_width": 1920,
            "screen_height": 1080,
            "cpu_core_num": 8,
            "device_memory": 8,
        }

    # ---- 底层请求 -------------------------------------------------------
    def _post(self, path: str, body: Dict[str, Any],
              token: str = "") -> tuple:
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

    def _probe(self, path: str, token: str = "") -> tuple:
        """带 HTTP 状态码的 GET。

        verify 必须能区分「401 未登录」与「接口返回空 / 上游异常」——后者不能
        判 invalid（会误杀账号），所以状态码不能丢。
        """
        ts = int(time.time() * 1000)
        params = dict(self._device)
        params["uuid"] = str(uuid.uuid4())
        params["unix"] = ts
        full = path + "?" + urllib.parse.urlencode(params)
        headers = {
            "User-Agent": UA,
            "Origin": BASE,
            "Referer": BASE + "/",
            "yy": _sign(full, "", ts),
        }
        if token:
            headers["token"] = token
        resp = requests.get(BASE + full, headers=headers, timeout=30, verify=False)
        try:
            payload = resp.json() if resp.content else {}
        except ValueError:
            payload = {"_raw": resp.text[:300]}
        return resp.status_code, payload

    def checkin(self, secret: AccountSecret) -> Dict[str, Any]:
        """签到/每日奖励探测。统一契约：{"ok", "message", "reward"}。"""

        tok = self._tok(secret)
        if not tok:
            return {"ok": False, "message": "缺少凭据：需要海螺国内版 token（请重新登录）"}
        try:
            d = self._get("/v1/api/charge/monthly_gift_eligible", token=tok)
            if isinstance(d, dict) and d.get("isEligible"):
                return {"ok": True, "message": "检测到可领取的礼包资格，请到官网个人中心领取",
                        "reward": "月卡礼包资格"}
            return {"ok": False, "message": "海螺（国内版）没有每日签到功能；贝壳通过充值或官方活动获取"}
        except Exception as exc:  # noqa: BLE001
            s = str(exc)
            if "401" in s or "失效" in s or "login" in s.lower():
                return {"ok": False, "message": "登录态已失效，请重新登录"}
            return {"ok": False, "message": "查询礼包资格失败（网络异常）：%s" % s[:100]}

    def _get(self, path: str, token: str = "") -> Dict[str, Any]:
        """GET 请求（保留原语义：只回 payload）。"""
        return self._probe(path, token)[1]

    # ---- 海螺国内版业务结构解析（statusInfo）------------------------------
    # 常见错误码中文映射（实测沉淀，hailuoai.com）
    _BIZ_ZH = {
        32: "短信发送过于频繁，请稍后再试",
        1200009: "验证码错误或已过期",
        1200013: "手机号格式无效，请输入正确的手机号",
        1200048: "当前国家或地区暂未支持短信",
    }

    @classmethod
    def _biz_msg(cls, j: Any) -> str:
        """提取 statusInfo 里的错误：debugInfo(msg=) > 中文映射 > message。"""
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
            if msg and msg not in ("成功", "success"):
                zh = cls._BIZ_ZH.get(code)
                return ("%s（%s）" % (zh, msg)) if zh else msg
            return cls._BIZ_ZH.get(code) or ""
        except Exception:  # noqa: BLE001
            return ""

    def _biz_check(self, j: Any) -> None:
        """statusInfo.code 非成功（0/1/None）-> 抛 RuntimeError（中文信息）。

        国内版实测：成功 code=0（message="成功"）；手机号非法 code=1200013；
        地区不支持 code=1200048；短信频繁 code=32。
        """
        try:
            code = ((j or {}).get("statusInfo") or {}).get("code", 0)
        except Exception:  # noqa: BLE001
            code = 0
        if code not in (0, 1, None):
            raise RuntimeError(self._biz_msg(j) or ("业务错误码 %s" % code))

    # ---- 在线登录 -------------------------------------------------------
    def login_methods(self) -> List[str]:
        return ["sms", "pwd"]

    def login_send_sms(self, phone: str, area_code: str = "86") -> Dict[str, Any]:
        """发送登录短信验证码（会真实下发短信）。

        端点/字段实测：POST /v1/api/user/login/sms/send body {"phone","region":"CN"}。
        region 必须为 "CN"，否则按服务器地理位置判定为非 CN 区 -> 1200048。
        不查 statusInfo 的话「发送过于频繁」等错误会被吞成空 {}，故先 _biz_check。
        """
        j, _ = self._post("/v1/api/user/login/sms/send",
                          {"phone": phone, "region": "CN"})
        self._biz_check(j)   # 1200013 / 1200048 / 32 等业务错误在此抛出
        return _unwrap(j)

    def login_by_sms(self, phone: str, code: str, area_code: str = "86") -> Optional[AccountSecret]:
        """手机号 + 验证码登录。国内版登录态同样走 token（响应 data / cookie）。"""
        j, resp = self._post("/v1/api/user/login/phone",
                             {"phone": phone, "code": str(code), "region": "CN"})
        self._biz_check(j)   # 1200009 验证码错误等在此抛出
        data = _unwrap(j)
        token = ""
        for c in resp.cookies:
            if c.name in ("_token", "token"):
                token = c.value
        if not token:
            token = (data.get("token") or data.get("_token") or
                     data.get("access_token") or "") if isinstance(data, dict) else ""
        if not token:
            raise RuntimeError("登录成功但未提取到 token: %s" % (data or j))
        return AccountSecret(cookies={"_token": token}, oauth=None,
                             browser_state=None, extra={"token": token})

    def login_by_password(self, account: str, password: str) -> Optional[AccountSecret]:
        """邮箱账密登录（/v1/api/user/login/email，端点已逆向确认）。"""
        j, resp = self._post("/v1/api/user/login/email",
                             {"email": account, "password": password})
        data = _unwrap(j)
        token = ""
        for c in resp.cookies:
            if c.name in ("_token", "token"):
                token = c.value
        if not token and isinstance(data, dict):
            token = data.get("token") or data.get("_token") or ""
        if not token:
            raise RuntimeError("登录成功但未提取到 token: %s" % (data or j))
        return AccountSecret(cookies={"_token": token}, oauth=None,
                             browser_state=None, extra={"token": token})

    # ---- 会话 / 额度 ----------------------------------------------------
    def build_headers(self, secret: AccountSecret, path: str, body: Any = None,
                      extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h = {"User-Agent": UA, "Content-Type": "application/json"}
        if secret and secret.extra.get("token"):
            h["token"] = secret.extra["token"]
        h.update(extra or {})
        return h

    def credits(self, secret: AccountSecret) -> CreditView:
        try:
            # 实测（2026-10-16）：国内版权益是 **GET** /v1/api/user/equity（POST 404），
            # 数据在 data.totalCredits（新用户 80 积分），数据外层是 data 不是裸 dict。
            # 注意 _get 返回单值 dict（不是 tuple），不能解包成 j, _ —— 那会把 dict
            # 按 key 拆成字符串（credits 一直返回 0 的根因）。
            j = self._get("/v1/api/user/equity", token=self._tok(secret))
            data = (j or {}).get("data") if isinstance(j, dict) else None
            data = _unwrap(data) if not isinstance(data, dict) else data
        except Exception as exc:  # noqa: BLE001
            return CreditView(error=str(exc))
        total = 0.0
        if isinstance(data, dict):
            v = data.get("totalCredits")
            if isinstance(v, (int, float)):
                total = float(v)
            else:
                for k, v2 in data.items():
                    if isinstance(v2, (int, float)) and "credit" in str(k).lower():
                        total = max(total, float(v2))
        return CreditView(total=total, allocatable=total, raw=data if isinstance(data, dict) else {})

    @staticmethod
    def _tok(secret: Optional[AccountSecret]) -> str:
        if not secret:
            return ""
        return str(secret.extra.get("token") or secret.cookies.get("_token") or "")

    def verify(self, secret: AccountSecret) -> VerifyResult:
        """真实探测登录态：先打额度接口（与海外版同源），失败再退到用户信息接口。

        实测（假 token）：hailuoai.com 对两个端点一律回 **HTTP 401 + 空 body**，
        故 401 -> invalid。判定口径与海外版一致：
          无 token -> invalid（不发请求）；401/403/明确未登录 -> invalid；
          5xx/429/网络异常/空数据 -> risk（不误杀）；拿到 data -> active。
        """
        token = self._tok(secret)
        if not token:
            return VerifyResult(ok=False, status="invalid",
                                message="缺少凭据：需要海螺国内版 token（短信登录，或官网 Cookie 里的 _token）")
        last: Optional[BaseException] = None
        for path, label in (("/v1/api/billing/credit", "额度接口"),
                            ("/v1/api/user/info", "用户信息接口")):
            try:
                status, payload = self._probe(path, token=token)
            except Exception as exc:  # noqa: BLE001
                self._warn("海螺国内版 verify（%s）异常: %s", label, exc, exc_info=True)
                if looks_login_expired(str(exc)):
                    return VerifyResult(ok=False, status="invalid",
                                        message="海螺国内版登录态已失效，请重新登录导入凭据",
                                        raw={"probe": path})
                last = exc
                continue
            if status in (401, 403) or looks_login_expired(payload):
                return VerifyResult(ok=False, status="invalid",
                                    message="海螺国内版登录态已失效，请重新登录导入凭据",
                                    raw={"probe": path, "http_status": status})
            data = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(data, dict) and data:
                self._info("海螺国内版 verify 通过（%s）", label)
                return VerifyResult(ok=True, status="active",
                                    message=f"登录态有效（{label}返回正常数据）",
                                    account_type=sniff_account_type(payload),
                                    raw=payload)
            # 非凭据问题的失败：记住后换下一个探针，都不行就判 risk
            last = RuntimeError(f"{label} HTTP {status}，无有效数据")
        return verify_from_exception(last or RuntimeError("两个探针均无数据"),
                                     what="海螺国内版")

    # ---- 生成 -----------------------------------------------------------
    def submit(self, secret: AccountSecret, req: GenerateRequest) -> SubmitResult:
        """POST /v2/api/multimodal/generate/video。

        payload 为 2026-10-16 playwright UI 实抓（真实生成成功，扣 60 积分）：
        {quantity, parameter:{modelID(字符串名 hailuo3.0-t2v), desc, fileList,
         useOriginPrompt, resolution("1440"/"720" 字符串像素高), duration(秒数 int),
         aspectRatio("21:9"/"16:9" 字符串), referenceMode("all-reference")},
         videoExtra:{promptStruct(富文本 JSON 字符串，必填)}, projectID:"0"}
        数字 ID（23000 等）仅存在于 equity.modelControl 展示，服务端只认字符串名。
        """
        prompt = req.prompt or ""
        n = len(prompt)
        prompt_struct = json.dumps(
            {"value": [{"type": "paragraph", "children": [{"text": prompt}]}],
             "length": n, "plainLength": n, "rawLength": n},
            ensure_ascii=False, separators=(",", ":"))
        # 参数取值来自 /public/v2/api/multimodal/video/model/info（实拉 26KB 清单）：
        #   resolution 为字符串像素高（23000: "720"；23218: "768"/"1080"；hailuo3.0-t2v: "768"/"1440"）
        #   duration 为数字秒（23000: 6；hailuo3.0-t2v: 4-15）
        #   aspectRatio 仅 hailuo3.0-* 支持（"Auto"/"21:9"/"16:9"/"4:3"），老模型传空串
        res_map = {"768p": "768", "720p": "720", "1080p": "1080", "1440p": "1440", "480p": "480"}
        res = res_map.get(str(req.resolution or "").lower(), "720")
        model = req.model or "hailuo3.0-t2v"
        ar = {"16:9": "16:9", "9:16": "16:9", "1:1": "16:9", "4:3": "4:3",
              "21:9": "21:9", "auto": "Auto"}.get(str(req.ratio or "16:9").lower(), "16:9")
        if model.startswith("hailuo3.0") or model.startswith("hailuo_h3"):
            ar_val = ar
        else:
            ar_val = ""   # 老数字模型 aspectRatios 为空
        payload: Dict[str, Any] = {
            "quantity": 1,
            "parameter": {
                "modelID": model,
                "desc": prompt,
                "fileList": [],
                "useOriginPrompt": False,
                "resolution": res,
                "duration": int(req.duration or 6),
                "aspectRatio": ar_val,
                "referenceMode": "all-reference",
            },
            "videoExtra": {"promptStruct": prompt_struct},
            "projectID": "0",
        }
        if req.mode == "i2v" and req.images:
            payload["parameter"]["fileList"] = [{"uri": req.images[0]}]
        payload["parameter"].update(req.params or {})
        j, _ = self._post("/v2/api/multimodal/generate/video", payload,
                          token=self._tok(secret))
        si = (j or {}).get("statusInfo") or {}
        code = si.get("code")
        if code not in (0, None):
            return SubmitResult(accepted=False,
                                error="hailuo_cn code=%s: %s" % (code, si.get("message")), raw=j)
        data = _unwrap(j if isinstance(j, dict) else {})
        task_id = ""
        if isinstance(data, dict):
            for key in ("sl_id", "video_id", "taskId", "task_id", "id", "batchID"):
                if data.get(key):
                    task_id = str(data[key])
                    break
        if not task_id:
            return SubmitResult(accepted=False, error="未提取到任务 ID: %s" % data, raw=j)
        return SubmitResult(upstream_task_id=task_id, transport="requests", raw=j)

    def poll(self, secret: AccountSecret, upstream_task_id: str,
             req: Optional[GenerateRequest] = None) -> PollResult:
        """双接口轮询（2026-10-16 playwright 实测）：

        - /api/feed/creation/my/processing → 进行中（commonInfo.status/percent）
        - /api/feed/creation/my → 完成作品（feeds[].commonInfo + 视频字段）
        任务 ID = commonInfo.id（UI 提交实测 567479785221230599, percent 99）。
        """
        tid = str(upstream_task_id)

        # 进行中？
        try:
            d = self._post("/api/feed/creation/my/processing", {"limit": 10},
                           token=self._tok(secret))[0]
            data = _unwrap(d if isinstance(d, dict) else {})
            for batch in (data or {}).get("batchFeeds") or []:
                for feed in batch.get("feeds") or []:
                    ci = feed.get("commonInfo") or {}
                    if str(ci.get("id")) == tid or str(batch.get("batchID")) == tid:
                        pct = float(ci.get("percent") or 0)
                        return PollResult(status="processing", progress=pct,
                                          raw={"commonInfo": ci})
        except Exception:  # noqa: BLE001
            pass

        # 完成作品列表
        try:
            d = self._post("/api/feed/creation/my", {"limit": 20},
                           token=self._tok(secret))[0]
            data = _unwrap(d if isinstance(d, dict) else {})
            for feed in (data or {}).get("feeds") or []:
                ci = feed.get("commonInfo") or {}
                fid = str(ci.get("id") or "")
                if fid != tid and str(ci.get("batchID") or "") != tid:
                    continue
                status = int(ci.get("status") or 0)
                cover = (feed.get("feedCoverInfo") or {}).get("coverURL") or ""
                # 实测（2026-10-16 UI 生成成功）：status=2 为完成；
                # 视频在 metaInfo.videoMetaInfo.mediaInfo.url（无水印原片），
                # downloadURL.{watermarkURL,aiWatermarkURL,hailuoWatermarkURL} 为水印版。
                mi = ((feed.get("metaInfo") or {}).get("videoMetaInfo") or {}).get("mediaInfo") or {}
                media = [u for u in (mi.get("url"),) if isinstance(u, str) and u.startswith("http")]
                if status == 2 and media:
                    return PollResult(status="success", media=media,
                                      covers=[cover] if cover else [], raw=feed)
                if status in (3, 4) or str(ci.get("humanCheckStatus")) in ("2", "3"):
                    return PollResult(status="failed", error="status=%s" % status, raw=feed)
                if status == 2 and not media:
                    # 完成但 mediaInfo 未就绪（极少见），下一轮再取
                    return PollResult(status="processing", progress=99.0, raw=feed)
                return PollResult(status="processing",
                                  progress=float(ci.get("percent") or 0), raw=feed)
        except Exception as exc:  # noqa: BLE001
            return PollResult(status="processing", raw={"err": str(exc)[:100]})
        return PollResult(status="processing", raw={"found": False})


