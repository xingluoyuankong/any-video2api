# -*- coding: utf-8 -*-
"""一次性 state 校验的实测（fastapi TestClient）。

覆盖 5 项硬指标 + 附加用例：
  1) 无 state                     -> 400
  2) 伪造 state                   -> 400
  3) 正常流程（login_oauth 签发）  -> 200 且建号成功
  4) 同一 state 用两次            -> 第二次 400（一次性防重放）
  5) 超长 cookie                  -> 400
  附加：非法 JSON / 未知渠道 / 跨渠道 state / 条目超 500

运行：
  cd E:/API管理工具/any-video2api
  PYTHONPATH=. python tests/test_oauth_state.py

说明：用临时配置 + 临时 DB（AV2A_CONFIG 指向 tmp 里的 toml），
不触碰真实 data/unified.db；用 `TestClient(app)`（不进 with）跳过 lifespan，
避免启动 worker/保活线程和重建管理员。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ---- 临时配置/DB，必须在 import 项目模块之前设置（config 是 import 期单例）----
TMP = Path(tempfile.mkdtemp(prefix="av2a_oauth_test_"))
CFG = TMP / "setting.toml"
CFG.write_text(
    "[server]\n"
    "port = 8800\n"
    "public_base = 'http://127.0.0.1:8800'\n"
    "[storage]\n"
    f"database = '{TMP / 'test.db'}'\n"
    f"media_dir = '{TMP / 'media'}'\n"
    f"profile_dir = '{TMP / 'profiles'}'\n"
    "[session]\n"
    "enabled = false\n",
    encoding="utf-8")
os.environ["AV2A_CONFIG"] = str(CFG)

from fastapi.testclient import TestClient  # noqa: E402

import main as app_main  # noqa: E402
from src.channels.registry import registry  # noqa: E402
from src.platform import oauth_state  # noqa: E402
from src.platform.accounts import manager  # noqa: E402
from src.platform.keys import keys  # noqa: E402

client = TestClient(app_main.app)
API_KEY = keys.create("pytest-oauth")["api_key"]
AUTH = {"X-API-Key": API_KEY}

CH = "jimeng"
OK = []


def show(no, name, resp, expect, extra=""):
    code = resp.status_code
    try:
        detail = (resp.json() or {}).get("detail", "")
    except Exception:  # noqa: BLE001
        detail = ""
    passed = code == expect
    OK.append(passed)
    print(f"[{no}] {name}\n    expect={expect}  actual={code}  {'PASS' if passed else 'FAIL'}"
          f"{('  detail=' + str(detail)) if detail else ''}{extra}")


def post(body, raw=None):
    if raw is not None:
        return client.post("/oauth/callback", content=raw)
    return client.post("/oauth/callback", json=body)


def get_state(channel=CH):
    """经鉴权接口签发 state（模拟控制台点击）。"""
    r = client.get(f"/api/accounts/login/oauth?channel={channel}", headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


print("=" * 70)
print("渠道:", registry.names())
print("渠道错误:", registry.errors())
print("=" * 70)

# ---------- 0) 前置：确认签发接口返回 state ----------
d0 = get_state()
state0 = d0.get("state", "")
print(f"[0] GET /api/accounts/login/oauth -> 200, state={'有' if state0 else '无'}"
      f"(len={len(state0)}), bookmarklet 带 state={'state:' in d0.get('bookmarklet','')}")
OK.append(bool(state0) and "state:" in d0.get("bookmarklet", ""))
# 直接调用渠道对象的 login_oauth() 也要能拿到 state
d0b = registry.get(CH).login_oauth()
state_direct = d0b.get("state", "")
print(f"    直接 ch.login_oauth() -> state={'有' if state_direct else '无'}")
OK.append(bool(state_direct))

COOKIE = "sessionid=abc123; passport_csrf_token=def456; uid=10086"

# ---------- 1) 无 state ----------
r1 = post({"channel": CH, "cookie": COOKIE})
show(1, "无 state（CSRF 攻击面）", r1, 400)

# ---------- 2) 伪造 state ----------
r2 = post({"channel": CH, "state": "forged-state-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
           "cookie": COOKIE})
show(2, "伪造 state", r2, 400)

# ---------- 3) 正常流程 ----------
before = len(manager.list(CH))
s3 = get_state()["state"]
payload = {"channel": CH, "state": s3, "cookie": COOKIE}
r3 = post(payload)
acc = (r3.json() or {}).get("account") or {}
after = len(manager.list(CH))
show(3, "正常流程（login_oauth 签发 state -> POST）", r3, 200,
     f"\n    建号 id={acc.get('id')}  账号数 {before} -> {after}")
OK.append(after == before + 1)

# ---------- 4) 同一 state 用两次（重放）----------
r4 = post(payload)
show(4, "同一 state 第二次（防重放）", r4, 400)
OK.append(len(manager.list(CH)) == after)

# ---------- 5) 超长 cookie ----------
s5 = get_state()["state"]
r5 = post({"channel": CH, "state": s5, "cookie": "a=" + "x" * 40000})
show(5, "超长 cookie（>32KB）", r5, 400)

# ---------- 附加 A：非法 JSON ----------
rA = post(None, raw=b"channel=jimeng&cookie=abc")
show("A", "非法 JSON（纯文本 form）", rA, 400)

# ---------- 附加 B：未知渠道 ----------
sB = get_state()["state"]
rB = post({"channel": "not_a_channel", "state": sB, "cookie": COOKIE})
show("B", "未知渠道（白名单）", rB, 404)

# ---------- 附加 C：跨渠道 state 复用 ----------
sC = get_state("jimeng")["state"]
rC = post({"channel": "kling", "state": sC, "cookie": COOKIE})
show("C", "state 与 body.channel 不一致（跨渠道注入）", rC, 400)

# ---------- 附加 D：cookie 条目 > 500 ----------
sD = get_state()["state"]
big = json.dumps({f"k{i}": "v" for i in range(501)})
rD = post({"channel": CH, "state": sD, "cookie": big})
show("D", "cookie 条目 501（>500）", rD, 400)

# ---------- 附加 E：正常流程仍能建号（kling，证明不是全拦）----------
beforeE = len(manager.list("kling"))
rE = post({"channel": "kling", "state": get_state("kling")["state"], "cookie": COOKIE})
show("E", "正常流程（kling 渠道）", rE, 200,
     f"\n    账号数 {beforeE} -> {len(manager.list('kling'))}")

# ---------- 附加 F：TTL 过期 ----------
sF = oauth_state.issue(CH, ttl=0.01)
import time  # noqa: E402
time.sleep(0.05)
rF = post({"channel": CH, "state": sF, "cookie": COOKIE})
show("F", "state 已过期（TTL）", rF, 400)

print("=" * 70)
print(f"存活 state 数: {oauth_state.pending()}")
print(f"结果: {sum(OK)}/{len(OK)} 通过")
print("=" * 70)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if all(OK) else 1)
