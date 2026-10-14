# 部署记录与运维

## 当前线上部署

| 项 | 值 |
|---|---|
| 服务器 | AWS Lightsail（Sydney ap-southeast-2a）· `52.64.8.93` · 用户 `ubuntu` |
| SSH | `ssh aws-xzxyuan` |
| 部署路径 | `/opt/any-video2api` |
| 运行时 | Python 3.12 + venv（`/opt/any-video2api/.venv`） |
| 服务 | systemd `any-video2api`（`enabled` 开机自启，`Restart=always`） |
| 监听 | `127.0.0.1:8800` |
| 反向代理 | nginx 站点 `any-video2api`（80 直连 / 443 保留 SSL） |
| 日志 | `/var/log/any-video2api.log` |
| 密钥 | `/opt/any-video2api/.env`（`chmod 600`，`AV2A_API_KEY`） |

### 访问地址

| 地址 | 状态 |
|---|---|
| **https://video.xzx-yuan.ccwu.cc/login** | ✅ **主入口**（自有域名，CF 隧道，边缘自动 HTTPS） |
| https://video.xzx-yuan.ccwu.cc/console | ✅ 可用（登录后进入） |
| https://video.xzx-yuan.ccwu.cc/health | ✅ 可用 |
| http://52-64-8-93.sslip.io/login | ✅ 可用（备用，直连 HTTP） |
| https://52-64-8-93.sslip.io | ⏳ 证书已签，待 Lightsail 防火墙开放 443 |

### 控制台登录凭据

| 项 | 值 |
|---|---|
| 用户名 | `admin` |
| 密码 | `/opt/any-video2api/data/initial_admin.txt`（`600`，首次启动生成） |

```bash
cat /opt/any-video2api/data/initial_admin.txt     # 查看初始密码
# 登录后请在右上角「账号」中修改密码，然后删除该文件
```

> 程序调用仍用 API Key（`/v1/*`）；控制台会话不能调用生成接口。
> API Key 在 `/opt/any-video2api/.env`（`AV2A_API_KEY`），或在控制台「密钥」页新建。

---

## 自有域名（Cloudflare 隧道）— 已配置

`video.xzx-yuan.ccwu.cc` → Cloudflare 边缘（自动 HTTPS）→ 隧道 → `localhost:8800`。

**做法**（用 `cloudflared tunnel login` 授权链接，无需 API Token）：

```bash
# 1) 发起授权，输出一次性授权 URL
cloudflared tunnel login
#    浏览器打开 URL → 登录 Cloudflare → 选 zone xzx-yuan.ccwu.cc → 授权
#    证书自动落到 ~/.cloudflared/cert.pem

# 2) 新建本地管理的隧道（不影响原有 ttwz-panel）
cloudflared tunnel create any-video2api
#    -> id 6dbbcf1b-1627-4a32-a65e-b0e86e38c987
#    -> 凭据 ~/.cloudflared/6dbbcf1b-1627-4a32-a65e-b0e86e38c987.json

# 3) ~/.cloudflared/config.yml
#    tunnel: 6dbbcf1b-1627-4a32-a65e-b0e86e38c987
#    credentials-file: /home/ubuntu/.cloudflared/6dbbcf1b-....json
#    ingress:
#      - hostname: video.xzx-yuan.ccwu.cc
#        service: http://localhost:8800
#      - service: http_status:404

# 4) 建 DNS（CNAME video -> <tunnel-id>.cfargotunnel.com）
cloudflared tunnel route dns any-video2api video.xzx-yuan.ccwu.cc

# 5) systemd 常驻
sudo systemctl enable --now cloudflared-anyvideo
```

> 原有隧道 `ttwz-panel`（remotely-managed，token 启动）**未改动**；本服务用独立隧道
> `any-video2api`，互不影响。若想复用 `ttwz-panel`，需在 Cloudflare 面板加 Public Hostname
> （remotely-managed 的 ingress 本地改不了）。
>
> 备选：`deploy/cf-setup.sh`（用 API Token 追加 Public Hostname + 建 DNS），需 API Token 权限
> `Account → Cloudflare Tunnel → Edit` + `Zone → DNS → Edit`。

### 可选：开放 443 走直连

如需 `https://52-64-8-93.sslip.io` 直连可用：
**Lightsail 控制台 → 实例 → Networking → IPv4 Firewall → Add rule → HTTPS / TCP 443**

---

## 运维命令

```bash
# 服务
sudo systemctl status any-video2api
sudo systemctl restart any-video2api
sudo tail -f /var/log/any-video2api.log

# 更新代码
cd /opt/any-video2api && git pull && sudo systemctl restart any-video2api

# 读取 API Key
cat /opt/any-video2api/.env

# 轮换 API Key
NEW=$(openssl rand -hex 24)
printf 'AV2A_API_KEY=%s\n' "$NEW" > /opt/any-video2api/.env
chmod 600 /opt/any-video2api/.env
sudo systemctl restart any-video2api

# nginx
sudo nginx -t && sudo systemctl reload nginx
sudo certbot certificates          # 查看证书
sudo certbot renew --dry-run       # 测试自动续期

# 本地健康检查
curl -s http://127.0.0.1:8800/health
```

---

## 部署步骤（可复现）

```bash
# 1) 拉代码
sudo mkdir -p /opt/any-video2api && sudo chown ubuntu:ubuntu /opt/any-video2api
cd /opt/any-video2api && git clone https://github.com/xingluoyuankong/any-video2api.git .

# 2) 依赖
python3 -m venv .venv
.venv/bin/pip install -U pip && .venv/bin/pip install -r requirements.txt

# 3) 密钥（不落 git、不落对话）
printf 'AV2A_API_KEY=%s\n' "$(openssl rand -hex 24)" > /opt/any-video2api/.env
chmod 600 /opt/any-video2api/.env

# 4) systemd
sudo cp deploy/any-video2api.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now any-video2api

# 5) nginx
sudo cp deploy/nginx.conf /etc/nginx/sites-available/any-video2api
sudo ln -sf /etc/nginx/sites-available/any-video2api /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# 6) 证书（可选，需 80 可达）
sudo certbot --nginx -d <your-domain> --non-interactive --agree-tos -m <email>
```

---

## 注意事项

- **内存**：实例约 1.9G，已用较多；本服务常驻约 100–200M，注意与其它服务共存。
- **密钥**：`.env` 为 `600` 权限且被 `.gitignore` 排除；首次启动日志中会打印自动生成的库内 Key，
  若日志外泄请用上面命令轮换。
- **数据持久化**：`data/`（SQLite + 媒体）与 `config/` 在部署目录内，更新代码时不会被 `git pull` 覆盖（已在 `.gitignore`）。
- **Docker 未安装**：服务器无 docker，故用 venv + systemd 部署；如需容器化，参考根目录 `Dockerfile`。
