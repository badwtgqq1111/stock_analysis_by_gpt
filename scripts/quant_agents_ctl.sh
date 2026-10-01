#!/usr/bin/env bash
# 管理 4 个 launchd 定时任务（自动化）的开关。优化期间建议 off，避免后台任务抢 CPU / 覆盖产物。
#   bash scripts/quant_agents_ctl.sh status|on|off
set -uo pipefail
LA="$HOME/Library/LaunchAgents"
LABELS=(com.quant.cn-pipeline com.quant.cn-feature-realign com.quant.cn-panel-rescore com.quant.cn-reselect)
case "${1:-status}" in
  status)
    for l in "${LABELS[@]}"; do
      if launchctl list | grep -q "$l"; then
        printf "  %-32s LOADED  %s\n" "$l" "$(launchctl list | awk -v l="$l" '$3==l{print "pid="$1" last_exit="$2}')"
      else
        printf "  %-32s unloaded\n" "$l"
      fi
    done
    ;;
  off)
    for l in "${LABELS[@]}"; do launchctl bootout "gui/$UID/$l" 2>/dev/null && echo "  stopped $l" || echo "  $l 未加载"; done
    ;;
  on)
    for l in "${LABELS[@]}"; do
      plist="$LA/$l.plist"
      [ -f "$plist" ] || { echo "  缺少 $plist"; continue; }
      launchctl bootstrap "gui/$UID" "$plist" 2>/dev/null && echo "  started $l" || echo "  $l 启动失败（可能已加载）"
    done
    ;;
  *) echo "用法: bash scripts/quant_agents_ctl.sh status|on|off" >&2; exit 2 ;;
esac
