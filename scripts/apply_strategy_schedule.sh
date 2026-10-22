#!/usr/bin/env bash
set -uo pipefail
PLIST_SRC="/Users/ccs/code/quant/stock_analysis_by_gpt/deploy/strategy-refresh/macos/com.quant.strategy-lists.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.quant.strategy-lists.plist"
LABEL="com.quant.strategy-lists"
UID_NUM="$(id -u)"

plutil -lint "$PLIST_SRC" >/dev/null || { echo "[strategy-schedule] plist syntax error"; exit 1; }
if pgrep -f "scripts/refresh_strategy_lists.py" >/dev/null 2>&1; then
  echo "[strategy-schedule] strategy refresh is running; refusing reload"
  exit 2
fi
cp "$PLIST_SRC" "$PLIST_DST"
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST_DST" || exit 1
echo "[strategy-schedule] loaded $LABEL"
launchctl print "gui/$UID_NUM/$LABEL" 2>/dev/null | grep -E 'state =|runs =|last exit code' | head -10
