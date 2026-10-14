# -*- coding: utf-8 -*-
"""统一渠道抽象层（Channel Contract）。

这是整个平台的"接缝"：平台基座（账号池 / 任务队列 / 媒体 / 审计 / OpenAI 兼容层）
只依赖本文件定义的接口，不依赖任何具体渠道。新增一个渠道 = 实现一个 Channel 子类 +
写一份渠道描述符（config/channels/<name>.json），无需改动平台任何一行代码。

从 jimeng2api / tiktok2api / coze2api 三个项目提炼出的差异点，全部收敛到本接口：

    维度          jimeng                tiktok              coze
    ------------  --------------------  ------------------  ------------------------
    上游 base     jimeng.jianying.com   ads.tiktok.com      api/www/code.coze.cn
    认证          Cookie+MD5轻签名      Cookie+CSRF(无签名) OAuth(PKCE) 或 Cookie
    提交          /mweb/v1/aigc_draft/  .../cue/...         /api/workflow(_api)/...
    轮询          get_history_by_ids    check_gen_task      workflow/get_process
    额度          benefits/user_credit  get_permission      credit/balance/check
    签到          credit_receive        -                   /checkin
    传输          requests(+PW兜底)     requests            fetch 协议(Playwright仅诊断)
    任务ID        history_id            task_id             execute_id
"""
from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# 归一化数据结构：平台与渠道之间只传这些类型，渠道负责把上游 payload 翻译进来
# ---------------------------------------------------------------------------

#: 上游错误分类（三项目各自都有一份等价实现，此处统一）
ERROR_CLASSES = ("quota", "auth_or_risk", "concurrency", "timeout", "content", "upstream")

#: 统一任务状态
JOB_STATES = ("queued", "submitted", "processing", "success", "failed", "cancelled")


@dataclass
class ChannelSpec:
    """渠道静态描述符，来自 config/channels/<name>.json。"""
    name: str
    display_name: str = ""
    kind: str = "video"                      # video | image | chat | mixed
    base_url: str = ""
    proxy: str = ""
    timeout: int = 120
    user_agent: str = "Mozilla/5.0"
    transports: List[str] = field(default_factory=lambda: ["requests"])  # requests|playwright
    supports: Dict[str, bool] = field(default_factory=dict)   # video/image/upload/checkin/oauth/credits/models
    models: Dict[str, Any] = field(default_factory=dict)      # 别名 -> 上游 id
    endpoints: Dict[str, Any] = field(default_factory=dict)   # name -> {method,path,params}
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, name: str, raw: Dict[str, Any]) -> "ChannelSpec":
        known = {k: raw[k] for k in cls.__dataclass_fields__ if k in raw and k != "name"}
        spec = cls(name=name, **known)
        return spec


@dataclass
class AccountSecret:
    """账号凭据（明文形态，落库前由平台加密）。"""
    cookies: Dict[str, Any] = field(default_factory=dict)
    browser_state: Dict[str, Any] = field(default_factory=dict)
    oauth: Dict[str, Any] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VerifyResult:
    ok: bool
    status: str = "unknown"        # active | invalid | expired | risk
    message: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelSpec:
    id: str
    alias: str = ""
    display_name: str = ""
    kind: str = "video"            # video | image | chat
    resolutions: List[str] = field(default_factory=list)
    durations: List[int] = field(default_factory=list)
    ratios: List[str] = field(default_factory=list)
    supports_image_input: bool = False
    supports_long_video: bool = False
    credit_requirements: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CreditView:
    total: float = 0.0
    allocatable: float = 0.0
    buckets: Dict[str, float] = field(default_factory=dict)
    error: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GenerateRequest:
    """统一生成请求（平台层解析 /v1/* 后构造）。"""
    channel: str = ""
    kind: str = "video"            # video | image | chat
    model: str = ""
    prompt: str = ""
    mode: str = "t2v"              # t2v|i2v|r2v|...
    duration: int = 5
    ratio: str = "16:9"
    resolution: str = "720p"
    size: str = ""
    images: List[str] = field(default_factory=list)
    params: Dict[str, Any] = field(default_factory=dict)   # 渠道私有参数透传
    account_mode: str = "auto"     # auto|round_robin|least_used|specific|concurrent
    account_id: Optional[str] = None
    timeout: int = 600
    idempotency_key: str = ""


@dataclass
class SubmitResult:
    upstream_task_id: str = ""
    transport: str = "requests"
    accepted: bool = True
    error: str = ""
    error_class: str = ""          # 见 ERROR_CLASSES
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PollResult:
    status: str = "processing"     # queued|processing|success|failed
    media: List[str] = field(default_factory=list)     # 视频/图片直链（原始）
    covers: List[str] = field(default_factory=list)
    error: str = ""
    error_class: str = ""
    progress: float = 0.0
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AssetRef:
    uri: str = ""                  # 上游可引用的素材 URI / URL
    url: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 渠道抽象基类
# ---------------------------------------------------------------------------

class Channel(abc.ABC):
    """所有渠道适配器的父类。

    平台只调用以下方法。渠道不得反向依赖 src.api 或 src.platform（只能依赖
    src.core 的公共件），以保证渠道可插拔、可单测。
    """

    #: 渠道名（与 config/channels/<name>.json 一致）
    name: str = ""

    def __init__(self, spec: ChannelSpec, log=None):
        self.spec = spec
        self.log = log

    # ---- 元信息 ----------------------------------------------------------
    def capabilities(self) -> Dict[str, bool]:
        """返回支持的原子能力，供 /api/channels 与调度器使用。"""
        return {
            "models": True, "credits": False, "checkin": False,
            "upload": False, "oauth": False, "verify": True,
            **self.spec.supports,
        }

    def endpoint_map(self) -> Dict[str, Any]:
        """返回该渠道当前生效的上游端点映射（供 /api/channels/{name}/endpoints）。"""
        return self.spec.endpoints

    # ---- 认证 ------------------------------------------------------------
    @abc.abstractmethod
    def build_headers(self, secret: AccountSecret, path: str, body: Any = None,
                      extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """构造一次上游请求所需的全部头（含渠道特有签名）。"""

    def verify(self, secret: AccountSecret) -> VerifyResult:
        """验证账号登录态。默认按 capability 探测；渠道可覆写。"""
        return VerifyResult(ok=bool(secret.cookies or secret.oauth), status="unknown")

    def refresh(self, secret: AccountSecret) -> Optional[AccountSecret]:
        """OAuth 刷新。返回新凭据，或 None（不支持 / 失败）。"""
        return None

    # ---- 目录 / 额度 / 签到 ---------------------------------------------
    def list_models(self, secret: AccountSecret) -> List[ModelSpec]:
        return []

    def credits(self, secret: AccountSecret) -> CreditView:
        return CreditView()

    def checkin(self, secret: AccountSecret) -> Dict[str, Any]:
        return {"status": "unsupported"}

    # ---- 生成 ------------------------------------------------------------
    @abc.abstractmethod
    def submit(self, secret: AccountSecret, req: GenerateRequest) -> SubmitResult:
        """提交生成任务，返回上游任务 ID。"""

    @abc.abstractmethod
    def poll(self, secret: AccountSecret, upstream_task_id: str,
             req: Optional[GenerateRequest] = None) -> PollResult:
        """轮询任务状态，把上游 payload 归一化为 PollResult。"""

    def upload(self, secret: AccountSecret, data: bytes, filename: str,
               content_type: str = "application/octet-stream") -> AssetRef:
        raise NotImplementedError(f"{self.name} 不支持素材上传")

    # ---- 在线登录（可选，默认不支持；实现后控制台「导入账号」会出现对应方式）----
    def login_methods(self) -> List[str]:
        """声明本渠道支持的在线登录方式：sms | pwd | oauth。"""
        return []

    def login_send_sms(self, phone: str, area_code: str = "86") -> Dict[str, Any]:
        """发送登录短信验证码。"""
        raise NotImplementedError(f"{self.name} 不支持短信登录")

    def login_by_sms(self, phone: str, code: str, area_code: str = "86") -> Optional[AccountSecret]:
        """手机号 + 验证码登录，返回凭据。"""
        raise NotImplementedError(f"{self.name} 不支持短信登录")

    def login_by_password(self, account: str, password: str) -> Optional[AccountSecret]:
        """账密登录，返回凭据。"""
        raise NotImplementedError(f"{self.name} 不支持账密登录")

    def login_oauth(self) -> Dict[str, Any]:
        """返回 {login_url, bookmarklet, hint}，供前端展示（不启动浏览器）。"""
        raise NotImplementedError(f"{self.name} 不支持授权链接登录")

    # ---- 错误归一化（渠道覆写以识别自家错误码）--------------------------
    def classify_error(self, payload: Any) -> str:
        return "upstream"


# ---------------------------------------------------------------------------
# 便捷工具：渠道共用的归一化辅助
# ---------------------------------------------------------------------------

def fingerprint_headers(secret: "AccountSecret") -> Dict[str, str]:
    """从 secret.extra['fingerprint'] 生成防风控请求头。

    同一账号始终返回同一套 UA / Accept-Language，避免被判为新设备。
    """
    fp = (getattr(secret, "extra", None) or {}).get("fingerprint") or {}
    if not fp:
        return {}
    out: Dict[str, str] = {}
    if fp.get("user_agent"):
        out["User-Agent"] = str(fp["user_agent"])
    if fp.get("accept_language"):
        out["Accept-Language"] = str(fp["accept_language"])
    return out


def norm_status(state: str) -> str:
    s = str(state or "").strip().lower()
    if s in ("success", "succeeded", "completed", "done", "finished"):
        return "success"
    if s in ("failed", "failure", "rejected", "cancelled", "canceled", "error"):
        return "failed"
    if s in ("queued", "pending", "submitted", "created"):
        return "queued"
    return "processing"


def now() -> float:
    return time.time()
