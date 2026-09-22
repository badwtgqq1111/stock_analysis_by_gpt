#!/usr/bin/env bash
# 安装/更新 macOS launchd 定时任务（每天 19:30 / 20:30 / 22:00，开机补跑）
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
TARGET="$HOME/Library/LaunchAgents/com.quant.cn-pipeline.plist"
mkdir -p "$(dirname "$TARGET")" "$REPO/output/pipeline_reports/daily"
UV_DIR="$(dirname "$(command -v uv 2>/dev/null || echo /usr/local/bin/uv)")"
sed -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" -e "s|__UV_DIR__|$UV_DIR|g" \
    "$HERE/com.quant.cn-pipeline.plist" > "$TARGET"
echo "uv dir: $UV_DIR"
launchctl bootout "gui/$(id -u)/com.quant.cn-pipeline" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$TARGET"
launchctl enable "gui/$(id -u)/com.quant.cn-pipeline"
echo "installed: $TARGET"
echo "manual run: launchctl kickstart -k gui/$(id -u)/com.quant.cn-pipeline"
echo "logs: $REPO/output/pipeline_reports/daily/"
