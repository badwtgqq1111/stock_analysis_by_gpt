#!/usr/bin/env python3
"""Compare research-only drawdown overlays on the probability/logit path."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from p2_71_model_matrix import ROOT, load_data
from p2_71_probability_study import score_frame
from backend.services.factor_lightgbm import labels
from backend.services.factor_moneyflow import equivalent_spec
from backend.services.factor_probability import EmpiricalProbability
from backend.services.factor_replay import ReplayConfig, metrics, replay


def regime_filter(signals: pd.DataFrame, benchmark: pd.Series, *, window=20, min_return=-0.02):
    """Use only information in the signal day's benchmark close."""
    b = pd.Series(benchmark).sort_index().astype(float)
    ma = b.rolling(window).mean()
    ret = b / b.shift(window) - 1
    allowed = ((b > ma) & (ret >= min_return)).rename("regime_ok")
    out = signals.merge(allowed, left_on="trade_date", right_index=True, how="left")
    return out[out.regime_ok.fillna(False)].drop(columns="regime_ok").copy()


def volatility_filter(signals: pd.DataFrame, bars: pd.DataFrame, quantile=0.7, window=20):
    b = bars.sort_values(["stock_code", "trade_date"]).copy()
    b["ret"] = b.groupby("stock_code").close.pct_change()
    b["vol20"] = b.groupby("stock_code").ret.transform(lambda s: s.rolling(window).std())
    q = b.groupby("trade_date").vol20.transform(lambda s: s.quantile(quantile))
    b["vol_ok"] = b.vol20 <= q
    return signals.merge(b[["stock_code", "trade_date", "vol_ok"]], on=["stock_code", "trade_date"], validate="one_to_one").query("vol_ok == True").drop(columns="vol_ok")


def run(output: Path):
    protocol = json.loads((ROOT / "config/p2_71_lightgbm.json").read_text())
    output.mkdir(parents=True, exist_ok=True)
    bars, signals, x, benchmark, calendar, _ = load_data(protocol)
    train = labels(x, bars, calendar, *protocol["train"], boundary=protocol["validation"][0], horizon=protocol["horizon_sessions"])
    mapper = EmpiricalProbability.fit(train, ("mf_strength", "mf_persistence", "mf_return20", "price_to_ma20", "log_amount20", "log_price"))
    logit = score_frame(x, mapper, "logit_sum")
    spec = equivalent_spec(); execution = spec["execution"]
    base_config = dict(max_positions=execution["max_positions"], max_hold=execution["max_hold"], stop_pct=execution["stop_pct"], stop_basis=execution["stop_basis"], holding_clock=execution["holding_clock"], cost_one_way=execution["cost_one_way"])
    valid_days = calendar[(calendar >= pd.Timestamp(protocol["validation"][0])) & (calendar <= pd.Timestamp(protocol["validation"][1]))]
    eval_days = calendar[(calendar >= pd.Timestamp(protocol["evaluation"][0])) & (calendar <= pd.Timestamp(protocol["evaluation"][1]))]
    variants = [
        ("baseline", signals, {}),
        ("baseline_stop05", signals, {"stop_pct": .05}),
        ("baseline_stop06", signals, {"stop_pct": .06}),
        ("logit", logit, {}),
        ("logit_dd_soft08_hard15_floor25", logit, {"drawdown_soft": .08, "drawdown_hard": .15, "drawdown_floor": .25}),
        ("logit_dd_soft10_hard18_floor25", logit, {"drawdown_soft": .10, "drawdown_hard": .18, "drawdown_floor": .25}),
        ("logit_regime", regime_filter(logit, benchmark), {}),
        ("logit_vol70", volatility_filter(logit, bars, .70), {}),
        ("logit_vol80", volatility_filter(logit, bars, .80), {}),
        ("baseline_vol70", volatility_filter(signals, bars, .70), {}),
        ("logit_regime_dd", regime_filter(logit, benchmark), {"drawdown_soft": .08, "drawdown_hard": .15, "drawdown_floor": .25}),
    ]
    rows = []
    for name, frame, overlay in variants:
        cfg_kwargs = {**base_config, **overlay}
        cfg = ReplayConfig(**cfg_kwargs)
        vr = replay(frame[frame.trade_date.between(protocol["validation"][0], protocol["validation"][1])], bars[bars.trade_date.isin(valid_days)], benchmark, cfg, valid_days)
        er = replay(frame[frame.trade_date.between(protocol["evaluation"][0], protocol["evaluation"][1])], bars[bars.trade_date.isin(eval_days)], benchmark, cfg, eval_days)
        vm, em = metrics(vr["nav"]), metrics(er["nav"])
        base_vm = rows[0] if rows else None
        rows.append({"variant": name, "validation_cagr": vm["calendar_cagr"], "validation_drawdown": vm["max_drawdown"], "validation_sharpe": vm["sharpe"], "evaluation_cagr": em["calendar_cagr"], "evaluation_drawdown": em["max_drawdown"], "evaluation_sharpe": em["sharpe"], "evaluation_returns": len(eval_days)-1, "validation_cagr_gate": bool(base_vm is None or vm["calendar_cagr"] >= base_vm["validation_cagr"]), "drawdown_vs_baseline": em["max_drawdown"] - (base_vm["evaluation_drawdown"] if base_vm else em["max_drawdown"]), "status": "RETAIN_BASELINE"})
        pd.DataFrame({"nav": er["nav"], "benchmark_nav": er["benchmark_nav"]}).to_csv(output / f"{name}_evaluation_nav.csv")
    result = {"status": "RETAIN_BASELINE", "variants": rows, "holdout_not_used_for_selection": True, "production_changed": False, "created_at": datetime.now(timezone.utc).isoformat(), "remaining": [">=252 holdout returns", "PIT/publication-time checks", "capacity and forward shadow"]}
    (output / "manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    lines = ["# P2.71 回撤控制消融", "", "状态：**RETAIN_BASELINE**。所有风险覆盖均为研究版本，不接旧条件风控、不改生产策略。", "", "| 版本 | 验证CAGR | 验证回撤 | 留出CAGR | 留出回撤 | 留出Sharpe | 状态 |", "|---|---:|---:|---:|---:|---:|---|"]
    for r in rows:
        lines.append(f"| {r['variant']} | {r['validation_cagr']:+.4%} | {r['validation_drawdown']:+.4%} | {r['evaluation_cagr']:+.4%} | {r['evaluation_drawdown']:+.4%} | {r['evaluation_sharpe']:+.3f} | {r['status']} |")
    lines += ["", f"留出观测：{len(eval_days)-1}（少于252，任何版本均不晋级）。回撤节流仅缩小新开仓，不强平已有仓位；市场状态和波动过滤只使用信号日前可得数据。", "", "[manifest](manifest.json)"]
    (output / "REPORT.md").write_text("\n".join(lines) + "\n")
    print(f"RETAIN_BASELINE variants={len(rows)} holdout_returns={len(eval_days)-1}")
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--output", type=Path, default=ROOT / "output/research/p2_71/drawdown_overlay_v1"); run(ap.parse_args().output.resolve())
