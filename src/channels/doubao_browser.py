# -*- coding: utf-8 -*-
"""豆包浏览器执行器渠道（doubao_browser）。

背景（2026-10-16 实测）：
  豆包网页版已从 samantha SSE 改为 **IM 协议**（/im/message/*），
  API 直连（channels/doubao.py）必被风控（710022004，
  error_detail 为 shark image_select 图片点选验证码）。
  唯一可行路径：**真实浏览器执行**——服务器已装 playwright + chromium。

本渠道用 playwright 驱动 doubao.com/chat：
  submit：注入 Cookie（+反自动化检测）→ 输入 prompt → 点发送按钮
  poll  ：重新打开会话 → 在最近对话里找该 prompt 的回复 → 提取视频 URL

关键实测细节：
  * 反检测必需：--disable-blink-features=AutomationControlled
    + navigator.webdriver=undefined + window.chrome
  * 发送必须**点按钮**（.send-btn-wrapper，右下角），Enter 常无效
  * 验证码（rmc.bytedance.com iframe 图片点选）低频时不一定弹；
    若弹出会打印提示，需人工/多模态破解（未实现自动破解）
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import time
from typing import Any, Dict, List, Optional

from src.channels.base import (AccountSecret, Channel, ChannelSpec, GenerateRequest,
                               PollResult, SubmitResult, VerifyResult)
from src.core.errors import redact

# 反自动化检测脚本（实测：注入后 navigator.webdriver 变 None）
_STEALTH = (
    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    "window.chrome={runtime:{},loadTimes:function(){},csi:function(){},app:{}};"
    "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
    "Object.defineProperty(navigator,'languages',{get:()=>['zh-CN','zh','en']});"
    "Object.defineProperty(navigator,'hardwareConcurrency',{get:()=>8});"
    "Object.defineProperty(navigator,'deviceMemory',{get:()=>16});"
)

_CHAT_URL = "https://www.doubao.com/chat/"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36")


class DoubaoBrowserChannel(Channel):
    """豆包（网页版，浏览器执行器）。"""

    def __init__(self, spec: ChannelSpec, log=None):
        super().__init__(spec, log)
        self._profile_dir = os.path.join(tempfile.gettempdir(), "doubao_pw_profile")

    # ---- 内部：playwright 上下文 --------------------------------------
    def _cookies_of(self, secret: AccountSecret) -> List[dict]:
        out: List[dict] = []
        for dom in (".doubao.com", "doubao.com"):
            for k, v in (secret.cookies or {}).items():
                if str(k).startswith("__"):
                    continue
                out.append({"name": str(k), "value": str(v), "domain": dom, "path": "/"})
        return out

    @staticmethod
    def _task_id(prompt: str) -> str:
        return "db_" + hashlib.md5(prompt.encode("utf-8")).hexdigest()[:12]

    # ---- verify --------------------------------------------------------
    def verify(self, secret: AccountSecret) -> VerifyResult:
        if not (secret.cookies or {}).get("sessionid"):
            return VerifyResult(ok=False, status="invalid",
                                message="缺少凭据：需要豆包 Cookie（sessionid）")
        # 豆包无公开 whoami；以「浏览器能打开且登录态存在」为准
        try:
            ok, detail = asyncio.run(self._check_login(secret))
        except Exception as exc:  # noqa: BLE001
            return VerifyResult(ok=False, status="risk",
                                message="浏览器校验异常，暂不判定失效：%s" % str(exc)[:80])
        if ok:
            return VerifyResult(ok=True, status="active", message=detail)
        return VerifyResult(ok=False, status="risk", message=detail)

    async def _check_login(self, secret: AccountSecret) -> tuple:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            b = await p.chromium.launch(headless=True, args=[
                "--no-sandbox", "--disable-blink-features=AutomationControlled"])
            ctx = await b.new_context(user_agent=_UA, viewport={"width": 1440, "height": 900},
                                      locale="zh-CN")
            await ctx.add_init_script(_STEALTH)
            await ctx.add_cookies(self._cookies_of(secret))
            pg = await ctx.new_page()
            try:
                await pg.goto(_CHAT_URL, wait_until="domcontentloaded", timeout=40000)
                await pg.wait_for_timeout(6000)
                txt = await pg.evaluate("() => document.body.innerText.slice(0, 3000)")
                logged = ("退出" in txt) or ("用户" in txt) or ("新对话" in txt)
                nick = ""
                m = re.search(r"用户\s*(\d+)", txt)
                if m:
                    nick = m.group(0)
                await b.close()
                return bool(logged), ("豆包浏览器登录态有效 %s" % nick).strip()
            except Exception as exc:  # noqa: BLE001
                await b.close()
                return False, "浏览器打开失败：%s" % str(exc)[:80]

    # ---- submit --------------------------------------------------------
    def submit(self, secret: AccountSecret, req: GenerateRequest) -> SubmitResult:
        if not (secret.cookies or {}).get("sessionid"):
            return SubmitResult(accepted=False, error="缺少豆包 Cookie（sessionid）")
        prompt = req.prompt or ""
        try:
            res = asyncio.run(self._send(secret, prompt))
        except Exception as exc:  # noqa: BLE001
            return SubmitResult(accepted=False, error="浏览器执行异常：%s" % str(exc)[:150])
        if not res.get("sent"):
            return SubmitResult(accepted=False, error=res.get("error") or "消息发送失败", raw=res)
        return SubmitResult(upstream_task_id=self._task_id(prompt), transport="browser",
                            raw={"note": res.get("note", "")})

    async def _send(self, secret: AccountSecret, prompt: str) -> Dict[str, Any]:
        from playwright.async_api import async_playwright
        out: Dict[str, Any] = {"sent": False, "error": "", "note": ""}
        async with async_playwright() as p:
            b = await p.chromium.launch(headless=True, args=[
                "--no-sandbox", "--disable-blink-features=AutomationControlled"])
            ctx = await b.new_context(user_agent=_UA, viewport={"width": 1440, "height": 900},
                                      locale="zh-CN")
            await ctx.add_init_script(_STEALTH)
            await ctx.add_cookies(self._cookies_of(secret))
            pg = await ctx.new_page()
            try:
                await pg.goto(_CHAT_URL, wait_until="domcontentloaded", timeout=45000)
                await pg.wait_for_timeout(9000)
                box = pg.locator("textarea, [contenteditable='true']").first
                await box.click()
                await box.type(prompt, delay=30)
                await pg.wait_for_timeout(1500)
                # 点发送按钮（.send-btn-wrapper 内的 button）
                btn = pg.locator(".send-btn-wrapper button").first
                try:
                    await btn.click(timeout=6000)
                except Exception:
                    # 兜底：右下角坐标点击
                    await pg.keyboard.press("Enter")
                await pg.wait_for_timeout(7000)
                txt = await pg.evaluate("() => document.body.innerText")
                cap = "请选择所有符合" in txt
                out["sent"] = prompt[:12] in txt
                out["note"] = ("消息已发送" if out["sent"] else "未检测到消息入列")
                if cap:
                    out["error"] = "触发图片点选验证码，需人工/多模态处理"
                    out["sent"] = False
                await pg.screenshot(path="/tmp/doubao_browser_submit.png")
            except Exception as exc:  # noqa: BLE001
                out["error"] = str(exc)[:150]
            finally:
                await b.close()
        return out

    # ---- poll ----------------------------------------------------------
    def poll(self, secret: AccountSecret, upstream_task_id: str,
             req: Optional[GenerateRequest] = None) -> PollResult:
        prompt = (req.prompt if req else "") or ""
        try:
            res = asyncio.run(self._read(secret, prompt))
        except Exception as exc:  # noqa: BLE001
            return PollResult(status="processing", raw={"err": str(exc)[:120]})
        if res.get("captcha"):
            return PollResult(status="processing", raw={"note": "验证码未过，需处理"})
        if res.get("media"):
            return PollResult(status="success", media=res["media"],
                              covers=res.get("covers") or [], raw={"note": res.get("note", "")})
        if res.get("failed"):
            return PollResult(status="failed", error=res.get("note", "生成失败"))
        return PollResult(status="processing", raw={"note": res.get("note", "生成中")})

    async def _read(self, secret: AccountSecret, prompt: str) -> Dict[str, Any]:
        """打开最近会话，找 prompt 对应回复，提取视频 URL。"""
        from playwright.async_api import async_playwright
        out: Dict[str, Any] = {"media": [], "covers": [], "note": "", "captcha": False,
                               "failed": False}
        async with async_playwright() as p:
            b = await p.chromium.launch(headless=True, args=[
                "--no-sandbox", "--disable-blink-features=AutomationControlled"])
            ctx = await b.new_context(user_agent=_UA, viewport={"width": 1440, "height": 900},
                                      locale="zh-CN")
            await ctx.add_init_script(_STEALTH)
            await ctx.add_cookies(self._cookies_of(secret))
            pg = await ctx.new_page()
            try:
                await pg.goto(_CHAT_URL, wait_until="domcontentloaded", timeout=45000)
                await pg.wait_for_timeout(9000)
                # 点左侧"最近"第一条对话
                try:
                    await pg.locator("[class*=conversation], [class*=session], [class*=chat-item]").first.click(timeout=5000)
                    await pg.wait_for_timeout(6000)
                except Exception:
                    pass
                html = await pg.content()
                out["captcha"] = "请选择所有符合" in html
                urls = await pg.evaluate("""() => {
                    const out = {videos: [], covers: [], text: ''};
                    document.querySelectorAll('video').forEach(v => {
                        if (v.src && v.src.startsWith('http')) out.videos.push(v.src);
                        const s = v.querySelector('source');
                        if (s && s.src) out.videos.push(s.src);
                    });
                    document.querySelectorAll('img').forEach(im => {
                        if (im.src && (im.src.includes('video') || im.src.includes('cover'))) out.covers.push(im.src);
                    });
                    const t = document.body.innerText;
                    out.text = t.slice(-1500);
                    return out;
                }""")
                out["media"] = list(dict.fromkeys(urls["videos"]))[:5]
                out["covers"] = list(dict.fromkeys(urls["covers"]))[:3]
                txt = urls.get("text") or ""
                if "生成失败" in txt or "无法生成" in txt:
                    out["failed"] = True
                    out["note"] = "页面提示生成失败"
                elif out["media"]:
                    out["note"] = "已拿到视频直链"
                elif "生成中" in txt or "正在生成" in txt or "排队" in txt:
                    out["note"] = "生成中"
                else:
                    out["note"] = "未见视频元素"
                await pg.screenshot(path="/tmp/doubao_browser_poll.png")
            except Exception as exc:  # noqa: BLE001
                out["note"] = str(exc)[:120]
            finally:
                await b.close()
        return out

    # ---- 其它 ----------------------------------------------------------
    def build_headers(self, secret: AccountSecret, path: str, body: Any = None,
                      **kwargs) -> Dict[str, str]:
        """浏览器执行器不用 HTTP 头；保留接口以满足 Channel 抽象方法。"""
        return {"User-Agent": _UA, "Referer": _CHAT_URL}

    def credits(self, secret: AccountSecret):
        from src.channels.base import CreditView
        return CreditView(total=0, message="豆包网页版无公开额度端点（每日免费次数）")

    def classify_error(self, payload: Any) -> str:
        try:
            s = json.dumps(payload, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            return "upstream"
        if "验证码" in s or "verify" in s or "slide" in s:
            return "auth_or_risk"
        if "额度" in s or "次数" in s:
            return "quota"
        return "upstream"
