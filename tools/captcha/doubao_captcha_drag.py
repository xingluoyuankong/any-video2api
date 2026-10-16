# -*- coding: utf-8 -*-
import json, asyncio, math
from playwright.async_api import async_playwright

d = json.load(open('/tmp/dbcred.json', encoding='utf-8'))
UA = d['ua']
cookies = []
for dom in (".doubao.com", "doubao.com"):
    for k, v in d['cookies'].items():
        cookies.append({"name": k, "value": str(v), "domain": dom, "path": "/"})

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        ctx = await b.new_context(user_agent=UA, viewport={"width":1440,"height":900}, locale="zh-CN")
        await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});window.chrome={runtime:{}};")
        await ctx.add_cookies(cookies)
        pg = await ctx.new_page()
        await pg.goto("https://www.doubao.com/chat/", wait_until="domcontentloaded", timeout=45000)
        await pg.wait_for_timeout(9000)
        box = pg.locator("textarea, [contenteditable='true']").first
        await box.click()
        await box.type("生成视频：一只橘猫在草地上奔跑", delay=30)
        await pg.wait_for_timeout(1200)
        await pg.locator(".send-btn-wrapper button").first.click(timeout=6000)
        await pg.wait_for_timeout(13000)
        fr = None
        for f in pg.frames:
            if 'rmc.bytedance.com' in (f.url or ''):
                fr = f; break
        if not fr:
            print("NO_CAPTCHA"); await b.close(); return
        await pg.wait_for_timeout(4000)
        # 看 active 何时加/去（MutationObserver）
        await fr.evaluate("""() => {
            window.__act = [];
            const da = document.querySelector('.drag-area');
            new MutationObserver(() => window.__act.push(da.className + '@' + Date.now()%100000)).observe(da, {attributes:true, attributeFilter:['class']});
        }""")
        sbox = await fr.locator(".canvas-container").nth(0).bounding_box()
        dbox = await fr.locator(".drag-area").bounding_box()
        sx, sy = sbox["x"]+55, sbox["y"]+55
        dx, dy = dbox["x"]+dbox["width"]/2, dbox["y"]+dbox["height"]/2
        # 真实感轨迹：先慢速靠近，进入后停留
        await pg.mouse.move(sx, sy); await pg.wait_for_timeout(400)
        await pg.mouse.down(); await pg.wait_for_timeout(400)
        steps = 25
        for i in range(steps):
            t = (i+1)/steps
            # 加轻微曲线
            px = sx + (dx-sx)*t + math.sin(t*3.14)*6
            py = sy + (dy-sy)*t
            await pg.mouse.move(px, py)
            await pg.wait_for_timeout(45)
        # 在放置区中心停 1.5s，小幅抖动
        for k in range(8):
            await pg.mouse.move(dx + math.sin(k)*3, dy + math.cos(k)*2)
            await pg.wait_for_timeout(180)
        act = await fr.evaluate("() => window.__act.slice(0,10)")
        print("active 变化:", json.dumps(act, ensure_ascii=False))
        await pg.mouse.up()
        await pg.wait_for_timeout(1500)
        print("格子数:", await fr.locator(".canvas-container").count())
        kids = await fr.evaluate("() => Array.from(document.querySelector('.drag-area').children).map(c=>(c.className||'').toString().slice(0,30))")
        print("drag-area kids:", json.dumps(kids, ensure_ascii=False)[:200])
        await pg.screenshot(path="/tmp/dbslow.png")
        await b.close()

asyncio.run(main())
