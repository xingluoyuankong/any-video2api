# -*- coding: utf-8 -*-
"""any-video2api 统一入口。

一个 FastAPI 进程，按渠道描述符动态加载 jimeng / tiktok / coze（及未来任意）渠道，
对外暴露统一的 OpenAI 兼容接口 + 渠道管理接口。
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src.api import (admin_routes, auth_routes, channel_routes, config_routes,
                     openai_routes, ops_routes, proxy_routes, stats_routes)
from src.channels.registry import registry
from src.core.config import ROOT, config
from src.platform.accounts import manager as account_manager
from src.platform.keys import PUBLIC_PATHS, keys
from src.platform.media import MEDIA_DIR, BUCKETS, resolve_local
from src.platform.jobs import worker
from src.platform.session import KeepaliveScheduler, keeper
from src.platform.users import SESSION_COOKIE, users

# 控制台登录相关公开路径（登录接口本身必须免鉴权）
LOGIN_PUBLIC_PATHS = {"/login", "/api/auth/login"}


def _keepalive_accounts() -> list:
    """待保活账号列表（供 KeepaliveScheduler 注入）。"""
    return [{"id": r["id"], "channel": r["channel"]} for r in account_manager.raws()]


def _soft_fail(account_id: str) -> None:
    """软失败：只累计连续失败，达阈值（默认 3）才降级为 invalid。

    注意：本函数在 record_activity 之前调用，所以读到的 consecutive_failures 是「上一次」的值。
    """
    try:
        st = keeper.health(account_id) or {}
        fails = int(st.get("consecutive_failures") or 0) + 1
        if fails >= config.session_relogin_threshold:
            account_manager.mark(account_id, status="invalid")
    except Exception:  # noqa: BLE001
        pass


def _keepalive_verify(account_id: str) -> bool:
    """心跳验证：调用渠道 verify，并按「错误类型」决定是否降级账号状态。

    治本要点（原实现的问题：任何一次失败都直接 mark invalid）：
      - 成功             -> status=active，重置连续失败
      - 明确鉴权失败      -> status=invalid / expired（真正的凭证失效）
      - 网络/限流/风控等  -> **不改状态**，只累计失败；连续达阈值才降级
    """
    row = account_manager.raw(account_id)
    if not row:
        return False
    ch = registry.get(row["channel"])
    if ch is None:
        return False

    try:
        res = ch.verify(account_manager.secret(account_id))
    except Exception:  # noqa: BLE001  网络异常等，不算凭证失效
        _soft_fail(account_id)
        return False

    if res.ok:
        account_manager.mark(account_id, status="active")
        account_manager.release(account_id, True)   # 成功即重置连续失败
        return True

    # 只有明确的鉴权失败才立即降级
    if res.status in ("invalid", "expired"):
        account_manager.mark(account_id, status=res.status)
        return False

    # risk / unknown / 空 -> 临时问题，软失败处理
    _soft_fail(account_id)
    return False


keepalive = KeepaliveScheduler(_keepalive_accounts, _keepalive_verify,
                               interval=config.session_keepalive_interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    registry.load()
    worker.start()
    if config.session_enabled:
        keepalive.start()
    bootstrap = keys.ensure_bootstrap()
    if bootstrap:
        print(f"[any-video2api] 已生成初始 API Key: {bootstrap}")
        print(f"[any-video2api] 请立即保存；也可写入 config/setting.toml 的 [api].api_key 覆盖。")
    init_pwd = users.ensure_default_admin()
    if init_pwd:
        print("[any-video2api] 已创建管理员账号，初始密码见 data/initial_admin.txt（登录后请立即修改）")
    print(f"[any-video2api] 渠道: {registry.names()}  错误: {registry.errors()}")
    print(f"[any-video2api] 防风控: {'on' if config.antiban_enabled else 'off'}  "
          f"会话保活: {'on' if config.session_enabled else 'off'} "
          f"(间隔 {config.session_keepalive_interval:.0f}s)")
    try:
        yield
    finally:
        worker.stop()
        if config.session_enabled:
            keepalive.stop()


app = FastAPI(title="any-video2api", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[f"http://127.0.0.1:{config.port}", f"http://localhost:{config.port}"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """控制台用账号密码会话；程序调用用 API Key。

    - 公开路径（首页/健康/文档/控制台/静态/媒体/登录）直接放行
    - 其余请求：先认 session cookie，再回退 API Key（供程序调用与兼容）
    """
    path = request.url.path
    if (request.method == "OPTIONS" or path in PUBLIC_PATHS
            or path in LOGIN_PUBLIC_PATHS):
        return await call_next(request)
    if path.startswith("/media/") or path.startswith("/static/"):
        return await call_next(request)
    if config.auth_disabled:
        return await call_next(request)

    # 1) 程序接口 /v1/*：只认 API Key（控制台会话不能调用生成接口）
    if path.startswith("/v1/"):
        provided = request.headers.get("X-API-Key") or request.headers.get("Authorization", "")
        if keys.authenticate(provided) is not None:
            return await call_next(request)
        static_key = config.api_key
        if static_key and provided.removeprefix("Bearer ").strip() == static_key:
            return await call_next(request)
        return JSONResponse({"detail": "invalid or missing api key"}, status_code=401)

    # 2) 控制台会话（账号密码登录）
    session_user = users.validate_session(request.cookies.get(SESSION_COOKIE))
    if session_user:
        request.state.user = session_user
        return await call_next(request)

    # 3) 回退：API Key（供脚本/自动化调用控制台接口）
    provided = request.headers.get("X-API-Key") or request.headers.get("Authorization", "")
    key_row = keys.authenticate(provided)
    if key_row is not None:
        request.state.api_key = key_row
        return await call_next(request)
    static_key = config.api_key
    if static_key and provided.removeprefix("Bearer ").strip() == static_key:
        return await call_next(request)

    # 4) 未认证
    if path.startswith("/api/"):
        return JSONResponse({"detail": "未登录或登录已过期，请重新登录"}, status_code=401)
    return JSONResponse({"detail": "invalid or missing api key"}, status_code=401)


# ---- 路由挂载 ----
app.include_router(auth_routes.router)
app.include_router(openai_routes.router)
app.include_router(channel_routes.router)
app.include_router(admin_routes.router)
app.include_router(proxy_routes.router)
app.include_router(stats_routes.router)
app.include_router(config_routes.router)
app.include_router(ops_routes.router)

# ---- 静态资源（前端控制台）----
STATIC_DIR = ROOT / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def root():
    return {"service": "any-video2api", "channels": registry.names(),
            "docs": "/docs", "console": "/console"}


# ---- 媒体 ----
@app.api_route("/media/{bucket}/{name}", methods=["GET", "HEAD"])
def media(bucket: str, name: str):
    if bucket not in BUCKETS:
        raise HTTPException(status_code=404, detail="media not found")
    target = resolve_local(bucket, name)
    if not target:
        raise HTTPException(status_code=404, detail="media not found")
    return FileResponse(target)


# ---- 控制台 ----
@app.get("/login", response_class=HTMLResponse)
@app.get("/console", response_class=HTMLResponse)
def console():
    """返回控制台页面（登录页与主界面同一入口，前端按登录态切换）。"""
    for name in ("index.html", "console.html"):
        page = STATIC_DIR / name
        if page.exists():
            return page.read_text(encoding="utf-8")
    return HTMLResponse("<h1>any-video2api</h1><p>前端未生成</p>")


MEDIA_DIR.mkdir(parents=True, exist_ok=True)
