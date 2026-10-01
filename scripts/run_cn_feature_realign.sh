#!/usr/bin/env bash
# R12 对齐：用“生产同一约定”重算受影响窗口的特征，然后重建面板并重新打分。
#
#   bash scripts/run_cn_feature_realign.sh [write_days] [warmup_days]
# 默认 30 / 515 —— 总历史 545 天，与每日生产（days=365 + warmup_days=180）一致，
# 这样累计型指标（OBV/AD/ADOSC/GTJA 累计和）的“水位”与日常批次相同。
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
ROOT="$(pwd)"
WRITE_DAYS="${1:-30}"
WARMUP_DAYS="${2:-515}"
LOG_DIR="$ROOT/output/verification/cn_base_data_repair_20260926"
mkdir -p "$LOG_DIR"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_DIR/realign.log"; }

log "start realign write_days=$WRITE_DAYS warmup_days=$WARMUP_DAYS"
PYTHONPATH=. .venv/bin/python output/verification/p1_22_p0_rebuild_20260924/rebuild_volume_features.py "$WRITE_DAYS" "$WARMUP_DAYS" --force \
  > "$LOG_DIR/realign_features.log" 2>&1
log "features exit=$?"

PYTHONPATH=. .venv/bin/python scripts/run_cn_pipeline.py --stage clean_panel \
  > "$LOG_DIR/realign_clean_panel.log" 2>&1
log "clean_panel exit=$?"

PYTHONPATH=. .venv/bin/python scripts/run_cn_pipeline.py --stage model_scores \
  > "$LOG_DIR/realign_model_scores.log" 2>&1
log "model_scores exit=$?"

grep -h "input guard" "$LOG_DIR/realign_model_scores.log" | tail -1 | sed 's/^/guard: /' | tee -a "$LOG_DIR/realign.log"
log "done"
