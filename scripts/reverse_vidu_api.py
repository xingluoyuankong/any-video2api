# -*- coding: utf-8 -*-
"""vidu 登录态接口逆向脚本（登录成功后跑）。

探测顺序：iam 用户类 → credit 额度类 → ent 生成类 → task 查询类。
TLS1.3-only 是必需的（绕腾讯 EdgeOne）。
"""
import io
import json
import time

import urllib3

urllib3.disable_warnings()

import sys  # noqa: E402

sys.path.insert(0, "/opt/any-video2api")
from src.channels.registry import registry  # noqa: E402
from src.platform.accounts import manager  # noqa: E402

BASE = "https://service.vidu.cn"

CANDIDATES = [
    # --- 用户/认证 ---
    ("GET", "/iam/v1/users/me"),
    ("GET", "/iam/v1/users/info"),
    ("GET", "/iam/v1/users/profile"),
    ("GET", "/iam/v1/users/every-day-credit"),
    ("GET", "/iam/v1/users/credit"),
    # --- 额度 ---
    ("GET", "/credit/v1/credits/me"),
    ("GET", "/credit/v1/credits/detail"),
    ("GET", "/credit/v1/credits/transactions"),
    ("GET", "/credit/v1/credits/history"),
    # --- 任务/生成（只读查询）---
    ("GET", "/ent/v2/tasks"),
    ("GET", "/ent/v2/task/list"),
    ("GET", "/ent/v2/tasks/mine"),
    # --- 订阅/权益 ---
    ("GET", "/iam/v1/users/subscription"),
    ("GET", "/iam/v1/users/vip"),
    ("GET", "/iam/v1/users/privilege"),
]

ch = registry.get("vidu")
accs = [a for a in manager.raws() if a["channel"] == "vidu"]
if not accs:
    print("NO VIDU ACCOUNT —— 先登录"); raise SystemExit
secret = manager.secret(accs[0]["id"])
cookie = secret.extra.get("cookie") or ch._cookie_of(secret)
print("账号:", accs[0]["id"], "cookie 长度:", len(cookie))

s = ch._tls13_only_session()
h = {**ch._headers(), "Cookie": cookie}

rows = []
for method, path in CANDIDATES:
    try:
        r = s.request(method, BASE + path, headers=h, timeout=20)
        try:
            j = r.json()
            keys = ",".join(sorted((j.get("data") or j).keys())[:8]) if isinstance(j.get("data") if isinstance(j, dict) else None, dict) else str(j)[:100]
            body = json.dumps(j, ensure_ascii=False)[:200]
        except Exception:  # noqa: BLE001
            keys, body = "-", (r.text or "")[:100]
        rows.append((path, r.status_code, keys))
        print("%-6s %-42s %s  %s" % (method, path, r.status_code, body[:100]))
    except Exception as e:  # noqa: BLE001
        rows.append((path, "EXC", type(e).__name__))
        print("%-6s %-42s EXC  %s" % (method, path, type(e).__name__))
    time.sleep(0.15)

ok = [r for r in rows if r[1] == 200]
print("\n=== 结果：探测 %d，HTTP 200 %d ===" % (len(rows), len(ok)))
io.open("/opt/any-video2api/scripts/vidu_api_map.txt", "w", encoding="utf-8").write(
    "\n".join("%s\t%s\t%s" % r for r in rows) + "\n")
print("已存 scripts/vidu_api_map.txt")
