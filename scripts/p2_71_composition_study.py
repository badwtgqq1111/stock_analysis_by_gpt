#!/usr/bin/env python3
"""Run the first prespecified mathematical factor-composition study.

Hard ``AND`` gates stay unchanged.  Only the ranking score for eligible names
changes, and every learned direction/weight/PCA component is fit on train.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from p2_71_model_matrix import ROOT, load_data
from backend.services.factor_composition import PCAComposer, weighted_rank_score
from backend.services.factor_lightgbm import labels
from backend.services.factor_moneyflow import equivalent_spec
from backend.services.factor_replay import ReplayConfig, metrics, replay


FEATURES = ("mf_strength", "mf_persistence", "mf_return20", "price_to_ma20", "log_amount20", "log_price")


def _rank_ic_weights(train: pd.DataFrame) -> tuple[dict[str, float], dict[str, bool], dict[str, float]]:
    directions = {"mf_strength": False, "mf_persistence": False, "mf_return20": False, "price_to_ma20": False, "log_amount20": False, "log_price": False}
    corrs = {}
    for col in FEATURES:
        values = []
        for _, day in train.groupby("trade_date"):
            if len(day) >= 2 and day[col].nunique() > 1 and day.label.nunique() > 1:
                values.append(day[col].rank().corr(day.label.rank()))
        corrs[col] = float(pd.Series(values).mean()) if values else 0.0
    # For return20, lower absolute return is the prespecified quality direction.
    # The raw signed return is retained in the table for audit, but excluded from
    # the learned ranker to avoid an unregistered sign convention.
    corrs["mf_return20"] = 0.0
    weights = {col: abs(corrs[col]) for col in FEATURES}
    for col, corr in corrs.items():
        if abs(corr) > 1e-12:
            directions[col] = corr < 0
    return weights, directions, corrs


def _score(frame: pd.DataFrame, method: str, train: pd.DataFrame, pca=None, ic=None):
    out = frame.copy()
    out["return20_abs"] = -out["mf_return20"].abs()
    if method == "baseline":
        score = out["mf_strength"].astype(float)
    elif method == "equal_rank":
        score = weighted_rank_score(out, {"mf_strength": 1, "mf_persistence": 1, "return20_abs": 1, "price_to_ma20": 1, "log_amount20": 1, "log_price": 1}, ascending={"mf_strength": False, "mf_persistence": False, "return20_abs": False, "price_to_ma20": False, "log_amount20": False, "log_price": False})
    elif method == "ic_weighted":
        weights, directions, _ = ic
        weights = dict(weights); weights["return20_abs"] = weights.pop("mf_return20")
        dirs = dict(directions); dirs["return20_abs"] = False
        score = weighted_rank_score(out, weights, ascending=dirs)
    elif method == "pca":
        score = pca.score(out)
    elif method == "pca_blend25":
        base = weighted_rank_score(out, {"mf_strength": 1}, ascending={"mf_strength": False})
        pc = pca.score(out)
        pc_rank = pc.groupby(out.trade_date).rank(method="first", pct=True)
        score = .75 * base + .25 * pc_rank
    else:
        raise ValueError(method)
    out["score"] = score
    out["cause_width"] = -out["score"]
    return out.sort_values(["trade_date", "cause_width", "stock_code"], kind="stable").reset_index(drop=True)


def run(output: Path):
    protocol = json.loads((ROOT / "config/p2_71_lightgbm.json").read_text())
    output.mkdir(parents=True, exist_ok=True)
    bars, signals, x, benchmark, calendar, _ = load_data(protocol)
    train = labels(x, bars, calendar, *protocol["train"], boundary=protocol["validation"][0], horizon=protocol["horizon_sessions"])
    spec = equivalent_spec(); execution = spec["execution"]
    config = ReplayConfig(**{k: execution[k] for k in ["max_positions", "max_hold", "stop_pct", "stop_basis", "holding_clock", "cost_one_way"]})
    valid_days = calendar[(calendar >= pd.Timestamp(protocol["validation"][0])) & (calendar <= pd.Timestamp(protocol["validation"][1]))]
    eval_days = calendar[(calendar >= pd.Timestamp(protocol["evaluation"][0])) & (calendar <= pd.Timestamp(protocol["evaluation"][1]))]
    ic = _rank_ic_weights(train)
    pca = PCAComposer.fit(train, FEATURES, label="label")
    methods = ["baseline", "equal_rank", "ic_weighted", "pca", "pca_blend25"]
    rows = []
    base_valid = replay(signals[signals.trade_date.between(protocol["validation"][0], protocol["validation"][1])], bars[bars.trade_date.isin(valid_days)], benchmark, config, valid_days)
    base_eval = replay(signals[signals.trade_date.between(protocol["evaluation"][0], protocol["evaluation"][1])], bars[bars.trade_date.isin(eval_days)], benchmark, config, eval_days)
    base_vm, base_em = metrics(base_valid["nav"]), metrics(base_eval["nav"])
    for method in methods:
        scored = _score(x, method, train, pca=pca, ic=ic)
        vr = replay(scored[scored.trade_date.between(protocol["validation"][0], protocol["validation"][1])], bars[bars.trade_date.isin(valid_days)], benchmark, config, valid_days)
        er = replay(scored[scored.trade_date.between(protocol["evaluation"][0], protocol["evaluation"][1])], bars[bars.trade_date.isin(eval_days)], benchmark, config, eval_days)
        vm, em = metrics(vr["nav"]), metrics(er["nav"])
        rows.append({"method": method, "validation_cagr": vm["calendar_cagr"], "validation_drawdown": vm["max_drawdown"], "evaluation_cagr": em["calendar_cagr"], "evaluation_drawdown": em["max_drawdown"], "evaluation_returns": len(eval_days)-1, "validation_gate": bool(vm["calendar_cagr"] >= base_vm["calendar_cagr"] and abs(vm["max_drawdown"]) <= abs(base_vm["max_drawdown"])+.03), "evaluation_cagr_gate": bool(em["calendar_cagr"] >= base_em["calendar_cagr"]), "status": "RETAIN_BASELINE"})
        pd.DataFrame({"nav": er["nav"], "benchmark_nav": er["benchmark_nav"]}).to_csv(output / f"{method}_evaluation_nav.csv")
    result = {"status": "RETAIN_BASELINE", "methods": rows, "baseline_validation": base_vm, "baseline_evaluation": base_em, "train_rank_ic": ic[2], "pca_component": pca.component_.tolist(), "pca_label_sign": pca.label_sign, "holdout_not_used_for_selection": True, "production_changed": False, "created_at": datetime.now(timezone.utc).isoformat(), "remaining": [">=252 holdout returns", "PIT/publication-time checks", "capacity and forward shadow"]}
    (output / "manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    lines = ["# P2.71 数学因子组合研究", "", "状态：**RETAIN_BASELINE**。AND 仍是硬门槛；组合方法只改变门槛内排序。PCA只在train拟合。", "", "| 方法 | 验证CAGR | 留出CAGR | 留出回撤 | 验证门禁 | 状态 |", "|---|---:|---:|---:|:---:|---|"]
    for r in rows:
        lines.append(f"| {r['method']} | {r['validation_cagr']:+.4%} | {r['evaluation_cagr']:+.4%} | {r['evaluation_drawdown']:+.4%} | {r['validation_gate']} | {r['status']} |")
    lines += ["", f"基线验证CAGR：{base_vm['calendar_cagr']:+.4%}；基线留出CAGR：{base_em['calendar_cagr']:+.4%}；留出观测：{len(eval_days)-1}（少于252，不允许晋级）。", "训练得到的IC权重：" + json.dumps(ic[2], ensure_ascii=False), "PCA分量：" + json.dumps(pca.component_.tolist()), "", "[manifest](manifest.json)"]
    (output / "REPORT.md").write_text("\n".join(lines) + "\n")
    print(f"RETAIN_BASELINE methods={len(rows)} holdout_returns={len(eval_days)-1}")
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--output", type=Path, default=ROOT / "output/research/p2_71/composition_v1"); run(ap.parse_args().output.resolve())

