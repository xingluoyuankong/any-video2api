# -*- coding: utf-8 -*-
import json, asyncio, sys
from playwright.async_api import async_playwright

d = json.load(open('/tmp/dbcred.json', encoding='utf-8'))
UA = d['ua']
cookies = []
for dom in (".doubao.com", "doubao.com"):
    for k, v in d['cookies'].items():
        cookies.append({"name": k, "value": str(v), "domain": dom, "path": "/"})

ANSWERS = [int(x) for x in (sys.argv[1] if len(sys.argv) > 1 else "9,8,6,4,3").split(",")]

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
            print("NO_CAPTCHA(可能直接通过)"); await b.close(); return
        await pg.wait_for_timeout(4000)
        # iframe 在主页面中的偏移
        fe = await fr.frame_element()
        fb = await fe.bounding_box()
        print("iframe 偏移:", fb)
        ox, oy = fb["x"], fb["y"]
        # 拖拽
        for idx in ANSWERS:
            cells = fr.locator(".canvas-container")
            n = await cells.count()
            if idx > n:
                print("idx 越界", idx, n); break
            sbox = await cells.nth(idx-1).bounding_box()
            dbox = await fr.locator(".drag-area").bounding_box()
            sx, sy = sbox["x"]+sbox["width"]/2, sbox["y"]+sbox["height"]/2
            dx, dy = dbox["x"]+dbox["width"]/2, dbox["y"]+dbox["height"]/2
            await pg.mouse.move(sx, sy)
            await pg.wait_for_timeout(150)
            await pg.mouse.down()
            await pg.wait_for_timeout(120)
            for st in range(12):
                await pg.mouse.move(sx+(dx-sx)*(st+1)/12, sy+(dy-sy)*(st+1)/12)
                await pg.wait_for_timeout(25)
            await pg.wait_for_timeout(350)
            await pg.mouse.move(dx+1, dy+1)
            await pg.wait_for_timeout(250)
            await pg.mouse.up()
            await pg.wait_for_timeout(700)
            print("拖 idx", idx, "剩余:", await fr.locator(".canvas-container").count(), flush=True)
        # 提交
        try:
            await fr.locator("text=提交").first.click(timeout=6000)
            print("已提交")
        except Exception as e:
            print("提交失败:", str(e)[:80])
        await pg.wait_for_timeout(9000)
        html = await pg.content()
        print("验证码仍在:", "请选择所有符合" in html)
        # 消息是否发出（红色感叹号 = 被拦）
        st = await pg.evaluate("""() => {
            const t = document.body.innerText;
            return {hasPrompt: t.includes('橘猫'), failed: t.includes('失败') || t.includes('重试')};
        }""")
        print("状态:", json.dumps(st, ensure_ascii=False))
        await pg.screenshot(path="/tmp/dbsolve.png")
        await b.close()

asyncio.run(main())
