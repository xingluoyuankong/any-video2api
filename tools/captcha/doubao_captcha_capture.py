# -*- coding: utf-8 -*-
import json, asyncio
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
        await pg.wait_for_timeout(1500)
        await pg.locator(".send-btn-wrapper button").first.click(timeout=6000)
        await pg.wait_for_timeout(12000)
        fr = None
        for f in pg.frames:
            if 'rmc.bytedance.com' in (f.url or ''):
                fr = f; break
        if not fr:
            print("NO_CAPTCHA"); await b.close(); return
        # 等图片加载
        await pg.wait_for_timeout(4000)
        q = await fr.evaluate("() => document.body.innerText.split('\\n').filter(x=>x.trim()).slice(0,3).join(' | ')")
        print("题目:", q[:100])
        # 截整个 iframe
        el = await fr.frame_element()
        await el.screenshot(path="/tmp/cap_full.png")
        # 每个 canvas 单独截图
        n = await fr.locator(".canvas-container").count()
        print("格子数:", n)
        for i in range(n):
            try:
                await fr.locator(".canvas-container").nth(i).screenshot(path="/tmp/cap_cell%d.png" % (i+1))
            except Exception as e:
                print("cell%d 截图失败: %s" % (i+1, str(e)[:50]))
        # 坐标（iframe 内）
        geo = await fr.evaluate("""() => {
            const da = document.querySelector('.drag-area');
            const r = da.getBoundingClientRect();
            const cells = Array.from(document.querySelectorAll('.canvas-container')).map(c => {
                const b = c.getBoundingClientRect();
                return {x: Math.round(b.x + b.width/2), y: Math.round(b.y + b.height/2)};
            });
            const fe = document.querySelector('iframe');
            return {drag: {x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2), top: Math.round(r.y), h: Math.round(r.height)},
                    cells, frameRect: null};
        }""")
        print("几何:", json.dumps(geo, ensure_ascii=False))
        await pg.screenshot(path="/tmp/cap_page.png")
        await b.close()

asyncio.run(main())
