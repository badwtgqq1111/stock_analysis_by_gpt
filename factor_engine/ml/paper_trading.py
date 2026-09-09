"""Deterministic paper-trading outcome evaluation for persisted selections."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd


def evaluate_selection_outcomes(
    selections: pd.DataFrame,
    ohlcv: pd.DataFrame,
    *,
    horizons=(1, 5, 20, 60),
    cost_bps: float = 10.0,
    benchmark: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Evaluate each persisted recommendation at future trading sessions.

    Only bars strictly after ``trade_date`` are used. Missing future bars remain
    ``pending`` instead of being imputed, making the result safe for daily
    incremental runs.
    """
    if selections is None or selections.empty:
        return pd.DataFrame()
    required = {"stock_code", "trade_date"}
    if required - set(selections.columns):
        raise ValueError("selection input requires stock_code and trade_date")
    bars = ohlcv.copy() if ohlcv is not None else pd.DataFrame()
    if bars.empty or not {"stock_code", "trade_date", "close"}.issubset(bars.columns):
        raise ValueError("ohlcv input requires stock_code, trade_date and close")
    bars["trade_date"] = pd.to_datetime(bars["trade_date"], errors="coerce")
    bars["close"] = pd.to_numeric(bars["close"], errors="coerce")
    bars = bars.dropna(subset=["stock_code", "trade_date", "close"])
    bars = bars[bars["close"] > 0].sort_values(["stock_code", "trade_date"])
    bars = bars.drop_duplicates(["stock_code", "trade_date"], keep="last")
    by_stock = {code: group.reset_index(drop=True) for code, group in bars.groupby("stock_code", sort=False)}
    benchmark_series = _prepare_benchmark(benchmark)
    selections = selections.copy()
    selections["trade_date"] = pd.to_datetime(selections["trade_date"], errors="coerce")
    rows = []
    fee = float(cost_bps) / 10000.0
    for _, signal in selections.dropna(subset=["trade_date"]).iterrows():
        code = signal["stock_code"]
        group = by_stock.get(code)
        if group is None:
            continue
        dates = group["trade_date"].to_numpy()
        indices = np.flatnonzero(dates == signal["trade_date"].to_datetime64())
        if len(indices) == 0:
            continue
        entry_index = int(indices[-1])
        entry = float(group.iloc[entry_index]["close"])
        for horizon in horizons:
            horizon = int(horizon)
            target_index = entry_index + horizon
            result = signal.to_dict()
            result.update({"horizon": horizon, "entry_price": entry, "cost_bps": float(cost_bps)})
            if target_index >= len(group):
                result.update({"status": "pending", "gross_return": np.nan, "net_return": np.nan,
                               "benchmark_return": np.nan, "excess_return": np.nan, "mae": np.nan, "mfe": np.nan})
            else:
                path = group.iloc[entry_index:target_index + 1]["close"].to_numpy(dtype=float)
                gross = float(path[-1] / entry - 1.0)
                result.update({"status": "matured", "exit_date": group.iloc[target_index]["trade_date"],
                               "exit_price": float(path[-1]), "gross_return": gross,
                               "net_return": gross - 2 * fee,
                               "mae": float(np.min(path / entry - 1.0)),
                               "mfe": float(np.max(path / entry - 1.0))})
                benchmark_return = _benchmark_return(benchmark_series, signal["trade_date"], group.iloc[target_index]["trade_date"])
                if pd.isna(benchmark_return):
                    # Explicitly label this as a market proxy when no benchmark
                    # series was supplied; do not silently call it an index return.
                    market_at_entry = bars[bars["trade_date"] == signal["trade_date"]][["stock_code", "close"]]
                    market_at_exit = bars[bars["trade_date"] == group.iloc[target_index]["trade_date"]][["stock_code", "close"]]
                    if not market_at_entry.empty and not market_at_exit.empty:
                        joined = market_at_entry.merge(market_at_exit, on="stock_code", suffixes=("_entry", "_exit"))
                        if not joined.empty:
                            benchmark_return = float((joined["close_exit"] / joined["close_entry"] - 1.0).median())
                result["benchmark_return"] = benchmark_return
                result["benchmark_source"] = "explicit" if not benchmark_series.empty and pd.notna(benchmark_return) else "cross_sectional_proxy"
                result["excess_return"] = gross - benchmark_return if pd.notna(benchmark_return) else np.nan
            rows.append(result)
    return pd.DataFrame(rows)


def _prepare_benchmark(benchmark: pd.DataFrame | None) -> pd.DataFrame:
    if benchmark is None or benchmark.empty:
        return pd.DataFrame(columns=["trade_date", "close"])
    frame = benchmark.copy()
    date_column = "trade_date" if "trade_date" in frame.columns else "date"
    close_column = "close" if "close" in frame.columns else "price"
    if date_column not in frame.columns or close_column not in frame.columns:
        return pd.DataFrame(columns=["trade_date", "close"])
    frame = frame.rename(columns={date_column: "trade_date", close_column: "close"})
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    return frame.dropna(subset=["trade_date", "close"]).sort_values("trade_date").drop_duplicates("trade_date", keep="last")[["trade_date", "close"]]


def _benchmark_return(benchmark: pd.DataFrame, entry_date, exit_date):
    if benchmark is None or benchmark.empty:
        return np.nan
    entry = benchmark.loc[benchmark["trade_date"] == pd.Timestamp(entry_date), "close"]
    exit = benchmark.loc[benchmark["trade_date"] == pd.Timestamp(exit_date), "close"]
    if entry.empty or exit.empty or float(entry.iloc[-1]) <= 0:
        return np.nan
    return float(exit.iloc[-1] / entry.iloc[-1] - 1.0)


def write_outcome_report(outcomes: pd.DataFrame, output_dir="output/paper_trading", prefix="cn_signal_outcomes") -> dict:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    csv_path = directory / f"{prefix}.csv"
    json_path = directory / f"{prefix}.json"
    md_path = directory / f"{prefix}.md"
    outcomes.to_csv(csv_path, index=False)
    matured = outcomes[outcomes.get("status", pd.Series(dtype=str)) == "matured"] if not outcomes.empty else outcomes
    summary = {"rows": int(len(outcomes)), "matured": int(len(matured)), "pending": int(len(outcomes) - len(matured))}
    if not matured.empty:
        summary["mean_net_return"] = float(pd.to_numeric(matured["net_return"], errors="coerce").mean())
        summary["mean_excess_return"] = float(pd.to_numeric(matured["excess_return"], errors="coerce").mean())
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md_path.write_text("# CN Paper Trading Outcomes\n\n" + "\n".join(f"- {k}: **{v}**" for k, v in summary.items()) + f"\n\nCSV: `{csv_path}`\nJSON: `{json_path}`\n", encoding="utf-8")
    return {"csv": str(csv_path), "json": str(json_path), "markdown": str(md_path), **summary}
