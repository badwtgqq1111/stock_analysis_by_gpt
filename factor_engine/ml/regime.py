"""Point-in-time market regime features and rule-based model routing."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd


def build_market_regime(
    ohlcv: pd.DataFrame,
    *,
    min_stocks: int = 20,
    trend_window: int = 60,
    breadth_window: int = 20,
    volatility_window: int = 20,
    hysteresis_days: int = 3,
    version: str = "regime.v1",
) -> pd.DataFrame:
    """Build daily bull/bear/sideways labels using only data available by that date.

    The cross-sectional median return acts as a broad-market proxy when an
    explicit benchmark series is unavailable. Labels are deliberately
    configuration-driven and are not treated as ground-truth future regimes.
    """
    required = {"stock_code", "trade_date", "close"}
    if ohlcv is None or ohlcv.empty:
        return pd.DataFrame()
    missing = required - set(ohlcv.columns)
    if missing:
        raise ValueError(f"market regime input missing columns: {','.join(sorted(missing))}")
    frame = ohlcv[["stock_code", "trade_date", "close"]].copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["stock_code", "trade_date", "close"])
    frame = frame[frame["close"] > 0].sort_values(["stock_code", "trade_date"])
    if frame.empty:
        return pd.DataFrame()
    frame = frame.drop_duplicates(["stock_code", "trade_date"], keep="last")
    grouped = frame.groupby("stock_code", sort=False)
    frame["return_1d"] = grouped["close"].pct_change()
    frame["return_20d"] = grouped["close"].pct_change(int(breadth_window))
    frame["return_60d"] = grouped["close"].pct_change(int(trend_window))
    frame["ma20"] = grouped["close"].transform(lambda value: value.rolling(int(breadth_window), min_periods=int(breadth_window)).mean())
    frame["above_ma20"] = frame["close"] > frame["ma20"]
    frame["volatility_20d"] = grouped["return_1d"].transform(lambda value: value.rolling(int(volatility_window), min_periods=int(volatility_window)).std() * np.sqrt(252.0))
    daily = frame.groupby("trade_date", sort=True).agg(
        stock_count=("stock_code", "nunique"),
        median_return_20d=("return_20d", "median"),
        median_return_60d=("return_60d", "median"),
        breadth_above_ma20=("above_ma20", "mean"),
        realized_volatility_20d=("volatility_20d", "median"),
    ).reset_index()
    daily["regime"] = "insufficient"
    metric_ready = daily[["median_return_60d", "breadth_above_ma20", "realized_volatility_20d"]].notna().all(axis=1)
    enough = (daily["stock_count"] >= int(min_stocks)) & metric_ready
    bull = enough & (daily["median_return_60d"] > 0.08) & (daily["breadth_above_ma20"] >= 0.55)
    bear = enough & ((daily["median_return_60d"] < -0.08) | (daily["breadth_above_ma20"] <= 0.35))
    daily.loc[enough, "regime"] = "sideways"
    daily.loc[bull, "regime"] = "bull"
    daily.loc[bear, "regime"] = "bear"
    daily["regime"] = _apply_hysteresis(daily["regime"], hysteresis_days=int(hysteresis_days))
    daily["regime_version"] = version
    daily["model_weight_lightgbm"] = daily["regime"].map({"bull": 0.35, "sideways": 0.55, "bear": 0.70}).fillna(1.0)
    daily["model_weight_transformer"] = daily["regime"].map({"bull": 0.40, "sideways": 0.25, "bear": 0.15}).fillna(0.0)
    daily["model_weight_cnn"] = daily["regime"].map({"bull": 0.25, "sideways": 0.20, "bear": 0.15}).fillna(0.0)
    daily["gross_exposure_budget"] = daily["regime"].map({"bull": 0.95, "sideways": 0.75, "bear": 0.35}).fillna(0.0)
    daily["max_weight_budget"] = daily["regime"].map({"bull": 0.10, "sideways": 0.08, "bear": 0.05}).fillna(0.0)
    daily["strategy_id"] = daily["regime"].map({"bull": "trend_following", "sideways": "quality_reversion", "bear": "defensive_quality"}).fillna("insufficient_data")
    return daily


def _apply_hysteresis(labels: pd.Series, *, hysteresis_days: int) -> pd.Series:
    if hysteresis_days <= 1:
        return labels
    result = labels.astype(str).tolist()
    current = "insufficient"
    candidate = None
    count = 0
    for index, label in enumerate(result):
        if label in {"insufficient", current}:
            if label == current:
                candidate, count = None, 0
            continue
        if label == candidate:
            count += 1
        else:
            candidate, count = label, 1
        if count >= hysteresis_days:
            current = candidate
            for prior in range(index - hysteresis_days + 1, index + 1):
                result[prior] = current
            candidate, count = None, 0
        elif current != "insufficient":
            result[index] = current
    return pd.Series(result, index=labels.index)


def write_market_regime_report(frame: pd.DataFrame, output_dir="output/regime", *, prefix="cn_market_regime") -> dict:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    csv_path = directory / f"{prefix}.csv"
    json_path = directory / f"{prefix}.json"
    md_path = directory / f"{prefix}.md"
    frame.to_csv(csv_path, index=False)
    summary = {
        "rows": int(len(frame)),
        "latest_trade_date": str(frame["trade_date"].max().date()) if not frame.empty else None,
        "regime_counts": frame["regime"].value_counts(dropna=False).to_dict() if not frame.empty else {},
        "regime_version": str(frame["regime_version"].iloc[-1]) if not frame.empty else None,
    }
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    lines = ["# CN Market Regime", "", f"- Rows: **{summary['rows']}**", f"- Latest: **{summary['latest_trade_date']}**", f"- Counts: `{summary['regime_counts']}`", "", f"CSV: `{csv_path}`", f"JSON: `{json_path}`"]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"csv": str(csv_path), "json": str(json_path), "markdown": str(md_path), **summary}
