# 豆包验证码破解工具（进度记录）

## 现状（2026-10-16）
字节风控验证码 `rmc.bytedance.com/verifycenter/captcha/v2`（iframe，图片点选拖拽）。

已打通：事件注入、拖拽浮层、放置区激活、坐标提取、图片截图。
未打通：**drop 判定**（拖拽释放后图片未进入放置区）。

## 脚本
- `doubao_captcha_capture.py`：发消息触发验证码 → 截图 9 格 + 输出几何坐标
- `doubao_captcha_solve.py`：传入答案索引（如 "9,8,6,4,3"）→ 拖拽 + 提交
- `doubao_captcha_drag.py`：带 active 状态监测的真实感轨迹拖拽（调试用）

## 关键坐标（iframe 内）
- 9 格中心：x ∈ {70,190,310}，y ∈ {130,250,370}
- 放置区 `.drag-area`：中心 (190,464)，top=437，h=54
- 拖拽浮层 `.drager`（88×88，z-index:1，**无 pointer-events:none → 遮挡 pointerup**）

## 已排除的原因
1. ❌ 坐标错误（已用 iframe 偏移精确换算）
2. ❌ dragger 遮挡（压制 pointer-events 后 target 正确变 tit/drag-area）
3. ❌ 轨迹太短（试过 25 步慢轨迹 + 停留 + 抖动）
4. ❌ Enter 发送（必须点 .send-btn-wrapper button）

## 破解进展（2026-10-17 凌晨更新）

### ✅ 本地交互 100% 破解
读 captcha.js（Svelte 编译）定位到核心逻辑：
```js
x = function(){                       // 释放处理
  var o = {result: m.active ? 1 : 0, track: d};
  m.active && (count++, n.selected = true, n.dragTrack = o, k.push(o), v("change", k));
  ...
}
```
- **`m.active` = 鼠标是否在 .drag-area 内**（DOM class `active` 同源）
- 选中后**图片不移走**，只加 `.canvas-container.selected` + badge 显示已选数量
- **验证：拖拽后 selected 计数正确递增（1→8）、badge 文本正确**

### ❌ 卡点：服务端行为判定
本地全部正确（selected/badge/提交动作）后点提交 → **服务端判定失败**（刷新新题）。
上报数据 `dragTrack = {result, track:[{x1,y1,x2,y2,relative_time:0,time}]}` → 服务端分析行为特征。

已试过全部失效：
1. headless（--disable-blink-features=AutomationControlled + webdriver 隐藏）
2. **有头模式 + xvfb**（WebGL vendor/renderer 伪造 + plugins 补全）
3. **人类化轨迹**（三次贝塞尔 + ease-out 变速 + 抖动 + 落点微调）
4. 答案人工确认正确（如"在城市中能看到的场所"→1,4,6,7,8,9）

### 下一步方案
1. **人工过验证 + cookie 复用**（最实用）：用户浏览器过一次验证码 → 导出
   cookie（字节验证通过后种 verify token）→ 服务器复用
2. 逆向 verify 上报接口（rmc.bytedance.com 的 POST，含加密 payload）
3. 换豆包 App 协议（不同风控策略）
4. 深度试错：事件序列补全（pointerover/enter/move 完整链）+ 时间戳随机化
