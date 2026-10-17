#!/usr/bin/env python3
"""Run the frozen non-LightGBM model matrix on the independent factor path."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
from backend.services.factor_dsl import panel
from backend.services.factor_moneyflow import build_signals, equivalent_spec
from backend.services.factor_model_matrix import fit_model, model_specs, score_model, validation_gate
from backend.services.factor_pool import FactorStore
from backend.services.factor_replay import ReplayConfig, metrics, replay
from p2_71_factor_audit import digest


def load_data(protocol):
    end = protocol["evaluation"][1]
    files = sorted(
        p for year in range(2024, pd.Timestamp(end).year + 1)
        for p in (ROOT / "assets/data/clean/ohlcv/market=CN").glob(
            f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={year}/*.parquet"
        )
    )
    bounds = (ds.field("trade_date") >= pd.Timestamp("2024-06-01").to_pydatetime()) & (ds.field("trade_date") <= pd.Timestamp(end).to_pydatetime())
    bars = ds.dataset([str(p) for p in files], format="parquet").to_table(
        columns=["stock_code", "trade_date", "open", "close", "amount"], filter=bounds
    ).to_pandas()
    basic_path = ROOT / "assets/data/meta/cn_stock_basic.parquet"
    universe = set(pd.read_parquet(basic_path, columns=["ts_code"]).ts_code.astype(str))
    bars = panel(bars[bars.stock_code.isin(universe)])
    flow_path = ROOT / "assets/data/derived/cn_moneyflow_features.parquet"
    flow = ds.dataset(str(flow_path), format="parquet").to_table(
        columns=["stock_code", "trade_date", "source", "moneyflow_net_z_5d", "moneyflow_positive_ratio_10d"], filter=bounds
    ).to_pandas()
    flow = flow[flow.stock_code.isin(universe)]
    output = ROOT / "output/research/p2_71/model_matrix_v1"
    store = FactorStore(output / "factors.sqlite3")
    signals = build_signals(bars, flow, store, universe)
    # Reuse the frozen six transforms from the LightGBM study without importing its model.
    from backend.services.factor_lightgbm import features, labels
    x = features(signals, bars)
    benchmark = pd.read_parquet(ROOT / "assets/data/meta/cn_benchmark_daily.parquet")
    benchmark.trade_date = pd.to_datetime(benchmark.trade_date)
    benchmark = benchmark[(benchmark.ts_code == "000905.SH") & benchmark.trade_date.between("2024-06-01", end)].set_index("trade_date").close.sort_index()
    calendar = pd.DatetimeIndex(benchmark.index)
    return bars, signals, x, benchmark, calendar, labels


def run(output: Path):
    protocol = json.loads((ROOT / "config/p2_71_lightgbm.json").read_text())
    output.mkdir(parents=True, exist_ok=True)
    bars, signals, x, benchmark, calendar, labels = load_data(protocol)
    train = labels(x, bars, calendar, *protocol["train"], boundary=protocol["validation"][0], horizon=protocol["horizon_sessions"])
    valid = labels(x, bars, calendar, *protocol["validation"], boundary=protocol["evaluation"][0], horizon=protocol["horizon_sessions"])
    # The realized replay and the learning label are intentionally measured
    # separately.  This exposes objective mismatch instead of hiding it in one
    # aggregate CAGR number.
    valid_key = valid[["stock_code", "trade_date", "label"]]
    eval_label = labels(x, bars, calendar, *protocol["evaluation"], boundary=pd.Timestamp("2100-01-01"), horizon=protocol["horizon_sessions"])
    eval_key = eval_label[["stock_code", "trade_date", "label"]]
    spec = equivalent_spec()
    execution = spec["execution"]
    config = ReplayConfig(**{k: execution[k] for k in ["max_positions", "max_hold", "stop_pct", "stop_basis", "holding_clock", "cost_one_way"]})
    base_days = calendar[(calendar >= pd.Timestamp(protocol["validation"][0])) & (calendar <= pd.Timestamp(protocol["validation"][1]))]
    test_days = calendar[(calendar >= pd.Timestamp(protocol["evaluation"][0])) & (calendar <= pd.Timestamp(protocol["evaluation"][1]))]

    def evaluate(frame, days, start, end):
        b = replay(signals[signals.trade_date.between(start, end)], bars[bars.trade_date.isin(days)], benchmark, config, days)
        return b, metrics(b["nav"])

    baseline_valid, baseline_valid_m = evaluate(x, base_days, protocol["validation"][0], protocol["validation"][1])
    baseline_test, baseline_test_m = evaluate(x, test_days, protocol["evaluation"][0], protocol["evaluation"][1])

    def trade_diagnostics(result):
        ledger = result["ledger"]
        sells = ledger[ledger.side == "SELL"] if not ledger.empty else ledger
        return {
            "buy_count": int((ledger.side == "BUY").sum()) if not ledger.empty else 0,
            "sell_count": int((ledger.side == "SELL").sum()) if not ledger.empty else 0,
            "stop_exit_count": int(sells.reason.astype(str).str.startswith("stop_").sum()) if not sells.empty else 0,
            "max_hold_exit_count": int(sells.reason.astype(str).str.startswith("max_hold_").sum()) if not sells.empty else 0,
            "mean_realized_return_after_cost": float(sells.return_after_cost.mean()) if not sells.empty else None,
            "win_rate_closed": float((sells.return_after_cost > 0).mean()) if not sells.empty else None,
            "mean_holding_sessions": float(sells.holding_sessions.mean()) if not sells.empty else None,
            "total_fees": float(ledger.fee.sum()) if not ledger.empty else 0.0,
        }

    def rank_ic(frame, labels_frame, start, end):
        z = frame.merge(labels_frame, on=["stock_code", "trade_date"], how="inner")
        z = z[z.trade_date.between(start, end)]
        values = []
        for _, day in z.groupby("trade_date"):
            if len(day) >= 2 and day.score.nunique() > 1 and day.label.nunique() > 1:
                values.append(day.score.rank().corr(day.label.rank()))
        return float(pd.Series(values).mean()) if values else None

    def top_overlap(frame, start, end, n=5):
        a = frame[frame.trade_date.between(start, end)]
        b = signals[signals.trade_date.between(start, end)]
        overlaps = []
        for day, group in a.groupby("trade_date"):
            left = set(group.nlargest(n, "score").stock_code)
            right = set(b[b.trade_date == day].nlargest(n, "score").stock_code)
            if left or right:
                overlaps.append(len(left & right) / max(1, len(left | right)))
        return float(pd.Series(overlaps).mean()) if overlaps else None

    baseline_valid_ic = rank_ic(signals, valid_key, protocol["validation"][0], protocol["validation"][1])
    baseline_eval_ic = rank_ic(signals, eval_key, protocol["evaluation"][0], protocol["evaluation"][1])
    rows = []
    selected = []
    for ms in model_specs():
        model = fit_model(ms, train)
        pred = score_model(model, ms, x)
        vv = replay(pred[pred.trade_date.between(protocol["validation"][0], protocol["validation"][1])], bars[bars.trade_date.isin(base_days)], benchmark, config, base_days)
        tt = replay(pred[pred.trade_date.between(protocol["evaluation"][0], protocol["evaluation"][1])], bars[bars.trade_date.isin(test_days)], benchmark, config, test_days)
        vm, tm = metrics(vv["nav"]), metrics(tt["nav"])
        v_ic = rank_ic(pred, valid_key, protocol["validation"][0], protocol["validation"][1])
        t_ic = rank_ic(pred, eval_key, protocol["evaluation"][0], protocol["evaluation"][1])
        vpass = validation_gate(vm, baseline_valid_m)
        tpass = tm["calendar_cagr"] >= baseline_test_m["calendar_cagr"] and abs(tm["max_drawdown"]) <= abs(baseline_test_m["max_drawdown"]) + 0.03
        status = "CANDIDATE_OBSERVED_NOT_PROMOTED" if vpass and tpass and len(test_days) - 1 >= 252 else "RETAIN_BASELINE"
        diag = trade_diagnostics(tt)
        rows.append({"model": ms.name, "validation_cagr": vm["calendar_cagr"], "evaluation_cagr": tm["calendar_cagr"], "validation_drawdown": vm["max_drawdown"], "evaluation_drawdown": tm["max_drawdown"], "validation_rank_ic": v_ic, "evaluation_rank_ic": t_ic, "baseline_validation_rank_ic": baseline_valid_ic, "baseline_evaluation_rank_ic": baseline_eval_ic, "evaluation_top5_jaccard": top_overlap(pred, protocol["evaluation"][0], protocol["evaluation"][1]), "evaluation_trades": int(len(tt["ledger"])), **diag, "validation_gate": vpass, "evaluation_cagr_gate": tpass, "evaluation_returns": len(test_days) - 1, "status": status})
        pd.DataFrame({"nav": tt["nav"], "benchmark_nav": tt["benchmark_nav"]}).to_csv(output / f"{ms.name}_evaluation_nav.csv")
        if vpass:
            selected.append(ms.name)
    result = {"status": "RETAIN_BASELINE", "models": rows, "validation_selected": selected, "baseline_validation": baseline_valid_m, "baseline_evaluation": baseline_test_m, "baseline_evaluation_trade_diagnostics": trade_diagnostics(baseline_test), "evaluation_kind": protocol["evaluation_kind"], "holdout_not_used_for_selection": True, "production_changed": False, "created_at": datetime.now(timezone.utc).isoformat(), "remaining": [">=252 holdout returns", "PIT/publication-time checks", "capacity and forward shadow"]}
    (output / "manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    lines = ["# P2.71 非LightGBM因子模型矩阵", "", "状态：**RETAIN_BASELINE**。所有模型均为独立研究诊断，未替换生产或等价因子基线。", "", "| 模型 | 验证CAGR | 留出CAGR | 留出回撤 | 留出RankIC | Top5重合 | 卖出胜率 | 止损卖出 | 状态 |", "|---|---:|---:|---:|---:|---:|---:|---:|---|"]
    for r in rows:
        wr = r['win_rate_closed'] if r['win_rate_closed'] is not None else float('nan')
        lines.append(f"| {r['model']} | {r['validation_cagr']:+.4%} | {r['evaluation_cagr']:+.4%} | {r['evaluation_drawdown']:+.4%} | {r['evaluation_rank_ic'] if r['evaluation_rank_ic'] is not None else float('nan'):+.4f} | {r['evaluation_top5_jaccard'] if r['evaluation_top5_jaccard'] is not None else float('nan'):+.4f} | {wr:+.2%} | {r['stop_exit_count']} | {r['status']} |")
    bd = result["baseline_evaluation_trade_diagnostics"]
    lines += ["", f"共同基线留出日历CAGR：{baseline_test_m['calendar_cagr']:+.4%}；留出RankIC：{baseline_eval_ic if baseline_eval_ic is not None else float('nan'):+.4f}；留出收益观测：{len(test_days)-1}（当前未达到252，任何模型均不能晋级）。", f"基线账本：买入{bd['buy_count']}笔、卖出{bd['sell_count']}笔、止损卖出{bd['stop_exit_count']}笔、平仓胜率{bd['win_rate_closed'] if bd['win_rate_closed'] is not None else float('nan'):+.2%}、费用{bd['total_fees']:.4f}。", "训练/验证/留出切分、因子、成本和公共账本与LightGBM实验一致；仅验证门禁可影响候选登记，留出只作冻结后的诊断。", "", "解释：机器学习分数优化的是冻结标签的截面预测，不等于止损、持有期、交易成本后的组合收益；RankIC、Top5重合、交易/退出构成用于定位目标函数错配、状态漂移和换手差异。", "", "[manifest](manifest.json)"]
    (output / "REPORT.md").write_text("\n".join(lines) + "\n")
    print(f"RETAIN_BASELINE models={len(rows)} selected_validation={','.join(selected) or 'none'} holdout_returns={len(test_days)-1}")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "output/research/p2_71/model_matrix_v1")
    run(parser.parse_args().output.resolve())
