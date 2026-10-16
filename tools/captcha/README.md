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

## 下一步
- 读 `captcha.js`（813KB，rmc-captcha/1.0.0.951）定位 drop 判定状态机
  （pointerdown/up 监听绑定在 `i.u[6][1]` 元素上，捕获模式；状态在 `o.e/o.f/o.k/i.A.u[4353]`）
- 或尝试 CDP `Input.dispatchDragEvent`（真 HTML5 拖拽）
- 或逆向验证接口（verify 上报 payload 结构）
