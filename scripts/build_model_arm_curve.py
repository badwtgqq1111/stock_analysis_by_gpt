#!/usr/bin/env python3
"""把 OOS 预测 → **模型臂收益曲线（含中证500 对照）**（P2.45-A/B 的尺子）。

输入：`output/oos_predictions*/cn_lightgbm_oos_predictions.csv`（walk-forward 样本外预测，列含
      `trade_date/stock_code/<score>`）。
做法：每个预测日取 **分数前 top_n 等权** → 持有到下一个预测日（次日开盘建仓、段内以建仓开盘价为基准、
      段间按段末净值链接）→ 与中证500同期归一对齐。曲线算法直接复用
      `scripts/build_strategy_pool_performance.py:ledger_curve`（同一份口径，避免两套净值算法）。
用法：uv run python scripts/build_model_arm_curve.py --predictions <csv> --out output/research/model_arm_ab/<arm>.json --top-n 20
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _perf_module():
    spec = importlib.util.spec_from_file_location("perf_under_test", ROOT / "scripts/build_strategy_pool_performance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_bars(codes: list[str]) -> pd.DataFrame:
    files = [str(p) for p in (ROOT / "assets/data/clean/ohlcv/market=CN").glob(
        "exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet")]
    table = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "open", "close"],
        filter=ds.field("stock_code").isin(codes)).to_pandas()
    table["trade_date"] = pd.to_datetime(table["trade_date"])
    return table.drop_duplicates(["trade_date", "stock_code"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--predictions", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--top-n", type=int, default=20)
    ap.add_argument("--score-column", default=None)
    ap.add_argument("--vol-target", type=float, default=None,
                    help="年化波动目标（如 0.18，与生产 [selection].risk_control.target_volatility 同值）："
                         "按 20 日已实现波动缩放仓位，超出部分留现金（不加杠杆）")
    args = ap.parse_args(argv)

    frame = pd.read_csv(args.predictions)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    score = args.score_column or next((c for c in ("score", "prediction", "lightgbm_score", "y_pred") if c in frame.columns), None)
    if score is None:
        score = next(c for c in frame.columns if c not in ("trade_date", "stock_code", "fold"))
    frame = frame.dropna(subset=[score])
    targets = {}
    for day, group in frame.groupby("trade_date"):
        picked = group.sort_values(score, ascending=False).head(args.top_n)
        weights = pd.Series(1.0 / len(picked), index=picked["stock_code"].astype(str).to_numpy())
        targets[day] = weights
    bench_raw = pd.read_parquet(ROOT / "assets/data/meta/cn_benchmark_daily.parquet")
    bench = bench_raw[bench_raw.ts_code == "000905.SH"].copy()
    bench["trade_date"] = pd.to_datetime(bench["trade_date"])
    bench = bench.set_index("trade_date").close.sort_index()
    bars = load_bars(sorted({c for weights in targets.values() for c in weights.index}))
    perf = _perf_module()
    curve = perf.ledger_curve(targets, bench, bars, kind="model_arm_oos_topn")

    # ── 成本：按**单边换手 × 双边费率**从毛曲线里扣（口径取 config/execution_assumptions_cn.json 的唯一来源）──
    # 为什么必须算：模型臂每 5 个交易日调仓一次，换手远高于"持有到下一个决策日"的候选池/影子臂；
    # 只报毛收益会**系统性高估**（第四十九轮那条台账口径曲线也是毛的，我们把它标清楚了）。
    assumptions = json.loads((ROOT / "config/execution_assumptions_cn.json").read_text(encoding="utf-8"))
    costs = assumptions["costs"]
    buy_bps = float(costs["commission_bps"]) + float(costs["slippage_bps"])
    sell_bps = buy_bps + float(costs["stamp_tax_bps_sell"])
    dates = pd.DatetimeIndex(curve.get("dates", []))
    ordered = sorted(targets.items())
    total_cost = 0.0
    turnovers = []
    for i in range(1, len(ordered)):
        prev, cur = ordered[i - 1][1], ordered[i][1]
        index = prev.index.union(cur.index)
        one_way = float((prev.reindex(index, fill_value=0) - cur.reindex(index, fill_value=0)).abs().sum()) / 2.0
        turnovers.append(one_way)
        total_cost += one_way * (buy_bps + sell_bps) / 1e4
    vol_target = args.vol_target
    if vol_target and curve.get("nav"):
        nav_series = pd.Series(curve["nav"], index=pd.DatetimeIndex(curve["dates"]))
        returns = nav_series.pct_change().fillna(0.0)
        realized = returns.rolling(20, min_periods=5).std() * (252 ** 0.5)
        scale = (float(vol_target) / realized).clip(upper=1.0).shift(1).fillna(1.0)
        scaled_returns = scale * returns
        scaled_nav = (1.0 + scaled_returns).cumprod()
        curve["vol_target"] = {"target": float(vol_target),
                               "mean_scale": round(float(scale.mean()), 4),
                               "realized_vol": round(float(returns.std() * (252 ** 0.5)), 4),
                               "scaled_vol": round(float(scaled_returns.std() * (252 ** 0.5)), 4),
                               "nav": [round(float(v), 6) for v in scaled_nav.to_numpy()],
                               "total_return": round(float(scaled_nav.iloc[-1] - 1), 4),
                               "max_drawdown": round(float((scaled_nav / scaled_nav.cummax() - 1).min()), 4)}
        scale_by_period = scale.resample("D").mean().dropna() if len(scale) else None
        cost_scale = float(scale.mean())
    else:
        cost_scale = 1.0
    if curve.get("nav") and turnovers:
        gross = float(curve["metrics"]["total_return"])
        net = (1.0 + gross) * (1.0 - total_cost) - 1.0
        curve["costs"] = {"buy_bps": buy_bps, "sell_bps": sell_bps,
                          "turnover_mean": round(sum(turnovers) / len(turnovers), 4),
                          "turnover_total": round(sum(turnovers), 4),
                          "cost_total": round(total_cost, 4),
                          "gross_total_return": round(gross, 4), "net_total_return": round(net, 4),
                          "net_excess_return": round(net - float(curve["benchmark_metrics"]["total_return"]), 4),
                          "source": "config/execution_assumptions_cn.json"}
    curve["arm"] = Path(args.predictions).parent.name
    curve["top_n"] = args.top_n
    curve["n_prediction_dates"] = len(targets)
    _ = dates
    m, b = curve.get("metrics", {}), curve.get("benchmark_metrics", {})
    cost = curve.get("costs", {})
    print(f"[model-arm] {curve['arm']}: 预测日 {len(targets)} ｜ 交易日 {len(curve.get('dates', []))} "
          f"｜ {curve.get('start_date')}~{curve.get('end_date')} ｜ 累计 {m.get('total_return')} "
          f"vs 中证500 {b.get('total_return')} ｜ 毛超额 {curve.get('excess_return')} ｜ 回撤 {m.get('max_drawdown')}")
    if cost:
        print(f"[model-arm] 成本（单边换手×双边费率，来源 config/execution_assumptions_cn.json）："
              f"平均单边换手 {cost['turnover_mean']:.2f} ｜ 累计成本 {cost['cost_total']*100:.2f}% "
              f"｜ **净收益 {cost['net_total_return']*100:.2f}% ｜ 净超额 {cost['net_excess_return']*100:.2f}%**")
    if curve.get("vol_target"):
        vt, cost = curve["vol_target"], curve.get("costs", {})
        # 波动目标把仓位缩到 scale ⇒ 换手成本也按同一比例缩（近似：用平均 scale）
        mean_scale = vt["mean_scale"]
        scaled_cost = cost.get("cost_total", 0.0) * mean_scale
        net_vol = (1.0 + vt["total_return"]) * (1.0 - scaled_cost) - 1.0
        bench = float(curve["benchmark_metrics"]["total_return"])
        curve["vol_target"].update(cost_scaled=round(scaled_cost, 4), net_total_return=round(net_vol, 4),
                                   net_excess_return=round(net_vol - bench, 4))
        print(f"[model-arm] 波动目标 {vt['target']:.2f}：平均仓位 {mean_scale:.2f} ｜ 实现波动 "
              f"{vt['realized_vol']*100:.1f}% → {vt['scaled_vol']*100:.1f}% ｜ 毛收益 {vt['total_return']*100:.2f}% "
              f"｜ 回撤 {vt['max_drawdown']*100:.1f}% ｜ **净收益 {net_vol*100:.2f}% ｜ 净超额 "
              f"{(net_vol - bench)*100:.2f}%**")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(curve, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[model-arm] 曲线 -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
