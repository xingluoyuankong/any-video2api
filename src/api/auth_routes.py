# -*- coding: utf-8 -*-
"""控制台账号密码登录与会话管理。

    POST /api/auth/login                 用户名 + 密码 -> 下发 session cookie
    POST /api/auth/logout                注销当前会话
    GET  /api/auth/me                    当前登录用户
    POST /api/auth/password              修改自己的密码
    GET  /api/auth/sessions              我的登录会话
    POST /api/auth/sessions/revoke-all   注销我的全部会话
    GET  /api/auth/users                 用户列表（admin）
    POST /api/auth/users                 新建用户（admin）
    DELETE /api/auth/users/{username}    删除用户（admin）
    POST /api/auth/users/{username}/reset    重置密码（admin）
    POST /api/auth/users/{username}/disable|enable  禁用/启用（admin）
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from src.platform.users import SESSION_COOKIE, SESSION_TTL, users

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE, value=token, max_age=int(SESSION_TTL),
        httponly=True, samesite="lax", path="/",
        # 走 Cloudflare / HTTPS 时由边缘终止 TLS，本地是 HTTP，故不强制 secure
        secure=False,
    )


class LoginBody(BaseModel):
    username: str
    password: str


class PasswordBody(BaseModel):
    old_password: str
    new_password: str


class UserCreateBody(BaseModel):
    username: str
    password: str
    role: str = "admin"


class ResetBody(BaseModel):
    new_password: str


# ---- 登录 / 登出 ----
@router.post("/login")
def login(body: LoginBody, request: Request, response: Response):
    user = users.verify(body.username, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = users.create_session(user, ip=_client_ip(request),
                                 ua=request.headers.get("user-agent", ""))
    _set_session_cookie(response, token)
    return {"ok": True, "user": {"username": user["username"], "role": user.get("role", "admin")}}


@router.post("/logout")
def logout(request: Request, response: Response):
    users.revoke_session(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return {"username": user["username"], "role": user.get("role", "admin")}


@router.post("/password")
def change_password(body: PasswordBody, request: Request, response: Response):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    if not body.new_password or len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码至少 6 位")
    if not users.change_password(user["username"], body.old_password, body.new_password):
        raise HTTPException(status_code=400, detail="原密码错误")
    # 改密后全部会话失效，需重新登录
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True, "relogin_required": True}


@router.get("/sessions")
def my_sessions(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return {"sessions": users.list_sessions(user["id"])}


@router.post("/sessions/revoke-all")
def revoke_all(request: Request, response: Response):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    n = users.revoke_user_sessions(user["id"])
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True, "revoked": n}


# ---- 用户管理（admin）----
def _require_admin(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


@router.get("/users")
def list_users(request: Request):
    _require_admin(request)
    return {"users": users.list()}


@router.post("/users")
def create_user(body: UserCreateBody, request: Request):
    _require_admin(request)
    if len(body.password or "") < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")
    try:
        return users.create(body.username, body.password, role=body.role or "admin")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/users/{username}")
def delete_user(username: str, request: Request):
    me_ = _require_admin(request)
    if username == me_["username"]:
        raise HTTPException(status_code=400, detail="不能删除当前登录用户")
    try:
        ok = users.delete(username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"ok": True, "deleted": username}


@router.post("/users/{username}/reset")
def reset_password(username: str, body: ResetBody, request: Request):
    _require_admin(request)
    if len(body.new_password or "") < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")
    if not users.reset_password(username, body.new_password):
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"ok": True, "username": username}


@router.post("/users/{username}/disable")
def disable_user(username: str, request: Request):
    me_ = _require_admin(request)
    if username == me_["username"]:
        raise HTTPException(status_code=400, detail="不能禁用当前登录用户")
    if not users.set_disabled(username, True):
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"ok": True, "username": username, "disabled": True}


@router.post("/users/{username}/enable")
def enable_user(username: str, request: Request):
    _require_admin(request)
    if not users.set_disabled(username, False):
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"ok": True, "username": username, "disabled": False}
