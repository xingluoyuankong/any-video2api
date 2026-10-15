# -*- coding: utf-8 -*-
"""OAuth 回调一次性 state 令牌（防 CSRF + 防重放）。

背景：`/oauth/callback`（POST）在 `src/platform/keys.py` 的 PUBLIC_PATHS 里，
属于**免鉴权**端点。浏览器书签要把登录态从渠道官网跨域回传到这里，所以不能简单地
给它加 API Key / 会话校验（浏览器 fetch 带不上控制台的凭据）。

因此用「一次性 state」把信任链收敛为：
    已登录控制台的用户  ->  GET /api/accounts/login/oauth（**需鉴权**）签发 state
                        ->  书签带着 state 从官网跨域 POST 回来
                        ->  本模块 consume() 校验并**立即删除**

攻击者拿不到 state（签发接口要鉴权），因此：
  * 无法构造恶意页面 CSRF 写入任意 cookie（没有 state -> 400）
  * 无法刷接口无限建号（每次建号必须先消费一个已签发的 state）
  * state 是一次性的，抓到的回调请求无法重放

部署限制（重要）：
    state 只保存在**当前进程的内存字典**里，不落库、不跨进程共享。
    本项目以单进程 uvicorn 部署（`main.py` 单 app、无 --workers），可接受此限制。
    若改成多进程（uvicorn --workers>1 / gunicorn -w>1）或跨机多实例部署，
    A 进程签发的 state 落在 B 进程回调时会判为「已过期或无效」，
    必须换成共享存储实现（DB 表 / Redis，带 TTL + 原子删除），
    接口签名（issue / consume / gc）保持不变即可替换。
"""
from __future__ import annotations

import secrets
import threading
import time
from typing import Dict, Optional

__all__ = ["oauth_state", "issue", "consume", "gc", "pending"]

# 内存表：state -> {"channel": str, "exp": float}
_lock = threading.RLock()
_states: Dict[str, Dict[str, object]] = {}

#: 内存表上限（防止签发端被刷导致无限增长；超出后按 exp 从旧到新淘汰）
MAX_ENTRIES = 2000


def gc() -> int:
    """清掉过期项；仍超过 MAX_ENTRIES 时再淘汰最旧的一批。返回清理条数。"""
    nowt = time.time()
    removed = 0
    with _lock:
        for k in [k for k, v in _states.items() if float(v.get("exp", 0.0)) <= nowt]:
            _states.pop(k, None)
            removed += 1
        over = len(_states) - MAX_ENTRIES
        if over > 0:
            # 按过期时间升序淘汰最旧的 over 条（exp 相同则任意，不影响正确性）
            for k, _ in sorted(_states.items(), key=lambda kv: float(kv[1].get("exp", 0.0)))[:over]:
                _states.pop(k, None)
                removed += 1
    return removed


def issue(channel: str, ttl: float = 600.0) -> str:
    """为指定渠道签发一个一次性 state，ttl 秒后失效（默认 10 分钟）。"""
    gc()
    with _lock:
        state = secrets.token_urlsafe(32)
        # 32 字节随机量，碰撞概率可忽略；仍做一次防御性去重
        while state in _states:
            state = secrets.token_urlsafe(32)
        _states[state] = {"channel": channel, "exp": time.time() + float(ttl)}
        return state


def consume(state: Optional[str]) -> Optional[str]:
    """校验 state 并**立即删除**（一次性）。

    返回签发时绑定的 channel；state 为空 / 不存在 / 已过期 -> None。
    """
    if not state or not isinstance(state, str):
        return None
    gc()
    with _lock:
        item = _states.pop(state, None)      # 一次性：无论后续校验结果都先删掉
    if not item:
        return None
    if float(item.get("exp", 0.0)) <= time.time():
        return None
    return str(item.get("channel", ""))


def pending() -> int:
    """当前存活的 state 数量（诊断用）。"""
    gc()
    with _lock:
        return len(_states)


# 兼容 `from src.platform.oauth_state import oauth_state` 的模块式用法（见下方别名）
class _OAuthState:
    issue = staticmethod(issue)
    consume = staticmethod(consume)
    gc = staticmethod(gc)
    pending = staticmethod(pending)


oauth_state = _OAuthState()
