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
#      daily_bars → moneyflow → regime → features → clean_panel → model_scores
#      → preselection → pk → exits → paper_account → paper_outcomes
#      不再使用旧的一步式 --stage selection（它默认在配置里关闭）。
#   3a.（2026-10-08 新增）主阶段**之前**先跑「数据补齐」块，把选股真正依赖的数据面凑齐：
#        · fundamental  —— 股票基础信息 / 估值历史 / 行业分类（[industry].taxonomy）
#        · financials   —— 财务·财报增量（Tushare fina-indicator，按"应有报告期"判定陈旧）
#        每个步骤**非致命**：数据源会抽风（2026-10-08 baostock 就挂过），不该因此让整条选股链停摆；
#        缺什么由最后的数据完整性报表统一暴露，而不是靠日志里有没有报错去猜。
#        开关：DAILY_DATA_REFRESH=0 整体关闭；DAILY_FUNDAMENTAL / DAILY_FINANCIALS 可单独关。
#   3b.（2026-10-08 新增）主阶段跑完后，再跑两个**不阻塞生产**的步骤：
#        · arm_selection  —— 研究手臂选股（候选 10 / 买入 5），产物 output/results_cn/arm_latest_selection.*
#        · intraday 增量  —— 只补「分时文件最后日期 < 最新交易日」的标的，只抓缺失窗口并合并
#      这两步此前都不在每日流程里：arm_selection 只出现在 config 的 [pipeline].stages（本脚本不读那个列表），
#      分时则只有一个"一次性历史回补"的 LaunchAgent（判据是"文件存在"，补齐 5,228 只后永远空转）。
#      两者都需要日线先更新完（分时增量用日线的最大 trade_date 判定"最新交易日"），所以放在主阶段之后。
#      开关：DAILY_ARM_SELECTION=0 / DAILY_INTRADAY=0 可分别关掉；DAILY_INTRADAY_SECONDS 限时（默认 1800）。
#   3d.（2026-10-09 新增）**板块/龙虎榜/申万行业**三块研究数据面（全部非阻塞，失败不影响选股）：
#        · boards  —— 新浪行业/概念/地域 板块行情 + **全量成分股**（255 个板块，实测 79s），
#                     同花顺 90 行业汇总（带净流入）+ 375 概念名单。
#                     来源选型：东财板块接口走 push2 clist，本机代理稳定 403；同花顺成分页被 Nginx 403；
#                     **新浪可用且最快**，所以成分取新浪、行业资金流口径取同花顺。
#        · lhb     —— 龙虎榜明细 + 机构买卖统计（按**交易日**增量，节假日不再误报失败）
#        · sw2021  —— 申万 2021 一级行业成分（乐咕乐股；限流，默认每次最多补 8 个行业、分几天补齐）
#      开关：DAILY_BOARDS / DAILY_LHB / DAILY_SW2021 / DAILY_ADJ_FACTOR / DAILY_ARM_CONCEPT / DAILY_PERFORMANCE = 0 可分别关闭；
#      DAILY_BOARDS_SESSION_SECONDS（默认 900）/ DAILY_LHB_DAYS（默认 15）/ DAILY_SW2021_MAX（默认 4，小预算）/
#      DAILY_ADJ_FACTOR_DAYS（默认 8）/ DAILY_ADJ_FACTOR_STOCKS（默认 200，历史回补预算）/
#      DAILY_ARM_CONCEPT_BACKTEST_MONTHS（默认 24，概念手臂月频回放窗口）。
#        · adj_factor —— 复权因子审计快照（Tushare relay：按日全市场 + 逐股历史）。此前开关是关的，
#          数据体检一直报「目录不存在」；实测 relay 接口可用，属配置问题不是数据缺失。
#   3c.（2026-10-09 新增）选股台账重建 —— 前端「选股日历」读的就是它
#        （output/results_cn/selection_ledger.parquet，由 scripts/build_selection_ledger.py 生成）。
#        此前这一步**不在每日流程里**：日历一直显示到"上一次有人手工重建"为止
#        （实测 2026-10-09 早上的台账还是 10-08 16:57 的，最新决策日停在 09-30，
#          而 10-08 的 feishu_*.json 与 selection_snapshots 其实早就写好了）。
#        必须放在 notify **之后**：台账的三路输入之一 feishu_<date>.json 是通知步骤写的。
#   4b. 每个阶段都带 --force-rebalance：selection.rebalance_stride_days（默认 5）本意是
#      "再平衡节奏"，但它会在 stride 窗口内直接沿用上一版组合，导致新选出来的票几天不换。
#      需要保留 stride 行为时用 DAILY_FORCE_REBALANCE=0 关闭。
#   4. 每个阶段写独立日志，最后按阶段生成两阶段选股报告。
# launchd / cron 的环境**没有 LANG/LC_ALL**，bash 会退化成逐字节解析：
#   变量名紧跟全角括号时会被吃掉半个多字节字符 → 变成未定义变量 → set -u 终止脚本
#   → `set -u` 直接终止整个脚本（2026-10-09 事故：数据步骤失败本应"只记日志不阻塞"，
#      结果整个每日生产在 19:30 / 20:30 两次静默退出，主阶段一步没跑）。
# 两道保险：① 显式设 UTF-8 locale；② 所有 `$var` 紧跟中文的地方改写 `${var}`。
export LANG="${LANG:-en_US.UTF-8}"
export LC_ALL="${LC_ALL:-en_US.UTF-8}"
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
# 默认每日强制按当天选股结果换股（见头部 4b）；DAILY_FORCE_REBALANCE=0 可关掉。
FORCE_REBALANCE="${DAILY_FORCE_REBALANCE:-1}"
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
  "moneyflow:false"
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
PREFLIGHT_CMD="${DAILY_PREFLIGHT_CMD:-uv run python -m pytest test/test_portfolio_execution_guards.py test/test_selection_notify.py test/test_volume_volatility_features.py test/test_board_sync.py test/test_data_completeness.py test/test_concept_arm.py test/test_web_cache_and_build.py test/test_stock_bundle_and_strategies.py test/test_daily_script_safety.py test/test_calendar_filters_and_fixes.py test/test_web_freshness.py test/test_strategy_pool.py test/test_industry_heat.py -q}"
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
PIPELINE_FLAGS=""
if [ "$FORCE_REBALANCE" = "1" ]; then
  PIPELINE_FLAGS="--force-rebalance"
fi
log "pipeline flags: ${PIPELINE_FLAGS:-none}"
# ── 数据补齐（选股之前）────────────────────────────────────────────────────
# 放在主阶段之前的原因：这些是 features / clean_panel 的输入。若放在主阶段之后，
# 当天新披露的财报、新发布的行业分类要等到**第二天**才会进特征面板。
# 每步非致命：失败只记日志不中断，最终由数据完整性报表汇总（见脚本末尾）。
run_data_step() {
  local name="$1"; shift
  if [ "$DRY_RUN" -eq 1 ]; then
    log "data-step=$name would run: $*"
    return 0
  fi
  local started; started=$(date +%s)
  if "$@" > "$LOG_DIR/data_$name.log" 2>&1; then
    log "data-step=$name ok ($(( $(date +%s) - started ))s) -> $LOG_DIR/data_$name.log"
  else
    local code=$?
    log "data-step=$name FAILED exit=${code}（不阻塞选股）-> $LOG_DIR/data_$name.log"
    tail -3 "$LOG_DIR/data_$name.log" | sed 's/^/    /' | tee -a "$LOG_DIR/run.log"
  fi
}

if [ "${DAILY_DATA_REFRESH:-1}" = "1" ]; then
  if [ "${DAILY_FUNDAMENTAL:-1}" = "1" ]; then
    run_data_step fundamental uv run python scripts/run_cn_pipeline.py --stage fundamental
  fi
  if [ "${DAILY_FINANCIALS:-1}" = "1" ]; then
    # 增量：只重抓"最后报告期 < 应有报告期"的股票（判据见 backfill_cn_financials.refresh_pending）
    run_data_step financials uv run python scripts/backfill_cn_financials.py --refresh \
        --session-seconds "${DAILY_FINANCIALS_SECONDS:-1800}"
    # 逐股检查点 → cn_financial_indicators.parquet（因子表达式读的就是这一份）
    run_data_step financials_merge uv run python scripts/backfill_cn_financials.py --merge
  fi
fi

FAILED=0
for entry in "${STAGES[@]}"; do
  stage="${entry%%:*}"
  skip_flag="${entry##*:}"
  if [ "$skip_flag" = "true" ]; then
    log "stage=$stage skipped (--skip-retrain)"
    continue
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    log "stage=$stage would run: uv run python scripts/run_cn_pipeline.py --stage $stage $PIPELINE_FLAGS"
    continue
  fi
  started=$(date +%s)
  if uv run python scripts/run_cn_pipeline.py --stage "$stage" $PIPELINE_FLAGS > "$LOG_DIR/$stage.log" 2>&1; then
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

# ── 研究手臂选股（arm_selection）：不替代生产名单，失败不阻塞当日生产 ──
# 产物：output/results_cn/arm_latest_selection.{csv,json}（前端「选股结果 → CN 研究手臂」与
# 流水线页「研究手臂（arm_selection）」tab 都读它）
# ── sleeve 特征面板链路（v1→v2→v3→v3i→v4）─────────────────────────────
# 为什么放在这里：arm_selection 读的就是 v4 面板。此前这条链路**完全不在每日流程里**，
# 面板一直停在上一次手工重建的日期（实测 2026-10-09 早上面板还停在 09-30），
# 于是"研究手臂"每天选出来的其实是上个月的月末名单，前端日历里也就看不到当天的 arm_select。
# 依赖：日线（daily_bars）、资金流（moneyflow）、估值 relay、财务 relay —— 都在上面的主阶段跑完了。
# 成本：实测 v1..v4 全链路 **73 秒**（27+10+14+7+15），相对于 7 小时的主流程可以忽略。
# 非致命：失败只影响研究手臂与 sleeve 研究，不影响当日生产名单。
if [ "$FAILED" -eq 0 ] && [ "${DAILY_SLEEVE_PANEL:-1}" = "1" ]; then
  if [ "$DRY_RUN" -eq 1 ]; then
    log "step=sleeve-panel would run: build_sleeve_features_long → panel_extend_v2 → panel_extend_v3 → panel_merge_intraday → build_panel_v4_delisted"
  else
    started=$(date +%s)
    panel_ok=1
    for _step in build_sleeve_features_long.py panel_extend_v2.py panel_extend_v3.py panel_merge_intraday.py build_panel_v4_delisted.py; do
      if ! uv run python "scripts/$_step" >> "$LOG_DIR/sleeve_panel.log" 2>&1; then
        log "sleeve-panel FAILED at ${_step}（不影响生产名单；arm_selection 会用旧面板）"
        panel_ok=0
        break
      fi
    done
    [ "$panel_ok" = "1" ] && log "sleeve-panel ok ($(( $(date +%s) - started ))s) -> v4 面板已刷新"
  fi
fi

if [ "$FAILED" -eq 0 ] && [ "${DAILY_ARM_SELECTION:-1}" = "1" ]; then
  if [ "$DRY_RUN" -eq 1 ]; then
    log "step=arm_selection would run: uv run python scripts/run_cn_pipeline.py --stage arm_selection"
  else
    log "arm-selection start（研究手臂 base_v1；候选 10 / 买入 5）"
    started=$(date +%s)
    if uv run python scripts/run_cn_pipeline.py --stage arm_selection > "$LOG_DIR/arm_selection.log" 2>&1; then
      log "arm-selection ok ($(( $(date +%s) - started ))s) -> $LOG_DIR/arm_selection.log"
    else
      code=$?
      log "arm-selection FAILED exit=${code}（研究手臂，不影响生产名单）-> $LOG_DIR/arm_selection.log"
      tail -5 "$LOG_DIR/arm_selection.log" | sed 's/^/    /' | tee -a "$LOG_DIR/run.log"
    fi
  fi
fi

# ── 板块 / 龙虎榜 / 申万行业：研究数据面（非阻塞）──────────────────────────
# 位置说明：这三块**不喂当天选股**（板块热度与行业轮动还在研究阶段），所以放在主阶段之后，
# 用 run_data_step 的"失败只记日志"语义，网络抽风不会拖垮 7 小时的生产链。
# 依赖：无（纯外部数据源：新浪 / 同花顺 / 东财数据中心 / 乐咕乐股）。
if [ "${DAILY_BOARDS:-1}" = "1" ]; then
  run_data_step boards uv run python scripts/sync_cn_boards.py \
      --session-seconds "${DAILY_BOARDS_SESSION_SECONDS:-900}"
fi
if [ "${DAILY_ADJ_FACTOR:-1}" = "1" ]; then
  # 复权因子审计快照：先按日同步全市场（1~2 个请求/交易日），再用小预算补历史（逐股口径，
  # 首次全量约 1.9 小时，之后每天只补当天新增）。此前 relay_data.adjustment_factors_enabled
  # 一直是 false，数据体检里表现为「目录不存在」——是开关问题，不是 Tushare 没有这份数据。
  run_data_step adj_factor uv run python scripts/sync_cn_adj_factor.py --mode auto --days "${DAILY_ADJ_FACTOR_DAYS:-8}" --max-per-run "${DAILY_ADJ_FACTOR_STOCKS:-200}"
fi
if [ "${DAILY_ARM_CONCEPT:-1}" = "1" ]; then
  # 概念/板块轮动手臂：**必须在 boards 之后**（板块成分表当天刷新完才有意义）。
  # 与 arm_selection 并列的独立策略项，产物 output/results_cn/arm_concept_selection.*，
  # 前端「选股结果」的来源下拉与「流水线」页各占一个 tab。
  run_data_step arm_concept uv run python scripts/run_cn_pipeline.py --stage arm_concept
  # 月频回放摘要（前端「概念手臂」tab 上的期均/基准数字）：规则一变就该重算，
  # 与名单一起放在同一步里，避免"名单是新的、回放数字是上个月的"。
  run_data_step arm_concept_backtest uv run python scripts/select_concept_arm.py --backtest --months "${DAILY_ARM_CONCEPT_BACKTEST_MONTHS:-24}"
fi
if [ "${DAILY_PERFORMANCE:-1}" = "1" ]; then
  # 回测绩效页的数据链：**逐笔回放（4 个 arm，实测 ~2.5s/arm）→ portfolio_performance**。
  # 此前这条链完全不在每日流程里，页面窗口停在上一次手工跑（实测 2026-10-10 还停在 10-07）。
  # picks 的月末决策只到研究那次跑出来的最后一个月，重跑解决的是"净值/指标推进到最新交易日"，
  # 新的调仓决策需要重跑 llm_sleeve_oos_backtest（研究口径，另行排期）。
  run_data_step performance uv run python scripts/refresh_performance_chain.py
fi
if [ "${DAILY_LHB:-1}" = "1" ]; then
  run_data_step lhb uv run python scripts/sync_cn_lhb.py --days "${DAILY_LHB_DAYS:-15}"
fi
if [ "${DAILY_SW2021:-1}" = "1" ]; then
  run_data_step sw2021 uv run python scripts/sync_cn_sw2021_industry.py \
      --max-per-run "${DAILY_SW2021_MAX:-4}" --sleep "${DAILY_SW2021_SLEEP:-2.0}"
fi

# ── 分时增量：只补「文件最后日期 < 最新交易日」的标的，且只抓缺失窗口后 --merge 回原文件 ──
# 必须放在主阶段之后：它用日线的最大 trade_date 当"最新交易日"，日线没更新就没得补。
if [ "$FAILED" -eq 0 ] && [ "${DAILY_INTRADAY:-1}" = "1" ]; then
  if [ "$DRY_RUN" -eq 1 ]; then
    log "step=intraday-refresh would run: sync_cn_intraday_full.py --refresh --session-seconds ${DAILY_INTRADAY_SECONDS:-1800}"
  else
    log "intraday-refresh start（5min 增量，最多 ${DAILY_INTRADAY_SECONDS:-1800}s）"
    started=$(date +%s)
    if NO_PROXY='*' uv run python scripts/sync_cn_intraday_full.py --refresh \
         --session-seconds "${DAILY_INTRADAY_SECONDS:-1800}" > "$LOG_DIR/intraday_refresh.log" 2>&1; then
      log "intraday-refresh ok ($(( $(date +%s) - started ))s) -> $LOG_DIR/intraday_refresh.log"
    else
      code=$?
      log "intraday-refresh FAILED exit=${code}（不影响生产名单）-> $LOG_DIR/intraday_refresh.log"
      tail -5 "$LOG_DIR/intraday_refresh.log" | sed 's/^/    /' | tee -a "$LOG_DIR/run.log"
    fi
  fi
fi

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

# ── 归档当日最终名单 + 汇总模拟盘历史 ────────────────────────────────────
# 为什么必须做：`cn_ensemble_selected.csv` **每天被覆盖**，而模拟盘
# （`run_cn_paper_account`）需要的是**一串决策日** —— 喂单日文件时那天永远是最后一根 bar，
# 只会挂 pending、**永不成交**（实测 2026-10-09：nav 496 行恒为 45,000、fills 空表）。
# 这一步归档当天 + 回填历史 + 汇总成 paper_selection_history.csv，模拟盘才跑得起来。
# 放在 paper_account 之后的次日运行里生效（当天的成交要等 T+1 的 bar）。
if [ "$DRY_RUN" -eq 0 ] && [ "${DAILY_ARCHIVE_SELECTION:-1}" = "1" ]; then
  if uv run python scripts/archive_cn_selection.py >> "$LOG_DIR/archive_selection.log" 2>&1; then
    log "archive-selection ok（当日名单已归档，模拟盘历史已汇总）"
  else
    log "archive-selection FAILED（模拟盘历史会缺当天）-> $LOG_DIR/archive_selection.log"
  fi
fi

# ── 选股归因（逐股"为什么被选中"）────────────────────────────────────────
# 两步：explain_selection 生成 LightGBM SHAP + 门禁倾斜分解；pick_attribution_report 汇总成报告。
# 位置：这两个脚本原来一个躺在 output/verification/ 里（一次性产物）、一个在 scripts/ 但从不被调度，
# 导致 output/results_cn/attribution/ 最新只到 2026-09-29（一个月没更新）。
# 成本提醒：explain_selection 要加载 LGB 模型并算全市场 SHAP，shap_detail 产出约 30MB。
if [ "$FAILED" -eq 0 ] && [ "${DAILY_ATTRIBUTION:-1}" = "1" ]; then
  if [ "$DRY_RUN" -eq 1 ]; then
    log "step=attribution would run: explain_selection.py $TRADE_DATE → pick_attribution_report.py $TRADE_DATE"
  else
    started=$(date +%s)
    if uv run python scripts/explain_selection.py "$TRADE_DATE" >> "$LOG_DIR/attribution.log" 2>&1; then
      log "attribution-explain ok（SHAP 分解已生成）"
      if uv run python scripts/pick_attribution_report.py "$TRADE_DATE" >> "$LOG_DIR/attribution.log" 2>&1; then
        log "attribution ok ($(( $(date +%s) - started ))s) -> output/results_cn/attribution/$TRADE_DATE"
      else
        log "attribution-report FAILED（SHAP 有了但报告没生成）-> $LOG_DIR/attribution.log"
      fi
    else
      log "attribution-explain FAILED（会跳过报告）-> $LOG_DIR/attribution.log"
    fi
  fi
fi

# ── 选股台账重建（前端「选股日历」的数据源）──────────────────────────────
# 必须放在 notify 之后：台账要从 feishu_<date>.json 读当日最终名单，那个文件是通知步骤写的。
# 非致命：台账重建失败只影响日历展示，不影响当日生产结果与 production.done。
if [ "$DRY_RUN" -eq 0 ] && [ "$FAILED" -eq 0 ] && [ "${DAILY_LEDGER:-1}" = "1" ]; then
  started=$(date +%s)
  if uv run python scripts/build_selection_ledger.py >> "$LOG_DIR/ledger.log" 2>&1; then
    log "ledger ok ($(( $(date +%s) - started ))s) -> 选股日历已同步 $TRADE_DATE"
  else
    code=$?
    log "ledger FAILED exit=${code}（选股日历会停在上一版）-> $LOG_DIR/ledger.log"
    tail -5 "$LOG_DIR/ledger.log" | sed 's/^/    /' | tee -a "$LOG_DIR/run.log"
  fi
fi

# ── 前瞻收益记录 + 执行假设一致性（第三十二轮新增，都不阻塞生产）───────────
# ① forward-validation：把选股台账里**已实现**的前瞻收益串成长样本记录
#    （preselected 口径 2025-09 起；最终名单口径样本短，两条口径都要在页面上并存，
#     免得拿长样本的漂亮数字当账户业绩）。必须放在 ledger 之后 —— 它读的就是台账。
# ② execution-parity：核对模拟盘与日频回放的执行假设**实际生效值**是否一致
#    （成本/整手/涨跌停/停牌/退市来自同一份 config/execution_assumptions_cn.json），
#    并把快照写到 output/verification/execution_parity.json 供前端展示。
if [ "$DRY_RUN" -eq 0 ] && [ "$FAILED" -eq 0 ] && [ "${DAILY_FORWARD_VALIDATION:-1}" = "1" ]; then
  if uv run python scripts/build_forward_validation.py >> "$LOG_DIR/forward_validation.log" 2>&1; then
    log "forward-validation ok -> output/results_cn/forward_validation/"
  else
    log "forward-validation FAILED -> $LOG_DIR/forward_validation.log"
  fi
fi

if [ "$DRY_RUN" -eq 0 ] && [ "$FAILED" -eq 0 ] && [ "${DAILY_EXEC_PARITY:-1}" = "1" ]; then
  if uv run python scripts/execution_parity_check.py --quiet --json output/verification/execution_parity.json >> "$LOG_DIR/execution_parity.log" 2>&1; then
    log "execution-parity ok（模拟盘与回放执行假设一致）-> output/verification/execution_parity.json"
  else
    log "execution-parity 有不一致项 -> $LOG_DIR/execution_parity.log"
    grep -E "不一致|DIFFER" "$LOG_DIR/execution_parity.log" | tail -5 | sed 's/^/    /' | tee -a "$LOG_DIR/run.log"
  fi
fi

# ── 影子组合：决策逻辑的长期对照（第三十四轮新增，不阻塞生产）─────────────
# 用同一套统一执行假设并列跑 4 条规则（现行 / 现行+rank≤20上限 / 候选池前5等权 / 池前5低波动），
# 名单从台账重建，所以历史立刻回填到 2025-09。**这是"决策逻辑到底行不行"唯一能积累出对照的办法**。
# 必须放在 ledger 之后（它读台账）。开关：DAILY_SHADOW_VARIANTS=0
if [ "$DRY_RUN" -eq 0 ] && [ "$FAILED" -eq 0 ] && [ "${DAILY_SHADOW_VARIANTS:-1}" = "1" ]; then
  if uv run python scripts/run_shadow_variants.py >> "$LOG_DIR/shadow_variants.log" 2>&1; then
    log "shadow-variants ok -> output/paper_trading/shadow/summary.json"
  else
    log "shadow-variants FAILED -> $LOG_DIR/shadow_variants.log"
  fi
fi

# ── PK 护栏日报：下单之前把"不该拿到权重的票"点出来（第三十五轮，不阻塞生产）──
# 背景：第三十四轮实测 6 个 notify 决策日里有 5 天把权重给了 rank>20 的票（最狠的一天 59.5% 的权重
# 给了 rank 4017/4055），而 rank>20 是复盘里唯一通过按决策日符号检验的负向特征（p=0.02）。
# 只读、不改生产权重。开关：DAILY_PK_GUARD=0
if [ "$DRY_RUN" -eq 0 ] && [ "$FAILED" -eq 0 ] && [ "${DAILY_PK_GUARD:-1}" = "1" ]; then
  if uv run python scripts/pk_guard_report.py >> "$LOG_DIR/pk_guard.log" 2>&1; then
    tail -3 "$LOG_DIR/pk_guard.log" | sed 's/^/    /' | tee -a "$LOG_DIR/run.log"
    log "pk-guard ok -> output/results_cn/pk_guard/latest.json"
  else
    log "pk-guard FAILED -> $LOG_DIR/pk_guard.log"
  fi
fi

# ── 上线闸门：影子组合的结果够不够硬（第三十六轮，不阻塞生产）────────────
# 对每条候选改动做四项体检（窗口可比性 / 逐期配对符号检验 / 单票集中度 / 收益集中度），
# 输出 PROMOTE / KEEP / REJECT。**样本不够时它只会说 KEEP**，自动随影子组合累积而升级。
# 它不改任何生产参数：只是把"能不能进生产"变成每天可查的结论。开关：DAILY_PROMOTION_CHECK=0
if [ "$DRY_RUN" -eq 0 ] && [ "$FAILED" -eq 0 ] && [ "${DAILY_PROMOTION_CHECK:-1}" = "1" ]; then
  if uv run python scripts/promotion_check.py >> "$LOG_DIR/promotion_check.log" 2>&1; then
    grep -E "^  \[" "$LOG_DIR/promotion_check.log" | sed 's/^/    /' | tee -a "$LOG_DIR/run.log"
    log "promotion-check ok -> output/results_cn/promotion_check/"
  else
    log "promotion-check FAILED -> $LOG_DIR/promotion_check.log"
  fi
fi

# 因子 IC 显著性/有效性复核（§15 协议）：默认每周日跑一次，失败不影响当日生产结果。
# 只做两件事：① 复跑准入协议并与上一版对比（因子_ic_delta.json）② 把结果写进日志供人工核对。
if [ "$DRY_RUN" -eq 0 ] && [ "${RUN_IC_PROTOCOL:-auto}" != "0" ]; then
  case "${RUN_IC_PROTOCOL:-auto}" in
    1|always) run_protocol=1 ;;
    auto) [ "$(date +%u)" = "7" ] && run_protocol=1 || run_protocol=0 ;;
    *) run_protocol=0 ;;
  esac
  if [ "$run_protocol" = "1" ]; then
    log "ic-protocol start（因子 IC 显著性与有效性复核）"
    if PYTHONPATH=. uv run python scripts/factor_ic_protocol.py --days 756 >> "$LOG_DIR/ic_protocol.log" 2>&1; then
      log "ic-protocol ok（准入清单与漂移记录见 output/research/factor_ic_protocol/）"
    else
      log "ic-protocol FAILED（见 $LOG_DIR/ic_protocol.log；不影响当日生产结果）"
    fi
  fi
fi

# ── 导出同花顺可导入的自选股文件（含 reference 参考账户）────────────────
# 必须放在 shadow-variants **之后**：reference 源读的是参考账户的持仓（第三十七轮）。
# 同花顺**不开放写接口**（自选股在账号云端），所以只能做到"格式对齐 + 生成文件"，
# 人工在同花顺里点一次「导入自选股」。这份文件同时是"每天要买什么"的最终产物，
# 也可以直接拿去别的地方用（Excel/脚本）。
if [ "$DRY_RUN" -eq 0 ] && [ "${DAILY_THS_EXPORT:-1}" = "1" ]; then
  if uv run python scripts/export_ths_watchlist.py >> "$LOG_DIR/ths_export.log" 2>&1; then
    log "ths-export ok（holdings/preselected/arm/all 四个源）-> output/results_cn/ths_watchlist/"
  else
    log "ths-export FAILED（同花顺文件没生成）-> $LOG_DIR/ths_export.log"
  fi
fi

# ── 数据完整性报表：用产物说话，不看"任务有没有 exit 0" ────────────────────
# 独立于上面的 steps：即使某一步失败，这里也会如实列出"哪份数据没到最新交易日"。
if [ "${DAILY_COMPLETENESS:-1}" = "1" ]; then
  if [ "$DRY_RUN" -eq 1 ]; then
    log "step=data-completeness would run: scripts/data_completeness_report.py --quiet"
    log "step=ledger would run: scripts/build_selection_ledger.py（选股日历的数据源）"
    log "step=archive-selection would run: scripts/archive_cn_selection.py（模拟盘历史）"
    log "step=attribution would run: explain_selection.py + pick_attribution_report.py（选股归因）"
    log "step=forward-validation would run: scripts/build_forward_validation.py（前瞻收益记录）"
    log "step=execution-parity would run: scripts/execution_parity_check.py（模拟盘 vs 回放执行假设一致性）"
    log "step=shadow-variants would run: scripts/run_shadow_variants.py（决策逻辑对照：现行 vs 池前5/8/10 vs 降频 vs rank上限）"
    log "step=pk-guard would run: scripts/pk_guard_report.py（PK 护栏：不该拿到权重的票）"
    log "step=promotion-check would run: scripts/promotion_check.py（上线闸门：够不够硬）"
    log "step=ths-export would run: scripts/export_ths_watchlist.py（同花顺自选股 · 五个源，含 reference 参考账户）"
  else
    # 注意：不带 --strict 时这个脚本**退出码恒为 0**，所以不能用退出码判断"是否达标" ——
    # 第一版就是这么写的，于是"未达标 3/8"的那次在 run.log 里被打成了"ok（全部达标）"，
    # 正好和这个报表想解决的问题（"没跑"和"跑了"分不清）撞在一起。
    # 现在用它自己在 stdout 里打的结论行来判断。
    cmp_out="$(uv run python scripts/data_completeness_report.py 2>&1 || true)"
    printf '%s\n' "$cmp_out" >> "$LOG_DIR/run.log"
    summary="$(printf '%s\n' "$cmp_out" | sed -n 's/^\[data-completeness\] //p' | tail -1)"
    if printf '%s' "$summary" | grep -q '未达标 0/'; then
      log "data-completeness ok（8/8 达标）-> output/data_quality/data_completeness.md"
    else
      log "data-completeness 有未达标项：${summary:-未知} -> output/data_quality/data_completeness.md"
      sed -n '/未达标/p' output/data_quality/data_completeness.md 2>/dev/null | tail -1 | sed 's/^/    /' | tee -a "$LOG_DIR/run.log"
    fi
  fi
fi

# ── 策略池同步（2026-10-10 新增）────────────────────────────────────────
# 用户要求："应该有个策略池（模型选股 / arm_select / 行业轮动），agent 挖掘好的新策略应该进入策略池"。
# 这一步把「声明式登记表（config/strategies.json）+ 候选登记（output/strategy_pool/candidates/）
# + 研究证据（output/research/*/REPORT.md）」合成一份前端可读快照，并**点名落后的生产策略**。
# 位置：必须放在 arm_concept / arm_selection 名单写完之后（它读这些产物判新鲜度）；
# 也放在 web-freshness 之前（策略池页面本身也要进体检）。
# 非致命：同步失败只影响「策略池」页展示，不影响当日生产结果。
if [ "$DRY_RUN" -eq 1 ]; then
  log "step=strategy-pool would run: scripts/sync_strategy_pool.py --strict（策略池快照 + 生产策略新鲜度）"
elif [ "$FAILED" -eq 0 ]; then
  pool_out="$(uv run python scripts/sync_strategy_pool.py --strict --print 2>&1 && echo "__POOL_OK__")"
  printf '%s\n' "$pool_out" >> "$LOG_DIR/strategy_pool.log"
  if printf '%s' "$pool_out" | grep -q '__POOL_OK__'; then
    log "strategy-pool ok（生产策略全部到最新交易日）-> output/strategy_pool/strategy_pool.json"
  else
    stale_line="$(printf '%s\n' "$pool_out" | grep -E '^  \[STALE\]' | sed 's/^ *//')"
    log "strategy-pool 有落后的策略：${stale_line:-未知} -> $LOG_DIR/strategy_pool.log"
    notify_alert "策略池有策略未更新 ${TRADE_DATE}" "以下生产策略的最新交易日落后于基准：
${stale_line:-未知}

明细：output/strategy_pool/strategy_pool.md"
  fi
fi

# ── Web 页面新鲜度体检（每日流程的**最后一步**）─────────────────────────────
# 用户要求（2026-10-10）："自动化跑完，所有 web site 全部更新到最新"。
# 这条要求不能靠人肉记：audit_web_freshness.py 给每个页面登记"读哪个产物、怎么取数据日期"，
# 这里跑一次 --strict，不达标的页面**在日志和通知里直接点名**（但不阻塞 .done：
# 数据面本身缺了是上游问题，不该把整条流水线判死）。
if [ "$DRY_RUN" -eq 1 ]; then
  log "step=web-freshness would run: scripts/audit_web_freshness.py --strict（每个页面是否都是最新交易日）"
elif [ "$FAILED" -eq 0 ]; then
  fresh_out="$(uv run python scripts/audit_web_freshness.py --strict 2>&1 || true)"
  printf '%s\n' "$fresh_out" >> "$LOG_DIR/run.log"
  if printf '%s' "$fresh_out" | grep -q '所有每日页面都已是基准交易日'; then
    log "web-freshness ok（所有页面 == 最新交易日）-> output/data_quality/web_freshness.md"
  else
    stale_line="$(printf '%s\n' "$fresh_out" | sed -n 's/^\[web-freshness\] ❌ 不达标：//p' | tail -1)"
    log "web-freshness 有页面未更新：${stale_line:-未知} -> output/data_quality/web_freshness.md"
    notify_alert "Web 页面未更新到最新交易日 ${TRADE_DATE}" "以下页面的数据日期落后于最新交易日：
${stale_line:-未知}

明细：output/data_quality/web_freshness.md"
  fi
fi

if [ "$FAILED" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
  date '+%Y-%m-%dT%H:%M:%S%z' > "$MARKER"
  log "done: $TRADE_DATE 生产完成（当日后续触发会跳过；失败则下一次触发自动重试）"
elif [ "$DRY_RUN" -eq 1 ]; then
  log "dry-run finished: 上面的阶段序列 + sleeve-panel + arm_selection + 研究数据面（boards/adj_factor/arm_concept/回放/performance/lhb/sw2021）+ intraday-refresh + attribution + data-completeness + ledger + archive-selection + ths-export + forward-validation + execution-parity + shadow-variants + pk-guard + promotion-check + strategy-pool + web-freshness 即每日生产实际执行顺序（未执行任何命令）"
else
  log "finished with failures"
fi
exit "$FAILED"
