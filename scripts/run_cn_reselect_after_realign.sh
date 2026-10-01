#!/usr/bin/env bash
# 等特征对齐重算收尾（realign.done 出现）后，自动重跑 preselection -> pk，
# 出 2026-09-24 的正式名单（今天 09:36 那份作废；13:13 那版也会被对齐后的分数覆盖）。
#
# 由 launchd（comp.quant.cn-reselect，StartInterval=300）反复触发：
#   - realign.done 未出现 -> 直接退出（等下一轮）
#   - 已跑完（reselect.done 存在）-> 直接退出
#   - 否则跑 preselection（--force-rebalance）-> pk -> 渲染两阶段报告 -> 写 reselect.done
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
ROOT="$(pwd)"
OUT="$ROOT/output/verification/cn_base_data_repair_20260926"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$OUT/reselect.log"; }

if [ ! -f "$OUT/realign.done" ]; then
  log "wait: realign.done 尚未出现，跳过本轮"
  exit 0
fi
if [ -f "$OUT/reselect.done" ]; then
  log "skip: 已完成（reselect.done）"
  exit 0
fi

log "start reselection（数据源: 对齐后的特征 + 重建后的面板）"
PYTHONPATH=. .venv/bin/python scripts/run_cn_pipeline.py --stage preselection --force-rebalance \
  > "$OUT/reselect_preselection.log" 2>&1
rc_pre=$?
log "preselection exit=$rc_pre"
if [ "$rc_pre" -ne 0 ]; then
  log "preselection 失败，留待下一轮重试"; exit 1
fi

PYTHONPATH=. .venv/bin/python scripts/run_cn_pipeline.py --stage pk --force-rebalance \
  > "$OUT/reselect_pk.log" 2>&1
rc_pk=$?
log "pk exit=$rc_pk"
if [ "$rc_pk" -ne 0 ]; then
  log "pk 失败，留待下一轮重试"; exit 1
fi

PYTHONPATH=. .venv/bin/python scripts/render_two_stage_report.py >> "$OUT/reselect.log" 2>&1
log "two-stage report exit=$?"

# 摘要：来源日、候选数、持仓数、毛敞口
PYTHONPATH=. .venv/bin/python - "$OUT/reselect_summary.json" <<'PY' | tee -a "$OUT/reselect.log"
import json, sys
from pathlib import Path
out = Path(sys.argv[1])
sel = Path("output/results_cn/cn_ensemble_selected.csv")
pre = Path("output/results_cn/cn_ensemble_preselected.csv")
summary = {}
if pre.is_file():
    import pandas as pd
    p = pd.read_csv(pre)
    summary["preselected"] = int(len(p))
    summary["preselect_origin_date"] = str(p["trade_date"].iloc[0]) if "trade_date" in p.columns else None
    summary["model_candidates"] = int((p.get("selection_channel") == "model").sum()) if "selection_channel" in p.columns else None
if sel.is_file():
    import pandas as pd
    s = pd.read_csv(sel)
    held = s[s.get("target_weight", 0).astype(float) > 0] if "target_weight" in s.columns else s
    summary["held"] = int(len(held))
    summary["gross"] = round(float(held["target_weight"].sum()), 4) if "target_weight" in held.columns else None
    if "stock_code" in held.columns:
        summary["codes"] = held["stock_code"].tolist()
out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print("RESELECT_SUMMARY", json.dumps(summary, ensure_ascii=False))
PY

printf '%s completed %s\n' "$(date '+%F %T')" "$(cat "$OUT/realign.done" 2>/dev/null | head -c 80)" > "$OUT/reselect.done"
log "done: reselect.done 已写入"
