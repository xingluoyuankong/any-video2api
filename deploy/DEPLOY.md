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
| **http://52-64-8-93.sslip.io/login** | ✅ 可用（控制台登录页，账号密码） |
| **http://52-64-8-93.sslip.io/console** | ✅ 可用（登录后进入） |
| http://52-64-8-93.sslip.io/health | ✅ 可用 |
| https://52-64-8-93.sslip.io | ⏳ 证书已签，待 Lightsail 防火墙开放 443 |
| https://video.xzx-yuan.ccwu.cc | ⏳ 待 Cloudflare 面板配置（见下） |

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

## 待办：配置自有域名（Cloudflare Zero Trust 隧道）

服务器上已运行 **Cloudflare 隧道**（`cloudflared tunnel run --token ...`，remotely-managed，
配置在 Cloudflare 面板，本地无配置文件），已有 `flow.xzx-yuan.ccwu.cc` 走同一隧道。

要让 `video.xzx-yuan.ccwu.cc` 指向本服务，需在 **Cloudflare Zero Trust 面板**做两步：

### 1. 添加隧道 Public Hostname

路径：**Zero Trust → Networks → Tunnels → 选中现有隧道 → Public Hostname → Add a public hostname**

| 字段 | 值 |
|---|---|
| Subdomain | `video` |
| Domain | `xzx-yuan.ccwu.cc` |
| Path | 留空 |
| Service Type | `HTTP` |
| Service URL | `localhost:8800` |

> 也可直接指向 nginx：Service `HTTP` + `localhost:80`（此时由 nginx 按 server_name 分发）。

### 2. 确认 DNS

面板保存 Public Hostname 时会自动创建对应的 CNAME 记录（指向 `<tunnel-id>.cfargotunnel.com`）。
若未自动创建，手动加一条：

| Type | Name | Content | Proxy |
|---|---|---|---|
| CNAME | `video` | `<tunnel-id>.cfargotunnel.com` | Proxied（橙云） |

完成后访问 **https://video.xzx-yuan.ccwu.cc/console** —— 走 Cloudflare 边缘，**自动 HTTPS**，无需在 Lightsail 开放 443。

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
