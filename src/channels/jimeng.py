# -*- coding: utf-8 -*-
"""即梦（jimeng.jianying.com）渠道适配器。

移植自 jimeng2api：MD5 轻签名 + Appid/Device-Time 头 + aigc_draft/generate 提交 +
get_history_by_ids 轮询 + benefits 额度/签到。
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Dict, List, Optional

from src.channels.base import (AccountSecret, AssetRef, Channel, ChannelSpec, CreditView,
                               GenerateRequest, ModelSpec, PollResult, SubmitResult,
                               VerifyResult, fingerprint_headers, norm_status)
from src.core.errors import UpstreamError, classify_error, find_values, redact
from src.platform.transport import HttpClient

APPID = "513695"
APPVR = "8.4.0"
SDK_VERSION = "48.0.0"
PF = "7"
SIGN_SALT_A = "9e2c"
SIGN_SALT_B = "11ac"


def _md5_sign(path: str, ts: str) -> str:
    raw = f"{SIGN_SALT_A}|{path[-7:]}|{PF}|{APPVR}|{ts}||{SIGN_SALT_B}"
    return hashlib.md5(raw.encode()).hexdigest()


class JimengChannel(Channel):
    name = "jimeng"

    def __init__(self, spec: ChannelSpec, log=None):
        super().__init__(spec, log)
        self._clients: Dict[str, HttpClient] = {}

    # ---- 认证 ----
    def _cookie_header(self, cookies: Dict[str, Any]) -> str:
        return "; ".join(f"{k}={v}" for k, v in cookies.items() if v is not None)

    def build_headers(self, secret: AccountSecret, path: str, body: Any = None,
                      extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        cookies = secret.cookies or {}
        ts = str(int(time.time()))
        h = {
            "Cookie": self._cookie_header(cookies),
            "App-Sdk-Version": SDK_VERSION,
            "Appid": APPID,
            "Device-Time": ts,
            "Lan": "zh-Hans",
            "Loc": "cn",
            "Sign": _md5_sign(path, ts),
            "Sign-Ver": "1",
            "Appvr": APPVR,
            "Pf": PF,
        }
        csrf = cookies.get("passport_csrf_token") or cookies.get("passport_csrf_token_default")
        if csrf:
            h["X-CSRFToken"] = str(csrf)
        h.update(fingerprint_headers(secret))  # 固定 UA / 语言头，防风控
        if extra:
            h.update(extra)
        return h

    def _default_params(self, secret: AccountSecret) -> dict:
        cookies = secret.cookies or {}
        p = {"aid": APPID, "device_platform": "web", "region": "CN",
             "da_version": "3.3.26", "web_version": "7.5.0", "aigc_features": "app_lip_sync"}
        web_id = cookies.get("v2_spipe_web_id") or cookies.get("tea_web_id") or cookies.get("s_v_web_id")
        if web_id:
            p["web_id"] = str(web_id)
        for k in ("msToken", "uifid", "a_bogus", "verifyFp", "fp", "x-secsdk-web-signature"):
            if k in cookies:
                p[k] = str(cookies[k])
        return p

    def _client(self, secret: AccountSecret, account_id: str = "") -> HttpClient:
        key = account_id or "default"
        if key not in self._clients:
            self._clients[key] = HttpClient(
                base_url=self.spec.base_url, proxy=self.spec.proxy or None,
                timeout=self.spec.timeout, user_agent=self.spec.user_agent,
                channel=self.name, account_id=account_id)
        return self._clients[key]

    def _call(self, secret: AccountSecret, name: str, body: Any, account_id: str = "",
              params: Optional[dict] = None) -> Any:
        ep = (self.spec.endpoints or {}).get(name, {})
        method = ep.get("method", "POST")
        path = ep.get("path", "")
        merged = {**self._default_params(secret), **(ep.get("params") or {}), **(params or {})}
        headers = self.build_headers(secret, path, body)
        return self._client(secret, account_id).request(method, path, body=body, params=merged,
                                                        headers=headers)

    def verify(self, secret: AccountSecret) -> VerifyResult:
        try:
            r = self._call(secret, "health", None, params={"account_sdk_source": "web"})
            ok = str(r.get("ret", "0")) in ("0", "200", "") if isinstance(r, dict) else False
            return VerifyResult(ok=ok, status="active" if ok else "invalid",
                                message="" if ok else str(r)[:200], raw=redact(r))
        except UpstreamError as e:
            s = str(e)
            # 只有明确鉴权失败才算凭证失效；网络/限流/风控算临时错误（risk，不降级）
            if "401" in s or "未登录" in s or "not logged" in s.lower():
                return VerifyResult(ok=False, status="invalid", message=s)
            return VerifyResult(ok=False, status="risk", message=s)

    def list_models(self, secret: AccountSecret) -> List[ModelSpec]:
        try:
            r = self._call(secret, "common_config", {"scene": "video_generate", "params": {}})
        except UpstreamError:
            return []
        items = ((r.get("data") or {}).get("model_list")) or []
        out = []
        for m in items:
            mid = str(m.get("model_req_key") or m.get("id") or "")
            if not mid:
                continue
            out.append(ModelSpec(
                id=mid, alias=str(m.get("name") or mid), display_name=str(m.get("name") or mid),
                kind="video", resolutions=[], raw=redact(m)))
        return out

    def credits(self, secret: AccountSecret) -> CreditView:
        try:
            r = self._call(secret, "credit", {"data": {}})
        except UpstreamError as e:
            return CreditView(error=str(e))
        data = r.get("data") if isinstance(r, dict) else {}
        nested = data if isinstance(data, dict) else {}
        credit = nested.get("credit") if isinstance(nested.get("credit"), dict) else {}

        def num(x):
            try:
                return float(x or 0)
            except Exception:
                return 0.0
        vip, gift, purchase = (num(credit.get(k)) for k in ("vip_credit", "gift_credit", "purchase_credit"))
        return CreditView(total=vip + gift + purchase, allocatable=num(nested.get("allocatable_credit")),
                          buckets={"vip": vip, "gift": gift, "purchase": purchase}, raw=redact(r))

    def checkin(self, secret: AccountSecret) -> Dict[str, Any]:
        try:
            r = self._call(secret, "checkin", {"data": {}})
            return {"status": "success" if str(r.get("ret", "0")) in ("0", "200", "") else "failed",
                    "raw": redact(r)}
        except UpstreamError as e:
            return {"status": "failed", "message": str(e)}

    # ---- 生成 ----
    def _payload(self, req: GenerateRequest) -> dict:
        model_id = (self.spec.models or {}).get(req.model, req.model)
        duration_ms = max(4000, min(180000, int(req.duration * 1000)))
        submit_id = str(uuid.uuid4())
        component_id = str(uuid.uuid4())
        scene = {"type": "video", "scene": "BasicVideoGenerateButton", "modelReqKey": model_id,
                 "videoDuration": req.duration, "materialTypes": [],
                 "reportParams": {"enterSource": "generate", "vipSource": "generate",
                                  "extraVipFunctionKey": model_id, "useVipFunctionDetailsReporterHoc": True}}
        metrics = json.dumps({"promptSource": "custom", "isDefaultSeed": 1, "originSubmitId": submit_id,
                              "isRegenerate": False, "enterFrom": "use_bgimage_prompt",
                              "position": "page_bottom_box", "functionMode": "first_last_frames",
                              "sceneOptions": json.dumps([scene], ensure_ascii=False)}, ensure_ascii=False)
        inp = {"type": "", "id": str(uuid.uuid4()), "min_version": "3.0.5", "prompt": req.prompt,
               "video_mode": 2, "fps": 24, "duration_ms": duration_ms, "idip_meta_list": []}
        if req.resolution:
            inp["resolution"] = req.resolution
        images = req.images or ([req.image] if req.image else [])
        if images:
            def img(uri):
                return {"format": "", "height": 0, "id": str(uuid.uuid4()), "image_uri": uri, "name": "",
                        "platform_type": 1, "source_from": "upload", "type": "image", "uri": uri, "width": 0}
            inp["first_frame_image"] = img(images[0])
            if len(images) > 1:
                inp["end_frame_image"] = img(images[1])
        draft = {"type": "draft", "id": str(uuid.uuid4()), "min_version": "3.0.5", "min_features": [],
                 "is_from_tsn": True, "version": "3.3.9", "main_component_id": component_id,
                 "component_list": [{"type": "video_base_component", "id": component_id,
                                     "min_version": "1.0.0", "aigc_mode": "workbench",
                                     "metadata": {"type": "", "id": str(uuid.uuid4()), "created_platform": 3,
                                                  "created_time_in_ms": str(int(time.time() * 1000)), "created_did": ""},
                                     "generate_type": "gen_video",
                                     "abilities": {"type": "", "id": str(uuid.uuid4()),
                                                   "gen_video": {"id": str(uuid.uuid4()), "type": "",
                                                                 "text_to_video_params": {
                                                                     "type": "", "id": str(uuid.uuid4()),
                                                                     "video_gen_inputs": [inp],
                                                                     "video_aspect_ratio": req.ratio,
                                                                     "seed": int(time.time() * 1000) % 4294967296,
                                                                     "model_req_key": model_id, "priority": 0},
                                                                 "video_task_extra": metrics},
                                                   "process_type": 1}}]}
        benefit = self._benefit_type(model_id)
        return {"extend": {"root_model": model_id,
                           "m_video_commerce_info": {"benefit_type": benefit, "resource_id": "generate_video",
                                                     "resource_id_type": "str", "resource_sub_type": "aigc"},
                           "m_video_commerce_info_list": [{"benefit_type": benefit, "resource_id": "generate_video",
                                                           "resource_id_type": "str", "resource_sub_type": "aigc"}]},
                "submit_id": submit_id, "metrics_extra": metrics,
                "draft_content": json.dumps(draft, ensure_ascii=False, separators=(",", ":")),
                "http_common_info": {"aid": int(APPID)}}

    @staticmethod
    def _benefit_type(model_id: str) -> str:
        table = [("45_pro", "seedance_25_720p_output"), ("40_mini", "seedance_20_mini_720p_output"),
                 ("40_pro_vision", "seedance_20_pro_720p_output"), ("40_vision", "seedance_20_fast_720p_output"),
                 ("3.5_pro", "dreamina_video_seedance_15_pro"), ("minimax_h3", "minimax_h3_768p_output"),
                 ("happyhorse", "happyhorse_11_720p_output")]
        for key, val in table:
            if key in model_id:
                return val
        if "3.0_pro" in model_id:
            return "basic_video_operation_vgfm_v_three_pro"
        return "basic_video_operation_vgfm_v_three"

    def submit(self, secret: AccountSecret, req: GenerateRequest) -> SubmitResult:
        body = self._payload(req)
        r = self._call(secret, "generate", body, params=(req.params or {}).get("params"))
        code = r.get("ret") if isinstance(r, dict) else None
        if str(code) not in ("0", "200", "", "None"):
            raise UpstreamError(str(r.get("message") or "jimeng submit failed"),
                                self.classify_error(r), payload=redact(r))
        tid = find_values(r, {"history_id", "historyid", "history_record_id", "record_id", "task_id"})
        return SubmitResult(upstream_task_id=str(tid[0]) if tid else "", transport="requests",
                            raw=redact(r))

    def poll(self, secret: AccountSecret, upstream_task_id: str,
             req: Optional[GenerateRequest] = None) -> PollResult:
        r = self._call(secret, "poll", {"history_ids": [str(upstream_task_id)]})
        return self._normalize_poll(r, upstream_task_id)

    @staticmethod
    def _normalize_poll(payload: Any, upstream_task_id: str) -> PollResult:
        videos, images = [], []
        video_keys = {"video_url", "videourl", "play_url", "playurl", "download_url"}
        image_keys = {"cover_url", "coverurl", "image_url", "imageurl"}

        def walk(x, path=""):
            if isinstance(x, dict):
                for k, v in x.items():
                    lk = str(k).lower()
                    p = path + "/" + lk
                    if isinstance(v, str) and v.startswith("http"):
                        if lk in image_keys:
                            images.append(v)
                        elif lk in video_keys or ("/video/" in p and lk == "url"):
                            videos.append(v)
                    walk(v, p)
            elif isinstance(x, list):
                for i, v in enumerate(x):
                    walk(v, path + "/" + str(i))
        walk(payload)
        videos = list(dict.fromkeys(videos))
        images = list(dict.fromkeys(images))
        fail = any(str(x).strip().lower() not in {"", "0", "none", "null"}
                   for k in ("fail_code", "error_code", "error_message")
                   for x in find_values(payload, {k}))
        if videos:
            status = "success"
        elif fail:
            status = "failed"
        else:
            status = "processing"
        return PollResult(status=status, media=videos, covers=images,
                          error_class="upstream" if status == "failed" else "",
                          raw=redact(payload))

    def classify_error(self, payload: Any) -> str:
        code = ""
        msg = ""
        if isinstance(payload, dict):
            code = str(payload.get("ret") or payload.get("code") or "")
            msg = str(payload.get("message") or payload.get("errmsg") or "")
        return classify_error(code, msg)

    def upload(self, secret: AccountSecret, data: bytes, filename: str,
               content_type: str = "application/octet-stream") -> AssetRef:
        # 真实链路：get_upload_token -> ImageX ApplyImageUpload -> Commit。此处保留入口，
        # 由 upload_service 复用 jimeng2api 的 STS 签名逻辑实现。
        raise NotImplementedError("jimeng 上传需接入 ImageX STS 链路（见 upload_service）")


# 便于 registry 按约定名加载
JimengChannel = JimengChannel
