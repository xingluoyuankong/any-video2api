#!/usr/bin/env bash
# 用 Cloudflare API Token 自动为 any-video2api 配置隧道域名（Public Hostname + DNS）。
#
# 用法：
#   CF_TOKEN=cfut_xxx ./deploy/cf-setup.sh                          # 默认 video.xzx-yuan.ccwu.cc -> localhost:8800
#   CF_TOKEN=cfut_xxx ./deploy/cf-setup.sh api.example.com http://localhost:8800
#
# Token 所需权限（Cloudflare → My Profile → API Tokens → Create Token → Custom token）：
#   Account  → Cloudflare Tunnel → Edit
#   Zone     → DNS               → Edit   （Zone Resources: 包含 xzx-yuan.ccwu.cc / ccwu.cc）
#
# 也可直接指定 ACCOUNT_ID / TUNNEL_ID 覆盖下面的默认值。
set -euo pipefail

CF_TOKEN="${CF_TOKEN:?请设置 CF_TOKEN 环境变量（Cloudflare API Token）}"
HOSTNAME_FULL="${1:-video.xzx-yuan.ccwu.cc}"
SERVICE="${2:-http://localhost:8800}"

API="https://api.cloudflare.com/client/v4"
ACCOUNT_ID="${ACCOUNT_ID:-1aa08f06081e0aaef7ab984203f2978d}"
TUNNEL_ID="${TUNNEL_ID:-51e5b666-e527-4c0a-ae51-99205aca7598}"

PY="$(command -v python3 || command -v python)"

cf() { curl -s -m 30 -H "Authorization: Bearer $CF_TOKEN" -H "Content-Type: application/json" "$@"; }
jget() { "$PY" -c "import sys,json;d=json.load(sys.stdin);print(json.dumps(d.get('$1'),ensure_ascii=False))"; }
jok()  { "$PY" -c "import sys,json;d=json.load(sys.stdin);print('OK' if d.get('success') else 'FAIL', (d.get('errors') or [{}])[0].get('message',''))"; }

echo "== 1/4 校验 Token =="
if ! cf "$API/user/tokens/verify" | jok | grep -q '^OK'; then
  echo "   Token 无效。请确认："
  echo "   - 是从 Cloudflare → My Profile → API Tokens 生成的 API Token（不是 Tunnel Token / Access Token）"
  echo "   - 复制完整、未被撤销"
  cf "$API/user/tokens/verify" | head -c 300; echo; exit 1
fi
echo "   Token 有效"

echo "== 2/4 读取隧道当前配置 =="
CUR="$(cf "$API/accounts/$ACCOUNT_ID/cfd_tunnel/$TUNNEL_ID/configurations")"
echo "$CUR" | jok
CONFIG="$(echo "$CUR" | "$PY" -c "
import sys,json
d=json.load(sys.stdin)
cfg=(d.get('result') or {}).get('config') or {}
ingress=cfg.get('ingress') or []
print(json.dumps({'ingress':ingress},ensure_ascii=False))
")"
echo "   现有 ingress 条目: $(echo "$CONFIG" | "$PY" -c "import sys,json;print(len(json.load(sys.stdin)['ingress']))")"

echo "== 3/4 追加 Public Hostname: $HOSTNAME_FULL -> $SERVICE =="
NEW="$(echo "$CONFIG" | "$PY" -c "
import sys,json
cfg=json.load(sys.stdin)
ing=cfg['ingress']
host='$HOSTNAME_FULL'
# 去掉同名旧条目与结尾 catch-all
ing=[e for e in ing if e.get('hostname')!=host and not (e.get('service','').startswith('http_status:404') and 'hostname' not in e)]
ing.append({'hostname':host,'service':'$SERVICE','originRequest':{'noTLSVerify':False}})
ing.append({'service':'http_status:404'})
print(json.dumps({'config':{'ingress':ing}},ensure_ascii=False))
")"
cf -X PUT "$API/accounts/$ACCOUNT_ID/cfd_tunnel/$TUNNEL_ID/configurations" --data "$NEW" | jok

echo "== 4/4 确保 DNS（CNAME -> $TUNNEL_ID.cfargotunnel.com, 橙云）=="
ZONE_NAME="$(echo "$HOSTNAME_FULL" | awk -F. '{print $(NF-2)"."$(NF-1)"."$NF}')"
ZONE_ID="$(cf "$API/zones?name=$ZONE_NAME" | "$PY" -c "
import sys,json;d=json.load(sys.stdin);r=d.get('result') or []
print(r[0]['id'] if r else '')")"
if [ -z "$ZONE_ID" ]; then
  echo "   未找到 zone: $ZONE_NAME（Token 可能没有该 Zone 的读取权限）"; exit 1
fi
SUB="${HOSTNAME_FULL%.$ZONE_NAME}"
EXIST="$(cf "$API/zones/$ZONE_ID/dns_records?type=CNAME&name=$HOSTNAME_FULL" | "$PY" -c "
import sys,json;d=json.load(sys.stdin);r=d.get('result') or [];print(r[0]['id'] if r else '')")"
if [ -n "$EXIST" ]; then
  echo "   DNS 记录已存在，跳过"
else
  cf -X POST "$API/zones/$ZONE_ID/dns_records" --data "{\"type\":\"CNAME\",\"name\":\"$SUB\",\"content\":\"$TUNNEL_ID.cfargotunnel.com\",\"proxied\":true,\"comment\":\"any-video2api\"}" | jok
fi

echo
echo "完成。稍等 10~30 秒生效后访问： https://$HOSTNAME_FULL/login"
