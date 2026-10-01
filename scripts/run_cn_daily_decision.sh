#!/usr/bin/env bash
# ============================================================================
# 一键决策链路：daily_bars → moneyflow → features → clean_panel → model_scores
#                → preselection → pk
#
# 用法：
#   bash scripts/run_cn_daily_decision.sh                     # 全链路
#   bash scripts/run_cn_daily_decision.sh --from features     # 从某阶段续跑（前序已完成时）
#   bash scripts/run_cn_daily_decision.sh --only preselection,pk   # 只跑指定阶段
#   bash scripts/run_cn_daily_decision.sh --dry-run           # 只打印计划与前置体检
#   bash scripts/run_cn_daily_decision.sh --trade-date 2026-10-09  # 指定决策日（回放口径）
#   bash scripts/run_cn_daily_decision.sh --config config/cn_pipeline_x.toml
#
# 说明：
#   * 每个阶段独立日志，写在 output/verification/decision_<date>/ 下；
#   * 阶段失败立即停止并打印该日志尾部（可用 --from <stage> 续跑）；
#   * `preselection` / `pk` 自动带 --force-rebalance；
#   * 数据健康门（[pipeline.data_health]）由 pipeline 自身在每个受控阶段前执行，
#     覆盖率不达标会中止 —— 先用 --dry-run 看体检结果更省时间；
#   * 跑完输出 output/verification/decision_<date>/SUMMARY.md（名单 + 关键审计）。
# ============================================================================
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
ROOT="$(pwd)"

ALL_STAGES=(daily_bars moneyflow features clean_panel model_scores preselection pk)
CONFIG="config/cn_pipeline.toml"
FROM_STAGE=""
ONLY=""
DRY_RUN=0
TRADE_DATE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --from) FROM_STAGE="${2:-}"; shift 2 ;;
    --only) ONLY="${2:-}"; shift 2 ;;
    --config) CONFIG="${2:-}"; shift 2 ;;
    --trade-date) TRADE_DATE="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

STAGES=("${ALL_STAGES[@]}")
if [ -n "$ONLY" ]; then
  IFS=',' read -r -a STAGES <<< "$ONLY"
elif [ -n "$FROM_STAGE" ]; then
  found=0; tmp=()
  for stage in "${ALL_STAGES[@]}"; do
    [ "$stage" = "$FROM_STAGE" ] && found=1
    [ $found -eq 1 ] && tmp+=("$stage")
  done
  if [ ${#tmp[@]} -eq 0 ]; then echo "--from $FROM_STAGE 不是合法阶段" >&2; exit 2; fi
  STAGES=("${tmp[@]}")
fi

DAY_TAG="$(date +%Y%m%d)"
EV="output/verification/decision_${DAY_TAG}"
mkdir -p "$EV"
LOG="$EV/chain.log"
say() { printf '%s %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG"; }

say "=== 一键决策链路 start（config=$CONFIG, 阶段=${STAGES[*]}${TRADE_DATE:+, trade_date=$TRADE_DATE}）==="

# --- 前置体检：数据覆盖率（只读，不阻断 dry-run）---
PREFLIGHT=".venv/bin/python - <<'PY'
import json, warnings
warnings.filterwarnings('ignore')
from data.ingest.service import MarketDataService
import tomllib
cfg = tomllib.load(open('${CONFIG}', 'rb'))
dh = (cfg.get('pipeline') or {}).get('data_health') or {}
svc = MarketDataService(base_dir='assets/data')
report = svc.check_daily_bar_coverage(lookback_sessions=int(dh.get('lookback_sessions', 10)),
                                      min_ratio=float(dh.get('min_bar_coverage', 0.95)))
print(json.dumps(report, ensure_ascii=False))
PY"
say "[preflight] 数据覆盖率体检：$(eval "$PREFLIGHT" | tail -1)"
if [ "$DRY_RUN" -eq 1 ]; then
  say "[dry-run] 将依次执行：${STAGES[*]}"
  say "[dry-run] 每阶段命令：PYTHONPATH=. .venv/bin/python scripts/run_cn_pipeline.py --stage <stage> $([ -n "$TRADE_DATE" ] && echo "--trade-date $TRADE_DATE")"
  say "=== dry-run 结束（未执行任何阶段）==="
  exit 0
fi

for stage in "${STAGES[@]}"; do
  extra=()
  case "$stage" in
    preselection|pk) extra+=(--force-rebalance) ;;
  esac
  [ -n "$TRADE_DATE" ] && extra+=(--trade-date "$TRADE_DATE")
  t0=$(date +%s)
  # bash 3.2（macOS 自带）+ set -u：空数组展开 "${extra[@]}" 会报 unbound variable。
  # daily_bars / moneyflow 这类没有附加参数的阶段正好命中，必须用 ${arr[@]+"${arr[@]}"} 形式。
  PYTHONPATH=. .venv/bin/python scripts/run_cn_pipeline.py --stage "$stage" --config "$CONFIG" \
    ${extra[@]+"${extra[@]}"} > "$EV/${stage}.log" 2>&1
  rc=$?
  elapsed=$(( $(date +%s) - t0 ))
  say "$stage exit=$rc elapsed=${elapsed}s"
  if [ $rc -ne 0 ]; then
    say "!! $stage 失败，日志尾部："
    tail -5 "$EV/${stage}.log" | tee -a "$LOG"
    say "!! 可用：bash scripts/run_cn_daily_decision.sh --from $stage 续跑"
    exit $rc
  fi
done

# --- 一页摘要 ---
SUMMARY="$EV/SUMMARY.md"
PYTHONPATH=. .venv/bin/python - "$EV" "$SUMMARY" <<'PY' | tee -a "$LOG"
import json, sys, warnings
from pathlib import Path
warnings.filterwarnings("ignore")
import pandas as pd

ev, out = Path(sys.argv[1]), Path(sys.argv[2])
lines = ["# 决策链路摘要", "", f"生成时间：{pd.Timestamp.now():%F %T}", ""]
sel_path = Path("output/results_cn/cn_ensemble_selected.csv")
if sel_path.is_file():
    sel = pd.read_csv(sel_path)
    hold = sel[sel["target_weight"] > 0] if "target_weight" in sel.columns else sel
    lines += [f"决策日：**{sel['trade_date'].iloc[0]}**", "", "## 名单", "",
              "| 代码 | 权重 | 通道 | 模型分 | 排名 | 收盘 | 手数 |", "|---|---|---|---|---|---|---|"]
    for row in hold.itertuples():
        lines.append(f"| {row.stock_code} | {row.target_weight:.2%} | {getattr(row, 'selection_channel', '-')} | "
                     f"{getattr(row, 'model_score', float('nan')):.1f} | {int(getattr(row, 'rank', 0))} | "
                     f"{getattr(row, 'last_close', float('nan')):.2f} | {int(getattr(row, 'lots_at_target', 0))} |")
    model = float(hold[hold.get("selection_channel", "model") == "model"]["target_weight"].sum())
    lines += ["", f"- 模型块权重 **{model:.1%}** / 卫星块 **{hold['target_weight'].sum() - model:.1%}** / "
                  f"合计 **{hold['target_weight'].sum():.1%}**（目标 gross 见配置）", ""]
reports = sorted(Path("output/pipeline_reports").glob("cn_pipeline_*.json"))
if reports:
    report = json.loads(reports[-1].read_text())
    def walk(o, key):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == key: yield v
                yield from walk(v, key)
        elif isinstance(o, list):
            for item in o:
                yield from walk(item, key)
    for key in ("sleeve_block_cap", "strategy_state", "input_guard"):
        hit = next(walk(report, key), None)
        if hit is not None:
            lines += [f"## {key}", "", "```json", json.dumps(hit, ensure_ascii=False, indent=2)[:1200], "```", ""]
out.write_text("\n".join(lines), encoding="utf-8")
print(f"[summary] {out}")
PY

say "=== 全链路完成（摘要：${SUMMARY}）==="
