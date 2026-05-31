#!/usr/bin/env python
"""Single-factor IC analysis for the 6 HK manual factors.

Computes cross-sectional IC (Pearson) and Rank IC (Spearman) between
each factor and forward returns at multiple horizons (5d/10d/20d/60d).

Usage:
    uv run python experiments/factor_ic_analysis.py [--stocks N] [--days D]
"""

from __future__ import annotations

import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

sys.path.insert(0, "/Users/ccs/code/quant/stock_analysis_by_gpt")

CUSTOM_FACTORS = [
    "price_position_52w_high",
    "pb_ratio_sector_relative",
    "sector_rps_reversal_20d",
    "volume_price_divergence_10d",
    "consecutive_up_days_5d",
]

HORIZONS = [5, 10, 20, 60]


def compute_factors_for_stock(ohlcv_frame: pd.DataFrame, stock_code: str):
    """Compute the 6 custom factors + forward returns for one stock.

    Args:
        ohlcv_frame: DataFrame with columns Open/High/Low/Close/Volume, index=date.
        stock_code: Stock identifier.

    Returns:
        DataFrame with date index, factor columns, stock_code, and fwd_ret_{H}d.
    """
    from factor_engine import create_factor_set

    if ohlcv_frame is None or ohlcv_frame.empty or len(ohlcv_frame) < 60:
        return None

    ohlcv = ohlcv_frame.rename(
        columns={
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        }
    )
    if "vwap" not in ohlcv.columns:
        ohlcv["vwap"] = (
            ohlcv["open"] + ohlcv["high"] + ohlcv["low"] + ohlcv["close"]
        ) / 4.0

    try:
        fs = create_factor_set("alpha158_hk")
        factor_df = fs.transform(ohlcv)
    except Exception:
        return None

    if factor_df.empty:
        return None

    available = [c for c in CUSTOM_FACTORS if c in factor_df.columns]
    if not available:
        return None

    result = factor_df[available].copy()
    result.index = ohlcv.index
    result["stock_code"] = stock_code

    close = ohlcv["close"]
    for h in HORIZONS:
        future = close.shift(-h)
        result[f"fwd_ret_{h}d"] = future / close - 1.0

    return result


def compute_cross_sectional_ic(panel: pd.DataFrame, factor_col: str, horizon: int):
    """Compute cross-sectional IC for one factor at one horizon.

    For each date where >=10 stocks have data, compute corr(factor, fwd_ret).
    """
    ret_col = f"fwd_ret_{horizon}d"
    dates = []
    ics = []
    rank_ics = []

    for date, group in panel.groupby(level=0):
        valid = group[[factor_col, ret_col]].dropna()
        if len(valid) < 10:
            continue
        f = valid[factor_col]
        r = valid[ret_col]
        if f.std() < 1e-12 or r.std() < 1e-12:
            continue
        dates.append(date)
        ics.append(f.corr(r))
        rank_ics.append(f.rank().corr(r.rank()))

    if not ics:
        return None

    ic_series = pd.Series(ics)
    rank_ic_series = pd.Series(rank_ics)

    return {
        "factor": factor_col,
        "horizon": horizon,
        "n_dates": len(dates),
        "ic_mean": float(ic_series.mean()),
        "ic_std": float(ic_series.std()),
        "icir": float(ic_series.mean() / ic_series.std()) if ic_series.std() > 0 else 0.0,
        "ic_positive_rate": float((ic_series > 0).mean()),
        "rank_ic_mean": float(rank_ic_series.mean()),
        "rank_ic_std": float(rank_ic_series.std()),
        "rank_icir": float(rank_ic_series.mean() / rank_ic_series.std()) if rank_ic_series.std() > 0 else 0.0,
        "rank_ic_positive_rate": float((rank_ic_series > 0).mean()),
    }


def main(stocks: int = 100, days: int = 365):
    from core import StockAnalyzer

    print(f"单因子 IC 分析 — {len(CUSTOM_FACTORS)} 个因子, {stocks} 只股票, {days} 天")
    print("=" * 70)

    analyzer = StockAnalyzer()
    all_stocks = analyzer.get_all_stocks()
    stock_codes = all_stocks[:stocks]
    print(f"股票池: {len(stock_codes)} 只 (前 {stocks} / {len(all_stocks)})")

    # Phase 1: load data in batch, then compute factors in parallel
    print("\n[1/2] 加载数据 + 计算因子值...")
    t0 = time.time()

    warmup_days = max(days + 180, days)
    batch_data_map = analyzer.load_stock_data_batch(stock_codes, warmup_days)
    print(f"  数据加载: {len(batch_data_map)}/{len(stock_codes)} 只股票有数据, {time.time() - t0:.1f}s")

    all_frames: list[pd.DataFrame] = []
    completed = 0
    total = len(batch_data_map)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(compute_factors_for_stock, ohlcv, code): code
            for code, ohlcv in batch_data_map.items()
        }
        for future in as_completed(futures):
            code = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                print(f"  {code} ERROR: {exc}")
                result = None
            completed += 1
            if result is not None:
                all_frames.append(result)
            if completed % 50 == 0:
                print(f"  {completed}/{total} ...")

    elapsed = time.time() - t0
    print(f"  完成: {len(all_frames)}/{total} 只股票, 耗时 {elapsed:.1f}s")

    if not all_frames:
        print("ERROR: 无有效数据")
        return

    # Build panel: MultiIndex (date, stock_code)
    panel = pd.concat(all_frames)
    panel = panel.set_index("stock_code", append=True).swaplevel(0, 1)
    n_dates = panel.index.get_level_values(0).nunique()
    print(f"面板: {len(panel)} 行, {n_dates} 个交易日, {panel.index.get_level_values(1).nunique()} 只股票")

    # Phase 2: cross-sectional IC
    print("\n[2/2] 计算截面 IC...")
    results = []
    for factor in CUSTOM_FACTORS:
        for horizon in HORIZONS:
            r = compute_cross_sectional_ic(panel, factor, horizon)
            if r:
                results.append(r)

    # Print detailed table
    print(f"\n{'=' * 95}")
    print(f"{'Factor':<35} {'H':>4} {'IC':>8} {'ICIR':>7} {'IC>0':>7} {'RankIC':>8} {'RkICIR':>7} {'N':>5}")
    print("-" * 95)

    for r in sorted(results, key=lambda x: abs(x["ic_mean"]), reverse=True):
        print(
            f"{r['factor']:<35} {r['horizon']:>4}d "
            f"{r['ic_mean']:>8.4f} {r['icir']:>7.3f} {r['ic_positive_rate']:>6.1%} "
            f"{r['rank_ic_mean']:>8.4f} {r['rank_icir']:>7.3f} {r['n_dates']:>5}"
        )

    # Summary: best horizon per factor
    print(f"\n{'=' * 95}")
    print("各因子最优 horizon (按 |IC|):")
    print("-" * 95)
    best = {}
    for r in results:
        key = r["factor"]
        if key not in best or abs(r["ic_mean"]) > abs(best[key]["ic_mean"]):
            best[key] = r
    for f, r in sorted(best.items(), key=lambda x: abs(x[1]["ic_mean"]), reverse=True):
        ic_abs = abs(r["ic_mean"])
        signal = "✓" if ic_abs > 0.02 else "△" if ic_abs > 0.01 else "✗"
        direction = "多" if r["ic_mean"] > 0 else "空"
        print(
            f"  {signal} {f:<35} {r['horizon']:>2}d  "
            f"IC={r['ic_mean']:+.4f}  IR={r['icir']:+.3f}  "
            f"方向={direction}  RankIC>0={r['rank_ic_positive_rate']:.1%}"
        )

    print()
    print("图例: ✓ = |IC|>0.02 显著, △ = |IC|>0.01 边缘, ✗ = 不显著")
    print()
    print("需要修正的因子: 如果 IC 方向与预期相反, 或 |IC|<0.01, 应考虑移除或反转符号")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Single-factor IC analysis")
    p.add_argument("--stocks", type=int, default=100, help="股票数量 (default: 100)")
    p.add_argument("--days", type=int, default=365, help="回溯天数 (default: 365)")
    args = p.parse_args()
    main(stocks=args.stocks, days=args.days)
