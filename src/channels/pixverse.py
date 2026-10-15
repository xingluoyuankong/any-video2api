# -*- coding: utf-8 -*-
"""PixVerse 渠道（拍我 AI / pai.video，PixVerse 国内版）。

逆向自 pai.video 前端 JS（vendor-CLdaMCPH.js，rolldown 打包，无混淆）：
    API 基址  https://cn-app-api.pixverseai.cn/creative_platform
    登录      POST /account/getPhoneCode          {phone}                    -> 下发短信
              POST /account/loginPhoneWithCode    {phone, code}              -> Resp.Result.Token
              POST /auth/phone/password/login     {phone, password}          -> Token
              POST /login                         {username, password}       -> 邮箱登录（Go validator 实测确认字段）
    生成      POST /video/t2v                     {prompt, model, quality, aspect_ratio, duration, ...}
              POST /video/i2v                     {prompt, customer_img_path(s), ...}
              GET  /task/list?page_num=&page_size=                            -> 任务列表轮询
    额度      GET  /user/credits
    会话      GET  /getUserInfo                                                -> 验证登录态

实测结论（2025 无账号盲测）：
  * 全程无签名、无风控头 —— 纯 JSON + Cookie 即可；错误体为 {ErrCode, ErrMsg, Resp}，ErrCode=0 成功。
  * getPhoneCode / loginPhoneWithCode 已实测打通到字段校验层：phone 字段确认为 11 位大陆手机号
    （13800138000 返回「手机号格式无效」，格式校验服务端做）；loginPhoneWithCode 携带
    {phone, code} 到达验证码校验层（500113 验证码已失效）—— 协议通路完整。
  * 鉴权：登录成功后 Resp.Result.Token，前端写入 Cookie（token），后续请求带 Cookie 即可；
    实现为 cookie + header 双带。
  * 任务状态枚举（前端 ZE）：1 正常(完成) 5 等待 7 未过审 8 失败 9 排队 10 生成中。
全程纯 HTTP，不启动浏览器。
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional

import requests
import urllib3

from .base import (AccountSecret, Channel, ChannelSpec, CreditView,
                   GenerateRequest, PollResult, SubmitResult, norm_status)

urllib3.disable_warnings()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36")
SITE = "https://pai.video"
BASE = "https://cn-app-api.pixverseai.cn/creative_platform"

# 前端模型枚举（vendor-CLdaMCPH.js 视频模型段）完整提取
VIDEO_MODELS: Dict[str, str] = {
    "v6": "v6", "c1": "pixverse-c1", "v5.6": "v5.6", "v5.5": "v5.5",
    "v5-fast": "v5-fast", "v5": "v5", "v4.5": "v4.5", "v4": "v4",
    "v3.5": "v3.5", "v3": "v3", "v2": "v2", "v1": "v1",
    "sora-2": "sora-2", "sora-2-pro": "sora-2-pro",
    "veo-3.1-lite": "veo-3.1-lite", "veo-3.1-standard": "veo-3.1-standard",
    "veo-3.1-fast": "veo-3.1-fast",
    "grok-imagine": "grok-imagine", "grok-imagine-1.5": "grok-imagine-1.5",
    "seedance-2.0-standard": "seedance-2.0-standard",
    "seedance-2.0-fast": "seedance-2.0-fast",
    "seedance-2.0-mini": "seedance-2.0-mini", "seedance-2.5": "seedance-2.5",
    "gemini-omni-flash": "gemini-omni-flash",
    "kling-o3-standard": "kling-o3-standard", "kling-o3-pro": "kling-o3-pro",
    "kling-o3-4k": "kling-o3-4k",
    "kling-3.0-standard": "kling-3.0-standard", "kling-3.0-pro": "kling-3.0-pro",
    "kling-3.0-4k": "kling-3.0-4k",
    "happyhorse-1.0": "happyhorse-1.0",
    "minimax-h3": "minimax-h3", "minimax-h3-max": "minimax-h3-max",
    "wan-3.0": "wan-3.0",
}

#: 前端任务状态枚举 -> 归一化
_TASK_STATUS_MAP = {
    1: "success", 2: "success",           # NORMAL / 发布
    5: "queued", 9: "queued",             # WAITING / QUEUE
    10: "processing",
    7: "failed", 8: "failed", 11: "failed",
    12: "processing", 13: "failed", 17: "failed",
}


class PixverseChannel(Channel):
    """PixVerse（pai.video 拍我 AI）—— 手机号短信 / 账密 + t2v·i2v 生成。"""

    def __init__(self, spec: ChannelSpec, log=None):
        super().__init__(spec, log)
        self._device_id = uuid.uuid4().hex

    # ---- 底层请求 -------------------------------------------------------
    def _base_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": UA,
            "Content-Type": "application/json",
            "Accept-Language": "zh-CN",
            "Origin": SITE,
            "Referer": SITE + "/",
            "Workspace-Id": "0",
            "X-Device-ID": self._device_id,
            "Ai-Trace-Id": uuid.uuid4().hex,
        }

    def _handle(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """统一取 ErrCode/ErrMsg；非 0 抛业务异常。"""
        code = payload.get("ErrCode", 0)
        if code not in (0, None):
            raise RuntimeError("pixverse ErrCode=%s: %s" % (code, payload.get("ErrMsg") or payload))
        return payload.get("Resp") or {}

    def _post(self, path: str, body: Dict[str, Any],
              secret: Optional[AccountSecret] = None) -> Dict[str, Any]:
        headers = self._base_headers()
        if secret is not None:
            headers.update(self.build_headers(secret, path, body))
        r = requests.post(BASE + path, data=json.dumps(body, ensure_ascii=False),
                          headers=headers, timeout=30, verify=False)
        try:
            payload = r.json()
        except ValueError:
            raise RuntimeError("pixverse 非 JSON 响应 %s: %s" % (r.status_code, r.text[:200]))
        return payload

    def checkin(self, secret: AccountSecret) -> Dict[str, Any]:
        """每日签到（实测接口，2026-10-16 打通）。

        逆向自 vendor chunk：
            tI = e => U.get(`/task/list`, e)
            nI = e => U.post(`/task/finish`, e)
            handleFinishTask: async e => {
                let n = taskList.find(t => t.sub_type === e);
                if (!n || n.progress !== UNFINISHED) return;
                await nI({ sub_type: e });
            }
        枚举：UNFINISHED=1 / FINISHED=2 / CLAIMED=3 / SHARE_VIDEO=4
        任务项字段：{name, platform, task_type, sub_type, reward, progress, ...}
        → 提交体是 {"sub_type": <数字>}，且只提交 progress==1 的项。

        统一契约：{"ok", "message", "reward"}。
        """
        try:
            tasks = self._get("/task/list", secret=secret)
        except Exception as exc:  # noqa: BLE001
            s = str(exc)
            if "10003" in s or "not login" in s.lower():
                return {"ok": False, "message": "登录态已失效，请重新登录"}
            return {"ok": False, "message": "查询任务列表失败：%s" % s[:100]}

        resp = tasks.get("Resp") if isinstance(tasks, dict) else None
        items: List[Dict[str, Any]] = []
        if isinstance(resp, dict):
            for key in ("TaskList", "task_list", "tasks", "List", "list"):
                v = resp.get(key)
                if isinstance(v, list):
                    items = v
                    break
        elif isinstance(resp, list):
            items = resp
        if not items:
            return {"ok": False, "message": "未取到任务列表：%s" % str(tasks)[:150],
                    "raw": tasks}

        UNFINISHED, FINISHED = 1, 2

        def pick(items_list):
            out = []
            for it in items_list:
                if isinstance(it, dict) and it.get("sub_type") is not None:
                    out.append(it)
            return out

        all_tasks = pick(items)
        total = 0
        steps: List[str] = []

        # 阶段一：未完成(1) → /task/finish
        for it in all_tasks:
            if it.get("progress") != UNFINISHED:
                continue
            sub = it["sub_type"]
            try:
                res = self._post("/task/finish", {"sub_type": sub}, secret)
                code = res.get("ErrCode") if isinstance(res, dict) else None
                if code in (0, None):
                    steps.append("finish(sub_type=%s) ok" % sub)
                else:
                    steps.append("finish(sub_type=%s) %s"
                                 % (sub, res.get("ErrMsg") if isinstance(res, dict) else res))
            except Exception as exc:  # noqa: BLE001
                steps.append("finish(sub_type=%s) 异常 %s" % (sub, str(exc)[:50]))

        # 阶段二：已完成待领取(2) → /reward/claim（领取才真正到账）
        try:
            after = self._get("/task/list", secret=secret)
            resp2 = after.get("Resp") if isinstance(after, dict) else None
            items2 = []
            if isinstance(resp2, dict):
                for key in ("TaskList", "task_list", "tasks", "List", "list"):
                    if isinstance(resp2.get(key), list):
                        items2 = resp2[key]
                        break
            elif isinstance(resp2, list):
                items2 = resp2
            for it in pick(items2):
                if it.get("progress") != FINISHED:
                    continue
                sub = it["sub_type"]
                try:
                    res = self._post("/reward/claim", {"sub_type": sub}, secret)
                    code = res.get("ErrCode") if isinstance(res, dict) else None
                    if code in (0, None):
                        rw = it.get("reward")
                        total += int(rw) if isinstance(rw, (int, float)) else 0
                        steps.append("claim(sub_type=%s) +%s" % (sub, rw))
                    else:
                        steps.append("claim(sub_type=%s) %s"
                                     % (sub, res.get("ErrMsg") if isinstance(res, dict) else res))
                except Exception as exc:  # noqa: BLE001
                    steps.append("claim(sub_type=%s) 异常 %s" % (sub, str(exc)[:50]))
        except Exception as exc:  # noqa: BLE001
            steps.append("复查任务列表失败: %s" % str(exc)[:60])

        if not steps:
            return {"ok": True, "message": "今日无可签到任务（可能已完成）",
                    "reward": 0, "tasks": len(all_tasks)}
        ok = any(s.startswith("claim(") and "+" in s for s in steps) or \
             any("ok" in s for s in steps)
        return {"ok": ok, "message": "; ".join(steps)[:200], "reward": total,
                "steps": steps}

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None,
             secret: Optional[AccountSecret] = None) -> Dict[str, Any]:
        headers = self._base_headers()
        if secret is not None:
            headers.update(self.build_headers(secret, path))
        r = requests.get(BASE + path, params=params, headers=headers,
                         timeout=30, verify=False)
        try:
            return r.json()
        except ValueError:
            raise RuntimeError("pixverse 非 JSON 响应 %s: %s" % (r.status_code, r.text[:200]))

    # ---- 在线登录 -------------------------------------------------------
    def login_methods(self) -> List[str]:
        # oauth = 浏览器登录后回传 cookie（pixverse 登录有网易易盾，纯协议不可行，
        # 因此实际反代就是靠这条「官网登录 → 书签回传 cookie」链路）
        return ["sms", "pwd", "oauth"]

    def login_send_sms(self, phone: str, area_code: str = "86") -> Dict[str, Any]:
        """发送登录短信验证码（会真实下发短信）。国内版仅支持 11 位大陆手机号。

        实测实锤（登录态多轮试错）：
          * {"phone"}                            -> 400002 手机号格式无效
          * {"phoneNumber"}                      -> 400008 验证字段不能为空
          * {"phone","phoneNumber"}              -> 400008 仍缺必填
          * {"phone","phoneNumber","type":1}     -> 400008
          * {"phone","phoneNumber","type":"login"} -> 500120 通过校验进入发送（限流）
        → 正确 body：phone + phoneNumber + type:"login"
        """
        if str(area_code) not in ("86", "+86", ""):
            raise RuntimeError("PixVerse 国内版（pai.video）仅支持大陆手机号")
        payload = self._post("/account/getPhoneCode",
                            {"phone": phone, "phoneNumber": phone, "type": "login"})
        return self._handle(payload)

    def login_by_sms(self, phone: str, code: str, area_code: str = "86") -> Optional[AccountSecret]:
        """手机号 + 验证码登录 -> Token。"""
        payload = self._post("/account/loginPhoneWithCode",
                             {"phone": phone, "phoneNumber": phone,
                              "code": str(code), "type": "login"})
        resp = self._handle(payload)
        result = resp.get("Result") or {}
        token = result.get("Token") or ""
        if not token:
            raise RuntimeError("登录成功但未返回 Token: %s" % (result or resp))
        cookies = {"token": token}
        return AccountSecret(
            cookies=cookies, oauth=None, browser_state=None,
            extra={"token": token, "account_id": result.get("AccountId"),
                   "username": result.get("Username"), "login_type": "sms"},
        )

    def login_by_password(self, account: str, password: str) -> Optional[AccountSecret]:
        """账密登录：含 @ 走邮箱 /login，否则走手机号 /auth/phone/password/login。"""
        if "@" in account:
            payload = self._post("/login", {"username": account, "password": password})
        else:
            payload = self._post("/auth/phone/password/login",
                                 {"phone": account, "password": password})
        resp = self._handle(payload)
        result = resp.get("Result") or {}
        token = result.get("Token") or ""
        if not token:
            raise RuntimeError("登录成功但未返回 Token: %s" % (result or resp))
        cookies = {"token": token}
        return AccountSecret(
            cookies=cookies, oauth=None, browser_state=None,
            extra={"token": token, "account_id": result.get("AccountId"),
                   "username": result.get("Username"), "login_type": "pwd"},
        )

    # ---- 会话 / 额度 ----------------------------------------------------
    def verify(self, secret: AccountSecret):
        from .base import VerifyResult
        try:
            payload = self._get("/getUserInfo", secret=secret)
        except Exception as exc:  # noqa: BLE001
            # 网络/限流是临时问题 -> risk（不能返回 invalid，否则保活会误杀账号）
            return VerifyResult(ok=False, status="risk",
                                message="网络或上游异常，暂不判定失效：%s" % exc)
        if payload.get("ErrCode") == 0:
            return VerifyResult(ok=True, status="active", raw=payload)
        if payload.get("ErrCode") == 10003:
            return VerifyResult(ok=False, status="invalid", message="未登录，请重新登录")
        return VerifyResult(ok=False, status="unknown", raw=payload)

    def credits(self, secret: AccountSecret) -> CreditView:
        try:
            payload = self._get("/user/credits", secret=secret)
            resp = self._handle(payload)
        except Exception as exc:  # noqa: BLE001
            return CreditView(error=str(exc))
        # Resp 结构未知（需登录态实测），做防御性展开
        buckets: Dict[str, float] = {}
        total = 0.0
        def _walk(node: Any, prefix: str = "") -> None:
            nonlocal total
            if isinstance(node, dict):
                for k, v in node.items():
                    if isinstance(v, (int, float)) and any(
                            w in k.lower() for w in ("credit", "balance", "point")):
                        buckets[prefix + k] = float(v)
                        total = max(total, float(v))
                    elif isinstance(v, dict):
                        _walk(v, prefix + k + ".")
        _walk(resp)
        return CreditView(total=total, allocatable=total, buckets=buckets, raw=resp)

    # ---- 生成 -----------------------------------------------------------
    def build_headers(self, secret: AccountSecret, path: str, body: Any = None,
                      extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h = dict(extra or {})
        ck = (secret.cookies or {}) if secret else {}
        # 实测（2026-10-16）：pixverse 只认请求头里的 `token`，光有 Cookie 会返回
        # 10003 user is not login。登录态令牌就是 cookie 里的 `_ai_token`，
        # 书签回传 document.cookie 能拿到它（非 HttpOnly），因此这里统一提取。
        token = (secret.extra or {}).get("token") or ck.get("_ai_token") or ""
        cookie_parts: List[str] = [f"{k}={v}" for k, v in ck.items()]
        if token and not any(p.startswith("token=") for p in cookie_parts):
            cookie_parts.append(f"token={token}")
        if cookie_parts:
            h["Cookie"] = "; ".join(cookie_parts)
        if token:
            h["token"] = token  # 关键：pixverse 网关认 header 的 token
        return h

    def submit(self, secret: AccountSecret, req: GenerateRequest) -> SubmitResult:
        if req.kind != "video":
            raise NotImplementedError("PixVerse 渠道暂只实现视频生成（image 接口 /image/t2i 已逆向未接入）")
        model = VIDEO_MODELS.get(req.model, req.model or "v5.5")
        payload: Dict[str, Any] = {
            "prompt": req.prompt,
            "model": model,
            "quality": (req.resolution or "720p").replace("p", "p"),
            "aspect_ratio": req.ratio or "16:9",
            "duration": int(req.duration or 5),
            "watermark": False,
        }
        # 渠道私有参数透传（camera_movement / audio / negative_prompt / seed ...）
        payload.update(req.params or {})
        headers = {"Refresh": "credit"}  # 前端生成接口统一带此头触发额度刷新
        if req.mode == "i2v":
            imgs = req.images or []
            if imgs:
                payload["customer_img_path"] = imgs[0]
                if len(imgs) > 1:
                    payload["customer_img_paths"] = imgs
            body = self._post("/video/i2v", payload, secret)
        else:
            body = self._post("/video/t2v", payload, secret)
        resp = self._handle(body)
        # Resp 形如 {VideoId: ...}（字段名大小写未实测，防御式提取）
        task_id = ""
        for key in ("VideoId", "video_id", "VideoID", "TaskId", "task_id"):
            if isinstance(resp, dict) and resp.get(key):
                task_id = str(resp[key])
                break
        if not task_id:
            return SubmitResult(accepted=False, error="未从 Resp 提取到任务 ID: %s" % resp,
                                raw=resp)
        return SubmitResult(upstream_task_id=task_id, transport="requests", raw=resp)

    def poll(self, secret: AccountSecret, upstream_task_id: str,
             req: Optional[GenerateRequest] = None) -> PollResult:
        payload = self._get("/task/list",
                            {"page_num": 1, "page_size": 20}, secret=secret)
        try:
            resp = self._handle(payload)
        except Exception as exc:  # noqa: BLE001
            return PollResult(status="failed", error=str(exc), raw=payload)
        items: List[Dict[str, Any]] = []
        for key in ("TaskList", "task_list", "tasks", "List", "list"):
            v = resp.get(key) if isinstance(resp, dict) else None
            if isinstance(v, list):
                items = v
                break
        target = None
        for it in items:
            if str(it.get("id") or it.get("Id") or it.get("video_id")
                   or it.get("VideoId") or "") == str(upstream_task_id):
                target = it
                break
        if target is None:
            return PollResult(status="processing", raw={"found": False, "count": len(items)})
        status_raw = target.get("status") or target.get("Status")
        try:
            status_num = int(status_raw)
        except (TypeError, ValueError):
            status_num = -1
        status = _TASK_STATUS_MAP.get(status_num) or norm_status(str(status_raw))
        media = [u for u in (target.get("video_url"), target.get("url")) if u]
        covers = [u for u in (target.get("cover_url"), target.get("first_frame")) if u]
        return PollResult(status=status, media=[str(u) for u in media],
                          covers=[str(u) for u in covers], raw=target)

    # ---- 错误归一化 -----------------------------------------------------
    def classify_error(self, payload: Any) -> str:
        try:
            s = json.dumps(payload, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            return "upstream"
        if "10003" in s or "not login" in s:
            return "auth_or_risk"
        if any(k in s for k in ("积分不足", "credits", "额度")):
            return "quota"
        if any(k in s for k in ("敏感", "违规", "审核")):
            return "content"
        return "upstream"
