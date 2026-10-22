#!/usr/bin/env bash
# Independent daily strategy-list refresh.
#
# It deliberately does not run the model/data pipeline.  Its job is to make the
# per-strategy list contract visible even when the main production run failed or
# is still busy.  Stale upstream data is reported as `blocked_by` by
# refresh_strategy_lists.py instead of being silently refreshed from old inputs.
set -uo pipefail

ROOT="/Users/ccs/code/quant/stock_analysis_by_gpt"
LOG_DIR="$ROOT/output/results_cn/strategy_lists"
LOCK_DIR="$LOG_DIR/.refresh.lock"
LOG="$LOG_DIR/independent.log"
mkdir -p "$LOG_DIR"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG"; }

# The main production runner already invokes strategy_lists near the end.
# Do not contend with it; the 23:15 retry covers a run that is still active at 22:15.
if pgrep -f "run_daily_production.sh" >/dev/null 2>&1; then
  log "skip: run_daily_production.sh is still running; main pipeline will refresh lists"
  exit 0
fi

if [ -d "$LOCK_DIR" ]; then
  pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    log "skip: strategy refresh already running pid=$pid"
    exit 0
  fi
  rm -rf "$LOCK_DIR"
fi
mkdir -p "$LOCK_DIR"
printf '%s\n' "$$" > "$LOCK_DIR/pid"
cleanup() { rm -rf "$LOCK_DIR"; }
trap cleanup EXIT INT TERM

cd "$ROOT" || exit 1
if [ -f "$ROOT/config/scheduler_env.sh" ]; then
  # shellcheck disable=SC1091
  set -a; . "$ROOT/config/scheduler_env.sh"; set +a
fi
UV_BIN="$(command -v uv || true)"
if [ -z "$UV_BIN" ]; then
  log "FAILED: uv not found"
  exit 127
fi

log "strategy refresh start"
"$UV_BIN" run python scripts/refresh_strategy_lists.py --print >> "$LOG" 2>&1
rc=$?
log "strategy refresh exit=$rc"
exit "$rc"
