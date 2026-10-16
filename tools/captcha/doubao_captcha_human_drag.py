# -*- coding: utf-8 -*-
"""豆包验证码闭环 v2：人类化轨迹（贝塞尔+变速+抖动）"""
import json, asyncio, os, random, math
from playwright.async_api import async_playwright

d = json.load(open('/tmp/dbcred.json', encoding='utf-8'))
UA = d['ua']
cookies = []
for dom in (".doubao.com", "doubao.com"):
    for k, v in d['cookies'].items():
        cookies.append({"name": k, "value": str(v), "domain": dom, "path": "/"})

ANS_FILE = '/tmp/db_answer.txt'
PROMPT = "生成视频：一只橘猫在草地上奔跑"

async def human_drag(pg, sx, sy, dx, dy):
    """人类化拖拽：贝塞尔曲线 + ease-out 变速 + 抖动"""
    # 控制点（随机偏移，制造弧线）
    cx1 = sx + (dx-sx)*0.3 + random.uniform(-25, 25)
    cy1 = sy + (dy-sy)*0.3 + random.uniform(-25, 25)
    cx2 = sx + (dx-sx)*0.7 + random.uniform(-20, 20)
    cy2 = sy + (dy-sy)*0.7 + random.uniform(-20, 20)
    steps = random.randint(18, 28)
    await pg.mouse.move(sx, sy)
    await pg.wait_for_timeout(random.randint(120, 260))
    await pg.mouse.down()
    await pg.wait_for_timeout(random.randint(80, 180))
    for i in range(1, steps+1):
        t = i/steps
        # ease-out 速度曲线
        et = 1 - (1-t)**2
        # 三次贝塞尔
        x = ((1-et)**3)*sx + 3*((1-et)**2)*et*cx1 + 3*(1-et)*(et**2)*cx2 + (et**3)*dx
        y = ((1-et)**3)*sy + 3*((1-et)**2)*et*cy1 + 3*(1-et)*(et**2)*cy2 + (et**3)*dy
        # 抖动
        x += random.uniform(-1.5, 1.5)
        y += random.uniform(-1.5, 1.5)
        await pg.mouse.move(x, y)
        # 变速延迟（前快后慢 + 随机）
        delay = max(8, int(45 * (1 - t*0.6)) + random.randint(-8, 18))
        await pg.wait_for_timeout(delay)
    # 落点微调
    await pg.mouse.move(dx + random.uniform(-3, 3), dy + random.uniform(-3, 3))
    await pg.wait_for_timeout(random.randint(200, 500))
    await pg.mouse.up()

async def main():
    if os.path.exists(ANS_FILE):
        os.remove(ANS_FILE)
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
        await box.type(PROMPT, delay=random.randint(25, 45))
        await pg.wait_for_timeout(random.randint(1000, 2000))
        await pg.locator(".send-btn-wrapper button").first.click(timeout=6000)
        await pg.wait_for_timeout(13000)
        fr = None
        for f in pg.frames:
            if 'rmc.bytedance.com' in (f.url or ''):
                fr = f; break
        if not fr:
            print("NO_CAPTCHA"); await b.close(); return
        await pg.wait_for_timeout(5000)
        q = await fr.evaluate("() => document.body.innerText.split('\\n').filter(x=>x.trim())[0]")
        print("题目:", q, flush=True)
        el = await fr.frame_element()
        await el.screenshot(path="/tmp/dbfinal_cap.png")
        n = await fr.locator(".canvas-container").count()
        for i in range(n):
            try:
                await fr.locator(".canvas-container").nth(i).screenshot(path="/tmp/dbfinal_cell%d.png" % (i+1))
            except Exception:
                pass
        print("已截图 %d 格，等待答案..." % n, flush=True)
        ans = None
        for _ in range(300):
            if os.path.exists(ANS_FILE):
                ans = open(ANS_FILE).read().strip(); break
            await pg.wait_for_timeout(1000)
        if not ans:
            print("超时"); await b.close(); return
        print("答案:", ans, flush=True)
        idxs = sorted([int(x) for x in ans.replace("，", ",").split(",") if x.strip().isdigit()], reverse=True)
        for idx in idxs:
            cells = fr.locator(".canvas-container")
            if idx > await cells.count():
                continue
            sbox = await cells.nth(idx-1).bounding_box()
            dbox = await fr.locator(".drag-area").bounding_box()
            await human_drag(pg,
                             sbox["x"]+sbox["width"]/2, sbox["y"]+sbox["height"]/2,
                             dbox["x"]+dbox["width"]/2, dbox["y"]+dbox["height"]/2)
            await pg.wait_for_timeout(random.randint(400, 800))
            print("拖 %d → selected=%d" % (idx, await fr.locator(".canvas-container.selected").count()), flush=True)
        await fr.locator("text=提交").first.click(timeout=6000)
        print("已提交", flush=True)
        await pg.wait_for_timeout(10000)
        html = await pg.content()
        print("验证码仍在:", "请选择所有符合" in html, flush=True)
        await pg.screenshot(path="/tmp/dbfinal_after.png")
        await b.close()

asyncio.run(main())
