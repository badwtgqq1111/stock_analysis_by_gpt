#!/usr/bin/env bash
# 5min 历史回补的常驻调度器（一次会话 3 小时，退出后由 LaunchAgent 再拉起）。
#
# 为什么要单独一层：BaoStock 会因并发会话把 IP 拉黑（10001011）。被拉黑时**不能**反复登录，
# 否则黑名单会一直续期。所以这里：
#   1. 有 cooldown.json 就先睡到解禁；
#   2. 每轮只用一个会话（默认 2 个 worker），跑满 DAILY_HISTORY_SECONDS 就退出；
#   3. 连续 3 轮「零新增」→ 写 STOPPED 标记并停止，避免无限空转（人工删掉即可续跑）。
set -u
cd "$(dirname "$0")/.."

ROOT="$(pwd)"
UV="${UV:-$HOME/.local/bin/uv}"
[ -x "$UV" ] || UV="$(command -v uv || true)"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
STATE="$ROOT/assets/data/raw/intraday_baostock/_hist"
LOG_DIR="$ROOT/output/logs"
mkdir -p "$STATE" "$LOG_DIR"
STOP="$STATE/STOPPED"
ZERO="$STATE/zero_streak"
WORKERS="${HISTORY_WORKERS:-2}"
SECONDS_PER_SESSION="${HISTORY_SECONDS:-10800}"

[ -f "$STOP" ] && { echo "[hist] STOPPED 标记存在，跳过（删除 $STOP 可恢复）"; exit 0; }

# 冷却
if [ -f "$STATE/cooldown.json" ]; then
  "$UV" run python - "$STATE/cooldown.json" <<'PY'
import json,sys,time
try: until=float(json.load(open(sys.argv[1])).get("until") or 0)
except Exception: until=0
wait=int(until-time.time())
print(f"[hist] cooldown 剩余 {max(0,wait)//60} 分钟")
sys.exit(0 if wait>0 else 1)
PY
  if [ $? -eq 0 ]; then sleep 1800; exit 0; fi
fi

before=$("$UV" run python scripts/backfill_cn_intraday_history.py --status 2>/dev/null | uv run python -c 'import json,sys;print(json.load(sys.stdin).get("done_markers",0))' 2>/dev/null || echo 0)
echo "[hist] 会话开始 已完成标记=$before"
"$UV" run python scripts/backfill_cn_intraday_history.py --workers "$WORKERS" --session-seconds "$SECONDS_PER_SESSION" \
  >> "$LOG_DIR/intraday_hist_session.log" 2>&1
after=$("$UV" run python scripts/backfill_cn_intraday_history.py --status 2>/dev/null | uv run python -c 'import json,sys;print(json.load(sys.stdin).get("done_markers",0))' 2>/dev/null || echo 0)
echo "[hist] 会话结束 已完成标记=$after（新增 $((after-before))）"

if [ "$after" -le "$before" ]; then
  streak=$(( $(cat "$ZERO" 2>/dev/null || echo 0) + 1 ))
  echo "$streak" > "$ZERO"
  if [ "$streak" -ge 3 ]; then
    echo "$(date '+%F %T') 连续 $streak 轮零新增，写 STOPPED" > "$STOP"
    osascript -e 'display notification "分时历史回补连续 3 轮零新增，已暂停" with title "Quant"' 2>/dev/null || true
  fi
else
  echo 0 > "$ZERO"
fi
