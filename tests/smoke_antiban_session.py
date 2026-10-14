# -*- coding: utf-8 -*-
"""防风控 + 会话保活 集成冒烟测试（零上游请求）。

运行：
    cd E:/API管理工具/any-video2api
    PYTHONPATH=. python tests/smoke_antiban_session.py
"""
from __future__ import annotations

import sqlite3
import sys
import time

from fastapi.testclient import TestClient

import main
from src.platform.antiban import circuit
from src.platform.transport import HttpClient

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    if not cond:
        FAILED.append(name)
    print(f"  [{mark}] {name}" + (f"  {detail}" if detail else ""))


def main_test() -> int:
    with TestClient(main.app) as c:
        key = sqlite3.connect("data/unified.db").execute(
            "select secret_plain from api_keys limit 1").fetchone()[0]
        h = {"X-API-Key": key}

        print("\n== 1. 导入带 expires 的账号 ==")
        exp = time.time() + 7200
        acc = c.post("/api/accounts/import", headers=h, json={
            "channel": "coze", "name": "opt-smoke",
            "cookies": [{"name": "sessionid", "value": "x", "expires": exp},
                        {"name": "sid_guard", "value": "y", "expires": -1}],
            "oauth": {"access_token": "t", "token_expires_at": time.time() + 1800},
        }).json()
        aid = acc["id"]
        check("账号导入成功", bool(aid), aid)
        check("凭据已保存", acc.get("has_cookies") is True)

        print("\n== 2. 设备指纹一致性（防风控核心）==")
        s1 = c.get(f"/api/accounts/{aid}/session", headers=h).json()
        s2 = c.get(f"/api/accounts/{aid}/session", headers=h).json()
        fp1, fp2 = s1["fingerprint"], s2["fingerprint"]
        check("指纹稳定（两次一致）", fp1 == fp2)
        check("指纹含 UA/时区/分辨率",
              all(fp1.get(k) for k in ("user_agent", "timezone", "screen")))

        print("\n== 3. 会话健康与过期解析 ==")
        check("health=healthy", s1["health"] == "healthy", s1["health"])
        check("expires_in 接近 7200", 7000 < (s1["expires_in"] or 0) <= 7200,
              str(round(s1["expires_in"] or 0)))
        check("oauth 过期已识别", s1.get("oauth_expires_at") is not None)

        print("\n== 4. 多凭据冗余备份 ==")
        bks = c.get(f"/api/accounts/{aid}/backups", headers=h).json()["backups"]
        check("存在自动快照", len(bks) >= 1, f"{len(bks)} 份")
        r = c.post(f"/api/accounts/{aid}/restore", headers=h).json()
        check("恢复成功", r.get("restored") is True)
        after = c.get(f"/api/accounts/{aid}/session", headers=h).json()
        check("恢复后 expires 仍可解析", after.get("expires_at") is not None)

        print("\n== 5. 指纹轮换 ==")
        nf = c.post(f"/api/accounts/{aid}/fingerprint/rotate", headers=h).json()["fingerprint"]
        check("轮换后 device_id 变化", nf["device_id"] != fp1["device_id"])

        print("\n== 6. 防风控状态接口 ==")
        st = c.get("/api/antiban/status", headers=h).json()
        check("status 含 rate_limiter/circuit/proxy_bindings",
              all(k in st for k in ("rate_limiter", "circuit", "proxy_bindings")))

        print("\n== 7. 熔断器拦截（不发上游）==")
        for _ in range(3):
            circuit.record("coze:acc_circuit", "auth_or_risk")
        check("连续风控错误 -> open",
              circuit.state("coze:acc_circuit")["state"] == "open")
        blocked = False
        try:
            HttpClient(base_url="https://example.invalid", channel="coze",
                       account_id="acc_circuit").request("GET", "/x")
        except Exception as e:
            blocked = "antiban blocked" in str(e)
        check("熔断打开时请求被拦截", blocked)
        circuit.record("coze:acc_circuit", "success")
        check("成功后熔断复位",
              circuit.state("coze:acc_circuit")["state"] == "closed")

        print("\n== 8. 会话汇总 & 保活调度 ==")
        check("sessions 汇总可读", c.get("/api/sessions", headers=h).json()["total"] >= 1)
        c.delete(f"/api/accounts/{aid}", headers=h)
        ka = c.post("/api/keepalive/run", headers=h).json()
        check("保活 run_once 返回计数", all(k in ka for k in ("checked", "ok", "failed", "skipped")),
              str(ka))

    print("\n" + ("=" * 40))
    if FAILED:
        print(f"FAILED: {len(FAILED)} -> {FAILED}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main_test())
