# -*- coding: utf-8 -*-
"""用海螺登录态批量探测全部接口（读 scripts/hailuo_paths.txt）。

安全策略：跳过写操作（generate/create/delete/pay/submit 等），只探测只读接口。
"""
import io
import time

import urllib3

urllib3.disable_warnings()

import sys  # noqa: E402

sys.path.insert(0, "/opt/any-video2api")
from src.channels.registry import registry  # noqa: E402
from src.platform.accounts import manager  # noqa: E402

PATHS_FILE = "/opt/any-video2api/scripts/hailuo_paths.txt"

UNSAFE = ("generate", "create", "delete", "pay", "submit", "cancel", "edit",
          "update", "appeal", "like", "post", "share", "report", "issue",
          "confirm", "character", "enhance", "activate", "bind", "ack",
          "unsubscribe", "reset", "change", "top", "pin", "bookmark",
          "batch_delete", "batch_download", "download", "policy_callback",
          "page_view", "statistics", "download")

paths = [l.strip() for l in io.open(PATHS_FILE, encoding="utf-8") if l.strip()]
paths = sorted(set(paths))

ch = registry.get("hailuo")
accs = [a for a in manager.raws() if a["channel"] == "hailuo"]
if not accs:
    print("NO ACCOUNT"); raise SystemExit
secret = manager.secret(accs[0]["id"])
tok = secret.extra.get("token", "")

rows = []
for p in paths:
    pl = p.lower()
    if any(u in pl for u in UNSAFE):
        rows.append((p, "SKIP(写)", ""))
        continue
    try:
        d = ch._get(p, token=tok)
        if isinstance(d, dict):
            keys = ",".join(sorted(d.keys())[:8])
        else:
            keys = type(d).__name__
        rows.append((p, "OK", keys[:160]))
    except RuntimeError as e:
        rows.append((p, "ERR", str(e)[:100]))
    except Exception as e:  # noqa: BLE001
        rows.append((p, "EXC", type(e).__name__))
    time.sleep(0.12)

ok = [r for r in rows if r[1] == "OK"]
skip = [r for r in rows if r[1].startswith("SKIP")]
bad = [r for r in rows if r[1] in ("ERR", "EXC")]

print("=== 海螺接口逆向（登录态 %s）===" % accs[0]["id"])
print("总计 %d ｜ 可用 %d ｜ 写操作跳过 %d ｜ 报错 %d\n" % (len(rows), len(ok), len(skip), len(bad)))

print("--- 可用接口（返回字段）---")
for p, _, info in ok:
    print("  %-62s %s" % (p, info))
print("\n--- 写操作（已跳过，未调用）---")
for p, _, _ in skip:
    print("  " + p)
if bad:
    print("\n--- 报错 ---")
    for p, st, info in bad:
        print("  %-62s %s %s" % (p, st, info))

io.open("/opt/any-video2api/scripts/hailuo_api_map.txt", "w", encoding="utf-8").write(
    "\n".join("%s\t%s\t%s" % r for r in rows) + "\n")
print("\n已存 scripts/hailuo_api_map.txt")
