#!/usr/bin/env bash
# 每日生产入口：所有调度器（launchd / systemd timer / cron / Docker）都调用它。
#
#   ./scripts/run_daily_production.sh [--force] [--dry-run] [--skip-retrain]
#
# 逻辑：
#   1. 只在收盘之后运行（默认 16:00，`DAILY_EARLIEST_HOUR` 可调）；开机自启时若未到点则退出。
#   2. 同一天只跑一次：成功后写 output/pipeline_reports/daily/<date>.done，重复触发直接跳过
#      （--force 覆盖）。
#   3. 阶段顺序与 docs/runbooks/cn-data-pipeline.md 的"每日生产"一致：
#      daily_bars → regime → features → clean_panel → model_scores
#      → preselection → pk → exits → paper_account → paper_outcomes
#      不再使用旧的一步式 --stage selection（它默认在配置里关闭）。
#   4. 每个阶段写独立日志，最后按阶段生成两阶段选股报告。
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
ROOT="$(pwd)"

# launchd / cron / systemd 的 PATH 很干净，这里补齐 uv 的常见安装位置
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found on PATH=${PATH}；请安装 uv（curl -LsSf https://astral.sh/uv/install.sh | sh）或把其目录加进 PATH" >&2
  exit 127
fi

FORCE=0
DRY_RUN=0
SKIP_RETRAIN=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --skip-retrain) SKIP_RETRAIN=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

# 调度器环境：非交互式 shell 不会读取 ~/.bashrc（而且 ~/.bashrc 开头有 `case $- in *i*)` 守卫，
# 手动 source 也会立刻返回），所以把交互式环境里的关键变量固化到 config/scheduler_env.sh 后显式加载。
# 重新采集：bash scripts/capture_scheduler_env.sh
#
# 命令行显式传入的变量优先（便于临时覆盖 DAILY_NOTIFY=0 等）：source 之后再恢复调用方取值。
CALLER_ENV="$(env | grep -E '^[A-Za-z_][A-Za-z0-9_]*=' || true)"
load_env_file() {
  [ -f "$1" ] || return 0
  set -a; . "$1"; set +a
}
load_env_file "$ROOT/config/scheduler_env.sh"
load_env_file "$ROOT/config/notify.env"
if [ -n "$CALLER_ENV" ]; then
  while IFS= read -r line; do
    key="${line%%=*}"
    case "$key" in
      PATH|HOME|PWD|SHLVL|_|OLDPWD|SHELL|TERM|TMPDIR|USER|LOGNAME) continue ;;
    esac
    export "$key=${line#*=}"
  done <<< "$CALLER_ENV"
fi

EARLIEST_HOUR="${DAILY_EARLIEST_HOUR:-16}"
LOCK_DIR="$ROOT/output/pipeline_reports/daily/.lock"
STALE_LOCK_SECONDS="${DAILY_STALE_LOCK_SECONDS:-10800}"
TRADE_DATE="${DAILY_TRADE_DATE:-$(date +%Y-%m-%d)}"
RUN_DIR="$ROOT/output/pipeline_reports/daily"
LOG_DIR="$RUN_DIR/$TRADE_DATE"
MARKER="$LOG_DIR/production.done"
mkdir -p "$LOG_DIR"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_DIR/run.log"; }

if [ "$FORCE" -ne 1 ]; then
  if [ "$(date +%H)" -lt "$EARLIEST_HOUR" ]; then
    log "skip: 本地时间 $(date '+%H:%M') 早于 $EARLIEST_HOUR:00，收盘后数据尚未就绪"
    exit 0
  fi
  if [ -f "$MARKER" ]; then
    log "skip: ${TRADE_DATE} 已成功运行过（${MARKER}）；需要重跑请加 --force"
    exit 0
  fi
fi

STAGES=(
  "daily_bars:false"
  "regime:false"
  "features:$SKIP_RETRAIN"
  "clean_panel:$SKIP_RETRAIN"
  "model_scores:false"
  "preselection:false"
  "pk:false"
  "exits:false"
  "paper_account:false"
  "paper_outcomes:false"
)

# 单实例锁：19:00 / 20:30 / 22:00 三次触发不会并行跑；超过 3 小时的锁视为陈旧并接管
if [ "$DRY_RUN" -eq 0 ]; then
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo $$ > "$LOCK_DIR/pid"
    trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT
  else
    lock_age=$(( $(date +%s) - $(stat -f %m "$LOCK_DIR" 2>/dev/null || echo 0) ))
    lock_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || echo '')"
    lock_alive=0
    if [ -n "$lock_pid" ] && kill -0 "$lock_pid" 2>/dev/null; then
      lock_alive=1
    fi
    # 进程被杀（如手动中断/断电）不会执行 trap，这里按"持有者进程是否存活"判定，
    # 避免陈旧锁把当天后续的自动重试全部挡掉。
    if [ "$lock_alive" -eq 0 ] || [ "$lock_age" -gt "$STALE_LOCK_SECONDS" ]; then
      log "taking over stale lock (holder pid=${lock_pid:-none} alive=${lock_alive} age=${lock_age}s)"
      echo $$ > "$LOCK_DIR/pid"
      trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT
    else
      log "skip: 已有运行中的实例（pid=${lock_pid}，${lock_age}s）"
      exit 0
    fi
  fi
fi

log "env: uv=$(command -v uv) | CLICKHOUSE_HOST=${CLICKHOUSE_HOST:-unset} CLICKHOUSE_PASSWORD=$([ -n "${CLICKHOUSE_PASSWORD:-}" ] && echo set || echo unset) | TUSHARE_KEY=$([ -n "${TUSHARE_RELAY_KEY:-}" ] && echo set || echo unset) | proxy=${HTTPS_PROXY:-none}"
notify_alert() {
  # 失败告警：走同一套渠道（飞书/邮件/webhook）；通知本身失败不影响退出码
  if [ "${DAILY_NOTIFY:-1}" != "1" ]; then
    return 0
  fi
  local subject="$1"; shift
  local body="$1"
  uv run python scripts/notify_selection.py --text "$body" --subject "$subject" >> "$LOG_DIR/run.log" 2>&1 || true
}

# 预检：跑一遍与本次改动相关的回归测试（约 1 秒），把"代码级错误"挡在 70 分钟的重活之前
PREFLIGHT_CMD="${DAILY_PREFLIGHT_CMD:-uv run python -m pytest test/test_portfolio_execution_guards.py test/test_selection_notify.py test/test_volume_volatility_features.py -q}"
if [ "${DAILY_PREFLIGHT:-1}" = "1" ] && [ "$DRY_RUN" -eq 0 ]; then
  if eval "$PREFLIGHT_CMD" > "$LOG_DIR/preflight.log" 2>&1; then
    log "preflight ok: $PREFLIGHT_CMD"
  else
    log "preflight FAILED: ${PREFLIGHT_CMD}（见 $LOG_DIR/preflight.log）"
    notify_alert "CN 自动化预检失败 ${TRADE_DATE}" "预检命令失败：${PREFLIGHT_CMD}
$(tail -5 "$LOG_DIR/preflight.log")"
    exit 2
  fi
fi

log "start daily production trade_date=$TRADE_DATE force=$FORCE dry_run=$DRY_RUN skip_retrain=$SKIP_RETRAIN"
FAILED=0
for entry in "${STAGES[@]}"; do
  stage="${entry%%:*}"
  skip_flag="${entry##*:}"
  if [ "$skip_flag" = "true" ]; then
    log "stage=$stage skipped (--skip-retrain)"
    continue
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    log "stage=$stage would run: uv run python scripts/run_cn_pipeline.py --stage $stage"
    continue
  fi
  started=$(date +%s)
  if uv run python scripts/run_cn_pipeline.py --stage "$stage" > "$LOG_DIR/$stage.log" 2>&1; then
    log "stage=$stage ok ($(( $(date +%s) - started ))s) -> $LOG_DIR/$stage.log"
  else
    code=$?
    log "stage=$stage FAILED exit=$code -> $LOG_DIR/$stage.log"
    tail -5 "$LOG_DIR/$stage.log" | sed 's/^/    /' | tee -a "$LOG_DIR/run.log"
    notify_alert "CN 自动化失败 ${TRADE_DATE} stage=${stage}"       "每日生产在阶段 ${stage} 失败（exit=${code}）。
日志：$LOG_DIR/$stage.log
最后几行：
$(tail -5 "$LOG_DIR/$stage.log")
后续 20:30 / 22:00 的触发会自动重试；如需立即重跑：bash scripts/run_daily_production.sh --force"
    FAILED=1
    break
  fi
done

if [ "$DRY_RUN" -eq 0 ] && [ "$FAILED" -eq 0 ]; then
  if uv run python scripts/render_two_stage_report.py --trade-date "$TRADE_DATE" >> "$LOG_DIR/run.log" 2>&1; then
    log "two-stage report ok"
  else
    log "two-stage report FAILED"
    FAILED=1
  fi
  # 通知：飞书（bot 身份）+ 企业微信群机器人 webhook（若配置）等；失败不影响生产结果
  if [ "${DAILY_NOTIFY:-1}" = "1" ]; then
    if uv run python scripts/notify_selection.py --trade-date "$TRADE_DATE" >> "$LOG_DIR/run.log" 2>&1; then
      log "notify ok (feishu/wecom/dingtalk/generic 按已配置渠道)"
    else
      log "notify FAILED (见 $LOG_DIR/run.log；生产结果不受影响)"
    fi
  else
    log "notify disabled (DAILY_NOTIFY=0)"
  fi
fi

if [ "$FAILED" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
  date '+%Y-%m-%dT%H:%M:%S%z' > "$MARKER"
  log "done: $TRADE_DATE 生产完成（当日后续触发会跳过；失败则下一次触发自动重试）"
elif [ "$DRY_RUN" -eq 1 ]; then
  log "dry-run finished: 上面的阶段序列即每日生产实际执行顺序（未执行任何命令）"
else
  log "finished with failures"
fi
exit "$FAILED"
