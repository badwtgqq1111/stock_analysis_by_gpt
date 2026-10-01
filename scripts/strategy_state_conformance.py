#!/usr/bin/env python
"""策略 state 一致性体检：声明的入场状态 vs 头部候选的实际形态。

用法： PYTHONPATH=. .venv/bin/python scripts/strategy_state_conformance.py 2026-09-29 [--score-column model_score_raw]
输出： output/research/strategy_conformance/<date>.json + 控制台结论
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from factor_engine.strategies import load_strategies, state_conformance  # noqa: E402

GATE = ROOT / "output/verification/p1_22_shap_attribution_20260925"


def main() -> int:
    import tomllib
    ap = argparse.ArgumentParser()
    ap.add_argument("trade_date")
    ap.add_argument("--score-column", default="model_score_raw")
    ap.add_argument("--top-k", type=int, default=20)
    args = ap.parse_args()
    date = args.trade_date
    out_dir = ROOT / "output/research/strategy_conformance"
    out_dir.mkdir(parents=True, exist_ok=True)

    gate_path = GATE / f"gate_decomposition_{date}.csv"
    if not gate_path.is_file():
        raise SystemExit(f"缺少 {gate_path}（先跑 explain_selection.py {date}）")
    frame = pd.read_csv(gate_path).rename(columns={"flow_z_5d": "flow_z"})
    scores = pd.read_csv(ROOT / "output/model_scores/cn_lightgbm_scores.csv")
    if "model_score_raw" not in scores.columns:
        raise SystemExit("model_scores 文件缺少 model_score_raw")
    frame = frame.merge(scores[["stock_code", args.score_column]], on="stock_code", how="inner")

    specs = load_strategies(tomllib.load(open(ROOT / "config/cn_pipeline.toml", "rb")))
    # 只有"按模型分排序"的策略才可以用模型分 Top-k 做体检；recipe 驱动的腿（donchian/value_reversal/
    # bottom_momentum）的头部来自各自信号函数，不在这个帧里，标 n/a 而不是给一个误导性的 mismatch。
    def model_driven(spec) -> bool:
        signal = spec.signal or {}
        return str(signal.get("source", "")).lower() == "lightgbm" or str(signal.get("rank", "")).lower() == "model_score"

    results = {}
    for sid, spec in specs.items():
        if not model_driven(spec):
            results[sid] = {"strategy_id": sid, "verdict": "n/a",
                            "reason": "recipe 驱动的腿：头部来自信号函数，需用信号帧单独体检"}
        else:
            results[sid] = state_conformance(frame, spec, score_column=args.score_column, top_k=args.top_k)
    report = {"trade_date": date, "score_column": args.score_column, "top_k": args.top_k, "strategies": results}
    (out_dir / f"{date}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"# 策略 state 一致性体检 {date}（Top-{args.top_k} by {args.score_column}）")
    for sid, row in report["strategies"].items():
        if row["verdict"] == "n/a":
            print(f"  {sid:<20} n/a        ({row.get('reason')})")
        else:
            print(f"  {sid:<20} {row['verdict']:<10} 头部命中 {row['head_in_state_share']:.0%} vs 全市场 "
                  f"{row['market_in_state_share']:.0%} | lift {row['lift']}")
    print(f"\n[done] {out_dir / (date + '.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
