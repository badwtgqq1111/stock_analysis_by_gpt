#!/usr/bin/env bash
# 起 Web 服务（默认绑 0.0.0.0，手机在同一局域网即可访问）。
#
#   ./scripts/serve_web.sh                 # 默认 0.0.0.0:8000
#   ./scripts/serve_web.sh --port 8080
#   HOST=127.0.0.1 ./scripts/serve_web.sh  # 只允许本机（默认行为改回本机）
#   RELOAD=1 ./scripts/serve_web.sh        # 开发模式（改代码自动重载）
#
# 说明：
#   * 绑 0.0.0.0 才能从手机访问；绑 127.0.0.1 只有这台机器自己能连。
#   * 首次启动 macOS 可能弹"是否允许 Python/python3 接受传入网络连接"，要选**允许**；
#     否则局域网仍连不上（系统设置 → 网络 → 防火墙 里可复查）。
#   * 本服务无鉴权、内容是你本地的研究数据 —— 只在可信局域网（家里/自己的热点）里开。
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
RELOAD="${RELOAD:-0}"
for arg in "$@"; do
  case "$arg" in
    --port) shift; PORT="${1:-8000}" ;;
    --port=*) PORT="${arg#--port=}" ;;
    --host) shift; HOST="${1:-0.0.0.0}" ;;
    --host=*) HOST="${arg#--host=}" ;;
    --reload) RELOAD=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

# 取局域网 IP（en0=Wi-Fi，en1/en5 是有线/其它）
LAN_IP=""
for iface in en0 en1 en5; do
  ip="$(ipconfig getifaddr "$iface" 2>/dev/null || true)"
  if [ -n "$ip" ]; then LAN_IP="$ip"; break; fi
done

echo "──────────────────────────────────────────────"
echo " 本机访问 : http://127.0.0.1:${PORT}"
if [ "$HOST" = "0.0.0.0" ] && [ -n "$LAN_IP" ]; then
  echo " 手机访问 : http://${LAN_IP}:${PORT}      ← 手机连同一个 Wi-Fi 后打开"
elif [ "$HOST" != "0.0.0.0" ]; then
  echo " 局域网   : 已关闭（HOST=${HOST}，手机连不上）"
else
  echo " 局域网   : 未取到 IP，手动看 系统设置 → 网络"
fi
echo "──────────────────────────────────────────────"

ARGS=(uv run uvicorn backend.main:app --host "$HOST" --port "$PORT")
[ "$RELOAD" = "1" ] && ARGS+=(--reload)
exec "${ARGS[@]}"
