# 豆包（Doubao）渠道

逆向自豆包**网页版**（`www.doubao.com`），协议走纯 HTTP，**无 `a_bogus` / `X-Bogus` 签名**。

> 参考：[wangchuxiaoji-oss/doubao2api](https://github.com/wangchuxiaoji-oss/doubao2api)（逆向豆包网页端）。

## 1. 鉴权

| 项 | 值 |
|---|---|
| 凭据 | Cookie：`sessionid`、`ttwid`、`passport_csrf_token` |
| 设备指纹 | URL query：`msToken`、`fp`、`device_id`、`web_id`、`aid=582478` |
| CSRF 头 | `x-tt-passport-csrf-token` |
| 签名 | **无**（a_bogus 仅浏览器 JSSDK 注入，纯 HTTP 直连不需要） |

导入账号：浏览器登录 `www.doubao.com` → F12 → Application → Cookies，复制上述 cookie 为 JSON，
在控制台「账号管理 → 豆包 → 导入账号」粘贴，或调 `POST /api/accounts/import`：

```bash
curl -X POST http://127.0.0.1:8800/api/accounts/import \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"channel":"doubao","name":"doubao-1","cookies":{"sessionid":"...","ttwid":"...","passport_csrf_token":"..."}}'
```

## 2. 上游接口

| 步骤 | 方法 | 路径 | 说明 |
|---|---|---|---|
| 提交 | POST | `/samantha/chat/completion` | body `messages[0].content_type=2020`、`skill.skill_type=17`、`content=json{text,ratio,camera_movement}`；SSE 中 `fin_reason.async_task.id` 为任务 ID |
| 轮询 | POST | `/samantha/chat/async/stream` | body `{"task_id","event_id":0}`；SSE `content_type=2021` 取 `video_url` 或 `video_model.video_list.*.main_url`（base64） |
| CSRF | GET | `/passport/safe/csrf_token/` | 刷新 CSRF |
| 图片上传 | POST | `/samantha/pages/upload_image` | 图生视频素材 |

提交与轮询均为 **SSE 流式**，适配器直接读 `HttpClient.session` 原始文本（避免被通用响应截断）。

## 3. 模型

网页协议**未暴露显式 model id**，服务端按 `skill_type=17` 选定（即 Seedance 系列）。
描述符中提供两个别名占位：

| 别名 | 说明 |
|---|---|
| `doubao-video` | 文生视频 |
| `doubao-video-i2v` | 图生视频 |

## 4. 能力与限制

| 能力 | 支持 | 说明 |
|---|---|---|
| video | ✅ | 提交 → SSE 轮询 |
| models | ✅ | 静态别名 |
| verify | ✅ | 仅判 `sessionid` 存在（无公开 whoami 端点） |
| credits | ❌ | 上游无接口 |
| checkin | ❌ | 上游无接口 |
| image | ❌ | 图片生成同步返回、无 task_id，与 submit→poll 契约不合 |
| upload | ❌ | 未移植 |

**错误码**：`710022002` / `710022004` → 风控；`710012001` → 登录态过期。

## 5. 自测

```bash
PY="/c/Users/XZXyuan/AppData/Local/Programs/Python/Python313/python.exe"
cd E:/API管理工具/any-video2api && PYTHONPATH=. "$PY" -c "
from src.channels.registry import registry
registry.load()
print(registry.names(), registry.errors())
print(registry.get('doubao').capabilities())
"
```
