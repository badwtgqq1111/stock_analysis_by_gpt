#!/usr/bin/env bash
# 修复“分组批读丢行组”后重建面板并重新打分（一次性任务）。
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
ROOT="$(pwd)"
OUT="$ROOT/output/verification/cn_base_data_repair_20260926"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$OUT/panel_rescore.log"; }
log "start panel+rescore"
PYTHONPATH=. .venv/bin/python scripts/run_cn_pipeline.py --stage clean_panel > "$OUT/panel_rescore_clean_panel.log" 2>&1
log "clean_panel exit=$?"
PYTHONPATH=. .venv/bin/python scripts/run_cn_pipeline.py --stage model_scores > "$OUT/panel_rescore_model_scores.log" 2>&1
log "model_scores exit=$?"
grep -h "input guard" "$OUT/panel_rescore_model_scores.log" | tail -1 | sed 's/^/guard: /' | tee -a "$OUT/panel_rescore.log"
log "done"
