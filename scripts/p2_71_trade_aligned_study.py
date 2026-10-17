#!/usr/bin/env python3
"""Pilot models whose label follows the stop/hold/fee execution contract."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from p2_71_model_matrix import ROOT, load_data
from backend.services.factor_model_matrix import ModelSpec, fit_model, score_model
from backend.services.factor_moneyflow import equivalent_spec
from backend.services.factor_replay import ReplayConfig, metrics, replay
from backend.services.factor_trade_objective import trade_labels


def blend_rank(scored: pd.DataFrame, original: pd.DataFrame, alpha: float) -> pd.DataFrame:
    """Keep the rule score as an anchor and let ML make a bounded rank tilt."""
    base = original[["stock_code", "trade_date", "score"]].rename(columns={"score": "rule_score"})
    out = scored.merge(base, on=["stock_code", "trade_date"], validate="one_to_one")
    out["rule_rank"] = out.groupby("trade_date").rule_score.rank(method="first", pct=True)
    out["model_rank"] = out.groupby("trade_date").score.rank(method="first", pct=True)
    out["score"] = (1.0 - alpha) * out.rule_rank + alpha * out.model_rank
    out["cause_width"] = -out.score
    return out.sort_values(["trade_date", "cause_width", "stock_code"], kind="stable").reset_index(drop=True)


def run(output: Path):
    protocol = json.loads((ROOT / "config/p2_71_lightgbm.json").read_text())
    output.mkdir(parents=True, exist_ok=True)
    bars, signals, x, benchmark, calendar, _ = load_data(protocol)
    spec = equivalent_spec(); execution = spec["execution"]
    config = ReplayConfig(**{k: execution[k] for k in ["max_positions", "max_hold", "stop_pct", "stop_basis", "holding_clock", "cost_one_way"]})
    train = trade_labels(x, bars, calendar, start=protocol["train"][0], end=protocol["train"][1], boundary=pd.Timestamp(protocol["validation"][0]), horizon=config.max_hold, stop_pct=config.stop_pct, cost_one_way=config.cost_one_way)
    valid_labels = trade_labels(x, bars, calendar, start=protocol["validation"][0], end=protocol["validation"][1], boundary=pd.Timestamp(protocol["evaluation"][0]), horizon=config.max_hold, stop_pct=config.stop_pct, cost_one_way=config.cost_one_way)
    eval_labels = trade_labels(x, bars, calendar, start=protocol["evaluation"][0], end=protocol["evaluation"][1], boundary=pd.Timestamp("2100-01-01"), horizon=config.max_hold, stop_pct=config.stop_pct, cost_one_way=config.cost_one_way)
    valid_days = calendar[(calendar >= pd.Timestamp(protocol["validation"][0])) & (calendar <= pd.Timestamp(protocol["validation"][1]))]
    eval_days = calendar[(calendar >= pd.Timestamp(protocol["evaluation"][0])) & (calendar <= pd.Timestamp(protocol["evaluation"][1]))]

    def run_replay(frame, days, start, end):
        return replay(frame[frame.trade_date.between(start, end)], bars[bars.trade_date.isin(days)], benchmark, config, days)

    base_valid = run_replay(signals, valid_days, *protocol["validation"]); base_eval = run_replay(signals, eval_days, *protocol["evaluation"])
    base_vm, base_tm = metrics(base_valid["nav"]), metrics(base_eval["nav"])
    candidates = []
    frozen_specs = [s for s in (ModelSpec("ridge", "regression", {"alpha": 10.0}), ModelSpec("extra_trees", "regression", {"n_estimators": 300, "min_samples_leaf": 30, "max_features": 0.8}), ModelSpec("hist_gradient_boosting", "regression", {"max_iter": 150, "max_leaf_nodes": 15, "learning_rate": 0.03, "l2_regularization": 1.0}))]
    for ms in frozen_specs:
        model = fit_model(ms, train)
        raw = score_model(model, ms, x)
        for alpha in (0.25, 0.50):
            pred = blend_rank(raw, x, alpha)
            vr = run_replay(pred, valid_days, *protocol["validation"]); er = run_replay(pred, eval_days, *protocol["evaluation"])
            vm, em = metrics(vr["nav"]), metrics(er["nav"])
            candidates.append({"model": ms.name, "alpha": alpha, "validation_cagr": vm["calendar_cagr"], "validation_drawdown": vm["max_drawdown"], "evaluation_cagr": em["calendar_cagr"], "evaluation_drawdown": em["max_drawdown"], "evaluation_returns": len(eval_days)-1, "validation_trade_label_rows": len(valid_labels), "evaluation_trade_label_rows": len(eval_labels), "validation_gate": bool(vm["calendar_cagr"] >= base_vm["calendar_cagr"] and abs(vm["max_drawdown"]) <= abs(base_vm["max_drawdown"])+.03), "evaluation_cagr_gate": bool(em["calendar_cagr"] >= base_tm["calendar_cagr"]), "status": "RETAIN_BASELINE"})
            if alpha == .25 or True:
                pd.DataFrame({"nav": er["nav"], "benchmark_nav": er["benchmark_nav"]}).to_csv(output / f"{ms.name}_alpha_{int(alpha*100)}_evaluation_nav.csv")
    valid_candidates = [r for r in candidates if r["validation_gate"]]
    selected = max(valid_candidates, key=lambda r: (r["validation_cagr"], -r["alpha"], r["model"])) if valid_candidates else None
    if selected:
        selected["frozen_before_evaluation"] = True
        selected["status"] = "CANDIDATE_OBSERVED_NOT_PROMOTED" if selected["evaluation_cagr_gate"] and selected["evaluation_returns"] >= 252 else "RETAIN_BASELINE"
    result = {"status": "RETAIN_BASELINE", "objective": "execution_aligned_trade_label", "models": candidates, "validation_selected": selected, "baseline_validation": base_vm, "baseline_evaluation": base_tm, "holdout_not_used_for_selection": True, "production_changed": False, "created_at": datetime.now(timezone.utc).isoformat(), "remaining": [">=252 holdout returns", "PIT/publication-time checks", "capacity and forward shadow"]}
    (output / "manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    lines = ["# P2.71 交易目标对齐因子模型", "", "状态：**RETAIN_BASELINE**。模型标签跟随次日开盘、8%止损、10日到期和双边费用；公共账本仍是最终验收。", "", "| 模型 | 混合权重 | 验证CAGR | 留出CAGR | 留出回撤 | 验证门禁 | 状态 |", "|---|---:|---:|---:|---:|:---:|---|"]
    for r in candidates:
        lines.append(f"| {r['model']} | {r['alpha']:.2f} | {r['validation_cagr']:+.4%} | {r['evaluation_cagr']:+.4%} | {r['evaluation_drawdown']:+.4%} | {r['validation_gate']} | {r['status']} |")
    lines += ["", f"共同基线验证CAGR：{base_vm['calendar_cagr']:+.4%}；留出CAGR：{base_tm['calendar_cagr']:+.4%}；留出观测：{len(eval_days)-1}（少于252，不允许晋级）。", f"交易标签样本：训练{len(train)}、验证{len(valid_labels)}、留出{len(eval_labels)}；留出仅用于冻结后诊断，未参与模型/权重选择。", "", "这轮实验只验证‘目标函数错配’是否是主要原因；即使标签对齐，也必须通过更长留出、PIT、容量和前向影子门禁。", "", "[manifest](manifest.json)"]
    (output / "REPORT.md").write_text("\n".join(lines) + "\n")
    print(f"RETAIN_BASELINE candidates={len(candidates)} selected={selected['model']+'@'+str(selected['alpha']) if selected else 'none'} holdout_returns={len(eval_days)-1}")
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--output", type=Path, default=ROOT / "output/research/p2_71/trade_aligned_v1"); run(ap.parse_args().output.resolve())

