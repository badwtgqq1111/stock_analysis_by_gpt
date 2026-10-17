#!/usr/bin/env python3
"""Run a prespecified probability-semantic factor composition pilot."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from p2_71_model_matrix import ROOT, load_data
from backend.services.factor_lightgbm import labels
from backend.services.factor_moneyflow import equivalent_spec
from backend.services.factor_probability import EmpiricalProbability
from backend.services.factor_replay import ReplayConfig, metrics, replay


FACTORS = ("mf_strength", "mf_persistence", "mf_return20", "price_to_ma20", "log_amount20", "log_price")


def score_frame(frame, mapper, method):
    out = frame.copy()
    probs = mapper.transform(out)
    if method == "independence_product":
        out["score"] = probs.prod(axis=1)
    elif method == "logit_sum":
        out["score"] = mapper.logit_sum(out)
    elif method == "rule_probability_blend25":
        p = probs.prod(axis=1).groupby(out.trade_date).rank(method="first", pct=True)
        rule = out.groupby("trade_date").mf_strength.rank(method="first", pct=True, ascending=False)
        out["score"] = .75 * rule + .25 * p
    else:
        raise ValueError(method)
    out["cause_width"] = -out.score
    return out.sort_values(["trade_date", "cause_width", "stock_code"], kind="stable").reset_index(drop=True)


def run(output: Path):
    protocol = json.loads((ROOT / "config/p2_71_lightgbm.json").read_text())
    output.mkdir(parents=True, exist_ok=True)
    bars, signals, x, benchmark, calendar, _ = load_data(protocol)
    train = labels(x, bars, calendar, *protocol["train"], boundary=protocol["validation"][0], horizon=protocol["horizon_sessions"])
    mapper = EmpiricalProbability.fit(train, FACTORS, direction=[1., 1., 1., 1., 1., 1.])
    spec = equivalent_spec(); execution = spec["execution"]
    config = ReplayConfig(**{k: execution[k] for k in ["max_positions", "max_hold", "stop_pct", "stop_basis", "holding_clock", "cost_one_way"]})
    valid_days = calendar[(calendar >= pd.Timestamp(protocol["validation"][0])) & (calendar <= pd.Timestamp(protocol["validation"][1]))]
    eval_days = calendar[(calendar >= pd.Timestamp(protocol["evaluation"][0])) & (calendar <= pd.Timestamp(protocol["evaluation"][1]))]
    base_valid = replay(signals[signals.trade_date.between(protocol["validation"][0], protocol["validation"][1])], bars[bars.trade_date.isin(valid_days)], benchmark, config, valid_days)
    base_eval = replay(signals[signals.trade_date.between(protocol["evaluation"][0], protocol["evaluation"][1])], bars[bars.trade_date.isin(eval_days)], benchmark, config, eval_days)
    base_vm, base_em = metrics(base_valid["nav"]), metrics(base_eval["nav"])
    rows = []
    methods = ["independence_product", "logit_sum", "rule_probability_blend25"]
    for method in methods:
        scored = score_frame(x, mapper, method)
        vr = replay(scored[scored.trade_date.between(protocol["validation"][0], protocol["validation"][1])], bars[bars.trade_date.isin(valid_days)], benchmark, config, valid_days)
        er = replay(scored[scored.trade_date.between(protocol["evaluation"][0], protocol["evaluation"][1])], bars[bars.trade_date.isin(eval_days)], benchmark, config, eval_days)
        vm, em = metrics(vr["nav"]), metrics(er["nav"])
        vpass = vm["calendar_cagr"] >= base_vm["calendar_cagr"] and abs(vm["max_drawdown"]) <= abs(base_vm["max_drawdown"]) + .03
        rows.append({"method": method, "validation_cagr": vm["calendar_cagr"], "validation_drawdown": vm["max_drawdown"], "validation_sharpe": vm["sharpe"], "evaluation_cagr": em["calendar_cagr"], "evaluation_drawdown": em["max_drawdown"], "evaluation_sharpe": em["sharpe"], "validation_gate": vpass, "evaluation_cagr_gate": em["calendar_cagr"] >= base_em["calendar_cagr"], "evaluation_returns": len(eval_days)-1, "status": "RETAIN_BASELINE"})
        pd.DataFrame({"nav": er["nav"], "benchmark_nav": er["benchmark_nav"]}).to_csv(output / f"{method}_evaluation_nav.csv")
    result = {"status": "RETAIN_BASELINE", "methods": rows, "baseline_validation": base_vm, "baseline_evaluation": base_em, "probability_semantics": "empirical CDF fit on train; product is independence approximation; logit sum is a rank score, not calibrated probability", "holdout_not_used_for_selection": True, "production_changed": False, "created_at": datetime.now(timezone.utc).isoformat(), "remaining": [">=252 holdout returns", "PIT/publication-time checks", "capacity and forward shadow"]}
    (output / "manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    lines = ["# P2.71 概率语义因子组合研究", "", "状态：**RETAIN_BASELINE**。概率变换只改变硬AND可行域内排序，未替换生产基线。", "", "| 方法 | 验证CAGR | 验证回撤 | 留出CAGR | 留出回撤 | 验证门禁 | 状态 |", "|---|---:|---:|---:|---:|:---:|---|"]
    for r in rows:
        lines.append(f"| {r['method']} | {r['validation_cagr']:+.4%} | {r['validation_drawdown']:+.4%} | {r['evaluation_cagr']:+.4%} | {r['evaluation_drawdown']:+.4%} | {r['validation_gate']} | {r['status']} |")
    lines += ["", f"基线验证CAGR：{base_vm['calendar_cagr']:+.4%}；基线留出CAGR：{base_em['calendar_cagr']:+.4%}；留出观测：{len(eval_days)-1}（少于252，不允许晋级）。", "概率解释：P(A∩B)只有在独立性假设下才近似P(A)P(B)；实际相关性由联合事件/条件概率决定。本实验的乘积仅作显式的独立性近似，不作为事实概率。", "", "[manifest](manifest.json)"]
    (output / "REPORT.md").write_text("\n".join(lines) + "\n")
    print(f"RETAIN_BASELINE methods={len(rows)} holdout_returns={len(eval_days)-1}")
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--output", type=Path, default=ROOT / "output/research/p2_71/probability_v1"); run(ap.parse_args().output.resolve())
