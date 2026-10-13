#!/usr/bin/env bash
# 应用每日调度（plist）。**有每日任务在跑时拒绝**：launchctl 卸载/加载会杀掉那次运行。
#
#   bash scripts/apply_daily_schedule.sh            # 需要时 reload 并打印生效后的日程
#   bash scripts/apply_daily_schedule.sh --check    # 只打印当前生效的日程与 plist 内容差异
set -uo pipefail
PLIST="$HOME/Library/LaunchAgents/com.quant.cn-pipeline.plist"
LABEL="com.quant.cn-pipeline"
UID_NUM="$(id -u)"

running() { pgrep -f "run_daily_production.sh" >/dev/null 2>&1; }

show() {
  echo "== plist 里配置的日程 =="
  /usr/libexec/PlistBuddy -c "Print :StartCalendarInterval" "$PLIST" 2>/dev/null | tr '\n' ' ' | sed 's/  */ /g'
  echo
  echo "== launchd 当前生效的日程 =="
  launchctl print "gui/$UID_NUM/$LABEL" 2>/dev/null | grep -A 12 -i "calendar" | head -16
}

if [ "${1:-}" = "--check" ]; then show; exit 0; fi

if running; then
  echo "[schedule] 检测到每日任务正在运行 → 拒绝 reload（reload 会杀掉那次运行）。"
  echo "[schedule] 等它 done 之后再跑：bash scripts/apply_daily_schedule.sh"
  exit 2
fi

plutil -lint "$PLIST" >/dev/null || { echo "[schedule] plist 语法错误"; exit 1; }
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST" || { echo "[schedule] bootstrap 失败"; exit 1; }
echo "[schedule] 已 reload：$LABEL"
show
