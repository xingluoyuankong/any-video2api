# -*- coding: utf-8 -*-
"""前端交叉校验：导航/视图/ID 引用/路由函数。临时脚本。"""
import re
import pathlib

root = pathlib.Path(__file__).resolve().parents[1]
html = (root / "static" / "index.html").read_text(encoding="utf-8")
js = (root / "static" / "app.js").read_text(encoding="utf-8")

fail = []

# 1) 导航项 data-nav
navs = re.findall(r'data-nav="([\w-]+)"', html)
print("导航 data-nav (%d):" % len(navs), navs)
if len(navs) != 9:
    fail.append("导航项应为 9 个，实际 %d" % len(navs))
if len(set(navs)) != len(navs):
    fail.append("导航项有重复")

# 2) 视图容器 data-view + id=view-*
views = re.findall(r'data-view="([\w-]+)"', html)
print("视图 data-view (%d):" % len(views), views)
view_ids = set(re.findall(r'id="view-([\w-]+)"', html))
for n in navs:
    if n not in views:
        fail.append("导航 %s 缺少 #view-* 容器" % n)
    if n not in view_ids:
        fail.append("导航 %s 缺少 id=view-%s" % (n, n))

# 3) app.js ROUTES 与导航一致
routes_block = re.search(r"const ROUTES\s*=\s*\[([^\]]*)\]", js)
routes = re.findall(r"'([\w-]+)'", routes_block.group(1)) if routes_block else []
print("ROUTES (%d):" % len(routes), routes)
if routes != navs:
    fail.append("ROUTES 与导航不一致：%s vs %s" % (routes, navs))

# 4) 路由分支：每个 route 都有 case
for r in routes:
    if ("case '%s':" % r) not in js:
        fail.append("renderRoute 缺少 case '%s'" % r)

# 5) 每个 loadXxx 在 renderRoute 中被调用
load_fns = sorted(set(re.findall(r"function (load[A-Z]\w+)\(", js)))
print("load 函数:", load_fns)
route_body = js[js.index("function renderRoute()"):js.index("function renderRoute()") + 800]
for fn in load_fns:
    if (fn + "()") not in route_body:
        fail.append("load 函数 %s 未在 renderRoute 中调用" % fn)

# 6) JS 中引用的静态 id 必须在 HTML 中存在
html_ids = set(re.findall(r'id="([\w-]+)"', html))
refs = set(re.findall(r"\$\$?\(['\"]#([\w-]+)", js))
refs |= set(re.findall(r"getElementById\(['\"]([\w-]+)['\"]\)", js))
missing = sorted(r for r in refs if r not in html_ids)
print("JS 引用的 id 数:", len(refs))
if missing:
    fail.append("JS 引用但 HTML 缺失的 id: %s" % missing)

# 7) 新页面关键 id 存在
for need in ["usageStats", "usageDays", "usageChannel", "usageRefresh", "body-usage",
             "modelSearch", "modelReload", "modelProbe", "modelsRefresh", "modelProbeChannel", "body-models",
             "configReload", "configSave", "body-config",
             "acctCheckAll", "acctCheckinAll", "acctKeepaliveAll", "acctBalanceAll",
             "logChips", "logStats", "body-logs"]:
    if need not in html_ids:
        fail.append("缺少关键 id: %s" % need)

print("\n== 结果 ==")
if fail:
    for f in fail:
        print("FAIL:", f)
    raise SystemExit(1)
print("全部校验通过 ✓")
