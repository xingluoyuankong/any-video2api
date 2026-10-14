# -*- coding: utf-8 -*-
"""豆包（www.doubao.com）渠道适配器。

移植自 doubao2api（其逆向目标为**豆包网页版 / PC 桌面客户端协议**，非 App）：

- 鉴权：Cookie（``sessionid`` / ``ttwid`` / ``passport_csrf_token``）+ URL query
  安全参数（``msToken`` / ``fp`` / ``device_id`` / ``web_id`` / ``aid`` ...）。
  **无 a_bogus / X-Bogus 签名**——参考项目纯 HTTP 直连路径只用上述参数即可；
  a_bogus 仅出现在其 Playwright 浏览器路径里，由字节前端 JSSDK 自动注入，
  本适配器不依赖浏览器。
- 提交：``POST /samantha/chat/completion``（``messages[0].content_type=2020``、
  ``skill.skill_type=17``），SSE 响应中取 ``fin_reason.async_task.id`` 作为任务 ID。
- 轮询：``POST /samantha/chat/async/stream``（body ``{"task_id","event_id":0}``），
  SSE 响应 ``content_type=2021`` 中取 ``video_url`` 或 ``video_model.video_list.*.main_url``。

模型选择在服务端按 ``skill_type=17`` 决定（参考项目 README 称其使用 Seedance 2.0），
网页协议**未暴露显式 model id**，故描述符里的别名仅作展示，提交时不发送 model 字段。
"""
from __future__ import annotations

import base64
import json
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from src.channels.base import (AccountSecret, Channel, ChannelSpec, CreditView,
                               GenerateRequest, ModelSpec, PollResult, SubmitResult,
                               VerifyResult, fingerprint_headers)
from src.core.errors import UpstreamError, classify_error, redact
from src.platform.transport import HttpClient

BASE_URL = "https://www.doubao.com"
AID = "582478"

# 枚举值（来自 doubao2api/doubao2api/client.py）
CT_VIDEO_IN = 2020         # SamanthaVideoGenerationInput
CT_VIDEO_OUT = 2021        # SamanthaVideoGenerationOutput
CT_EVENT_MSG = 2001        # CMPL 消息块
CT_EVENT_ERR = 2005        # ERR 事件
SKILL_VIDEO = 17           # SkillVideoGeneration
REASON_ASYNC_TASK = 1      # FinReasonAsyncTask
RISK_ERROR_CODES = {710022002, 710022004}
EXPIRED_ERROR_CODE = 710012001


def _classify(code: Any, msg: str = "") -> str:
    """豆包专属错误码归一化：先认风控/登录态族，再回落通用规则。

    注意豆包 gateway-error 的数字码常藏在 message 里（code 字段是
    ``"Internal ErrorX"`` 之类），故对 code+msg 一起做数字扫描。
    """
    for n in re.findall(r"\d{6,}", f"{code or ''} {msg or ''}"):
        ni = int(n)
        if ni in RISK_ERROR_CODES:      # 风控 / 需验证码
            return "auth_or_risk"
        if 710012000 <= ni <= 710012999:  # passport 登录态族（如 710012001 登录已过期）
            return "auth_or_risk"
    return classify_error(code, msg)


class DoubaoChannel(Channel):
    name = "doubao"

    def __init__(self, spec: ChannelSpec, log=None):
        super().__init__(spec, log)
        self._clients: Dict[str, HttpClient] = {}

    # ---- 认证 ----------------------------------------------------------------
    @staticmethod
    def _cookie_header(cookies: Dict[str, Any]) -> str:
        return "; ".join(f"{k}={v}" for k, v in cookies.items() if v is not None)

    def build_headers(self, secret: AccountSecret, path: str, body: Any = None,
                      extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        cookies = secret.cookies or {}
        h: Dict[str, str] = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "Origin": BASE_URL,
            "Referer": BASE_URL + "/chat",
            "Agw-Js-Conv": "str",
        }
        if cookies:
            h["Cookie"] = self._cookie_header(cookies)
        # 豆包对 /chat/completion 系列要求 csrf 头（取自 cookie）
        csrf = cookies.get("passport_csrf_token") or cookies.get("passport_csrf_token_default")
        if csrf:
            h["x-tt-passport-csrf-token"] = str(csrf)
        h.update(fingerprint_headers(secret))  # 固定 UA / 语言头，防风控
        if extra:
            h.update(extra)
        return h

    def _security_params(self, secret: AccountSecret, include_fp: bool = True) -> Dict[str, str]:
        """构造 URL query 安全参数（豆包的"签名"实际就是这套设备指纹参数）。"""
        cookies = secret.cookies or {}
        ex = secret.extra or {}
        p: Dict[str, str] = {
            "aid": AID, "real_aid": AID,
            "device_platform": "web", "language": "zh", "region": "CN",
            "sys_region": "CN", "pkg_type": "release_version",
            "version_code": "20800", "pc_version": "2.1.7",
            "client_platform": "pc_client", "runtime": "web",
            "runtime_version": "3.5.4", "samantha_web": "1",
            "use-olympus-account": "1",
            "web_tab_id": str(uuid.uuid4()),
        }
        device_id = ex.get("device_id") or cookies.get("device_id")
        if device_id:
            p["device_id"] = str(device_id)
            p["tea_uuid"] = str(device_id)
        web_id = ex.get("web_id") or cookies.get("web_id")
        if web_id:
            p["web_id"] = str(web_id)
        if include_fp:  # 轮询 async/stream 不带 fp（与参考实现一致）
            fp = ex.get("fp") or cookies.get("fp") or cookies.get("verifyFp")
            if fp:
                p["fp"] = str(fp)
        # 空 / 伪造 msToken 会触发风控 710022002，仅在确有值时携带
        ms_token = ex.get("ms_token") or cookies.get("msToken")
        if ms_token:
            p["msToken"] = str(ms_token)
        return p

    # ---- 传输 ----------------------------------------------------------------
    def _client(self, account_id: str = "") -> HttpClient:
        key = account_id or "default"
        if key not in self._clients:
            self._clients[key] = HttpClient(
                base_url=self.spec.base_url or BASE_URL,
                proxy=self.spec.proxy or None, timeout=self.spec.timeout,
                user_agent=self.spec.user_agent, channel=self.name, account_id=account_id)
        return self._clients[key]

    def _sse_post(self, secret: AccountSecret, ep_name: str, body: Any,
                  params: Optional[dict] = None, account_id: str = "",
                  timeout: Optional[int] = None) -> str:
        """发一个 SSE 请求并返回**完整响应文本**。

        豆包提交/轮询端点均为 ``text/event-stream``；HttpClient.request 会因 JSON
        解析失败而把正文截断到 2000 字符，无法解析长轮询结果，故此处复用底层
        session 取原始文本（代理 / UA / 指纹仍由 HttpClient 统一配置）。
        """
        ep = (self.spec.endpoints or {}).get(ep_name, {})
        method = ep.get("method", "POST")
        path = ep.get("path", "")
        client = self._client(account_id)
        url = client._url(path)
        headers = self.build_headers(secret, path, body)
        try:
            resp = client.session.request(method.upper(), url, json=body,
                                          params=params or {}, headers=headers,
                                          timeout=timeout or self.spec.timeout)
        except Exception as exc:  # requests / 代理层异常统一归一化
            raise UpstreamError(f"doubao 网络异常: {exc}", "timeout") from exc
        if resp.status_code >= 400:
            raise UpstreamError(f"doubao 上游 HTTP {resp.status_code}",
                                classify_error(resp.status_code, resp.text[:300]),
                                resp.status_code, payload=redact(resp.text[:500]))
        return resp.text

    # ---- 元信息 --------------------------------------------------------------
    def list_models(self, secret: AccountSecret) -> List[ModelSpec]:
        out: List[ModelSpec] = []
        for alias in (self.spec.models or {}):
            out.append(ModelSpec(id=alias, alias=alias, display_name=alias, kind="video",
                                 resolutions=["720p", "1080p"],
                                 durations=[5, 10], ratios=["1:1", "16:9", "9:16"],
                                 supports_image_input=alias.endswith("-i2v")))
        return out

    def credits(self, secret: AccountSecret) -> CreditView:
        return CreditView(error="doubao 网页版未暴露额度查询接口")

    def checkin(self, secret: AccountSecret) -> Dict[str, Any]:
        return {"status": "unsupported", "reason": "doubao 网页版无签到接口"}

    def verify(self, secret: AccountSecret) -> VerifyResult:
        cookies = secret.cookies or {}
        if not cookies:
            return VerifyResult(ok=False, status="invalid", message="缺少豆包 Cookie（sessionid 等）")
        if not any(k in cookies for k in ("sessionid", "sessionid_ss", "sid_tt")):
            return VerifyResult(ok=False, status="invalid",
                                message="Cookie 中未找到 sessionid / sid_tt，无法确认登录态")
        return VerifyResult(
            ok=True, status="unknown",
            message="仅校验到 sessionid 存在；豆包网页版无公开 whoami 端点，登录态以 submit 实际结果为准")

    # ---- 生成：提交 ----------------------------------------------------------
    def _submit_event(self, req: GenerateRequest) -> Dict[str, Any]:
        content: Dict[str, Any] = {"text": req.prompt}
        if req.ratio:
            content["ratio"] = req.ratio
        cam = (req.params or {}).get("camera_movement")
        if cam:
            content["camera_movement"] = cam
        message: Dict[str, Any] = {
            "content": json.dumps(content, ensure_ascii=False),
            "content_type": CT_VIDEO_IN,
            "attachments": [],
            "references": [],
            "skill": {"skill_type": SKILL_VIDEO, "skill_type_no_default": SKILL_VIDEO,
                      "skill_id": str(SKILL_VIDEO), "skill_id_no_default": str(SKILL_VIDEO)},
        }
        ref_key = (req.params or {}).get("ref_image_key") or (req.images[0] if req.images else "")
        if ref_key:  # 图生视频：附参考图 key
            message["attachments"] = [{"type": "image", "key": ref_key}]
        return {
            "messages": [message],
            "completion_option": {
                "is_regen": False, "with_suggest": True, "need_create_conversation": True,
                "launch_stage": 1, "is_replace": False, "is_delete": False,
                "is_ai_playground": False, "memory_type": 2, "message_from": 0,
                "use_deep_think": False, "use_auto_cot": False, "resend_for_regen": False,
                "enable_commerce_credit": False, "action_bar_skill_id": SKILL_VIDEO,
            },
            "evaluate_option": {"web_ab_params": ""},
            "local_conversation_id": str(uuid.uuid4()),
            "local_message_id": str(uuid.uuid4()),
        }

    def submit(self, secret: AccountSecret, req: GenerateRequest) -> SubmitResult:
        if not (secret.cookies or {}):
            return SubmitResult(accepted=False, error="缺少豆包 Cookie（sessionid 等）",
                                error_class="auth_or_risk")
        try:
            body = self._submit_event(req)
            params = self._security_params(secret, include_fp=True)
            raw = self._sse_post(secret, "submit", body, params=params,
                                 timeout=min(self.spec.timeout, 60))
            task_id = self._extract_task_id(raw)
            if task_id:
                return SubmitResult(upstream_task_id=task_id, transport="requests",
                                    raw={"sse_len": len(raw)})
            err = self._extract_error(raw)
            if err:
                return SubmitResult(accepted=False, error=err[0], error_class=err[1],
                                    raw={"sse": redact(raw[:1000])})
            return SubmitResult(
                accepted=False, error_class="upstream",
                error="豆包未返回 async_task.id（可能登录态失效或响应格式变化）",
                raw={"sse": redact(raw[:1000])})
        except UpstreamError as e:
            return SubmitResult(accepted=False, error=str(e), error_class=e.error_class)
        except Exception as e:  # 兜底：绝不把异常抛到平台层
            return SubmitResult(accepted=False, error=f"{type(e).__name__}: {e}",
                                error_class="upstream")

    # ---- 生成：轮询 ----------------------------------------------------------
    def poll(self, secret: AccountSecret, upstream_task_id: str,
             req: Optional[GenerateRequest] = None) -> PollResult:
        if not upstream_task_id:
            return PollResult(status="failed", error="缺少 upstream_task_id",
                              error_class="upstream")
        try:
            params = self._security_params(secret, include_fp=False)
            body = {"task_id": str(upstream_task_id), "event_id": 0}
            raw = self._sse_post(secret, "poll", body, params=params)
            return self._parse_poll(raw)
        except UpstreamError as e:
            return PollResult(status="failed", error=str(e), error_class=e.error_class)
        except Exception as e:
            return PollResult(status="failed", error=f"{type(e).__name__}: {e}",
                              error_class="upstream")

    # ---- SSE 解析辅助 --------------------------------------------------------
    @staticmethod
    def _iter_sse_data(raw: str):
        """逐块产出 SSE 的 data 行反序列化后的对象。"""
        for block in raw.split("\n\n"):
            data_str = ""
            for line in block.strip().split("\n"):
                if line.startswith("data:"):
                    data_str = line[5:].strip()
            if not data_str:
                continue
            try:
                yield json.loads(data_str)
            except Exception:
                continue

    @staticmethod
    def _extract_task_id(raw: str) -> str:
        """从提交响应的 SSE 中提取 fin_reason.async_task.id。"""
        for data in DoubaoChannel._iter_sse_data(raw):
            if not isinstance(data, dict) or data.get("event_type") != CT_EVENT_MSG:
                continue
            ed = data.get("event_data")
            if isinstance(ed, str):
                try:
                    ed = json.loads(ed)
                except Exception:
                    continue
            if not isinstance(ed, dict):
                continue
            fin = ed.get("fin_reason") or {}
            if isinstance(fin, dict) and fin.get("reason") == REASON_ASYNC_TASK:
                tid = (fin.get("async_task") or {}).get("id", "")
                if tid:
                    return str(tid)
        return ""

    @staticmethod
    def _extract_error(raw: str) -> Optional[Tuple[str, str]]:
        """从 SSE / 顶层 JSON 中提取错误信息 -> (message, error_class)。"""
        # 1) gateway-error 事件
        for block in raw.split("\n\n"):
            ev, data_str = "", ""
            for line in block.strip().split("\n"):
                if line.startswith("event:"):
                    ev = line[6:].strip()
                elif line.startswith("data:"):
                    data_str = line[5:].strip()
            if ev == "gateway-error" and data_str:
                try:
                    obj = json.loads(data_str)
                except Exception:
                    obj = {}
                code = obj.get("code")
                msg = str(obj.get("message") or data_str)
                return (f"gateway-error code={code} {msg}"[:300], _classify(code, msg))
        # 2) SSE 内 error_code / ERR 事件
        for data in DoubaoChannel._iter_sse_data(raw):
            if not isinstance(data, dict):
                continue
            if data.get("event_type") == CT_EVENT_ERR:
                return (str(data.get("event_data"))[:300], "upstream")
            ec = data.get("error_code")
            if ec:
                em = str(data.get("error_msg") or "")
                return (f"code={ec} {em}"[:300], _classify(ec, em))
        # 3) 顶层 JSON 错误（非 SSE）
        s = raw.lstrip()
        if s.startswith("{"):
            try:
                obj = json.loads(raw)
            except Exception:
                obj = None
            if isinstance(obj, dict):
                code = obj.get("code") or obj.get("error_code")
                if code is not None:
                    msg = str(obj.get("msg") or obj.get("message") or "")
                    return (f"code={code} {msg}"[:300], _classify(code, msg))
        return None

    @staticmethod
    def _decode_video_model(vm: Any) -> str:
        """video_model.video_list.*.main_url 是 base64 编码的直链。"""
        if not vm:
            return ""
        if isinstance(vm, str):
            try:
                vm = json.loads(vm)
            except Exception:
                return ""
        if not isinstance(vm, dict):
            return ""
        vlist = vm.get("video_list") or {}
        if isinstance(vlist, dict):
            for _q, vinfo in vlist.items():
                if isinstance(vinfo, dict) and vinfo.get("main_url"):
                    try:
                        return base64.b64decode(vinfo["main_url"]).decode("utf-8", "replace")
                    except Exception:
                        return ""
        return ""

    def _parse_poll(self, raw: str) -> PollResult:
        media: List[str] = []
        covers: List[str] = []
        err = ""
        err_class = ""
        for data in self._iter_sse_data(raw):
            if not isinstance(data, dict):
                continue
            et = data.get("event_type")
            if et == CT_EVENT_ERR:
                err = str(data.get("event_data"))[:300]
                err_class = "upstream"
                continue
            if et != CT_EVENT_MSG:
                continue
            ed = data.get("event_data")
            if isinstance(ed, str):
                try:
                    ed = json.loads(ed)
                except Exception:
                    continue
            if not isinstance(ed, dict):
                continue
            msg = ed.get("message") or {}
            if not isinstance(msg, dict) or msg.get("content_type") != CT_VIDEO_OUT:
                continue
            content = msg.get("content")
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except Exception:
                    content = {}
            if not isinstance(content, dict):
                content = {}
            for item in (content.get("data") or [content]):
                if not isinstance(item, dict):
                    continue
                url = item.get("video_url") or item.get("url") or ""
                if not url:
                    url = self._decode_video_model(item.get("video_model"))
                if url:
                    media.append(url)
                cover = item.get("cover_url") or (item.get("cover") or {}).get("url") or ""
                if cover:
                    covers.append(cover)
        media = list(dict.fromkeys(media))
        covers = list(dict.fromkeys(covers))
        if media:
            return PollResult(status="success", media=media, covers=covers, progress=1.0,
                              raw={"sse_len": len(raw)})
        if err:
            return PollResult(status="failed", error=err, error_class=err_class or "upstream",
                              raw={"sse_len": len(raw)})
        return PollResult(status="processing", raw={"sse_len": len(raw)})

    # ---- 错误归一化 ----------------------------------------------------------
    def classify_error(self, payload: Any) -> str:
        if isinstance(payload, dict):
            code = payload.get("code") or payload.get("error_code")
            msg = str(payload.get("msg") or payload.get("message")
                      or payload.get("error_msg") or "")
            return _classify(code, msg)
        return _classify("", str(payload))
