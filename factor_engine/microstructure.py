"""Daily microstructure features from intraday OHLCV bars."""

from __future__ import annotations

import numpy as np
import pandas as pd


def build_intraday_microstructure_features(frame: pd.DataFrame, *, stock_code: str | None = None) -> pd.DataFrame:
    """Aggregate minute/intraday bars into daily execution-aware features."""
    if frame is None or frame.empty:
        return pd.DataFrame()
    working = frame.copy()
    if not isinstance(working.index, pd.DatetimeIndex):
        date_col = "date" if "date" in working.columns else "trade_date"
        working[date_col] = pd.to_datetime(working[date_col], errors="coerce")
        working.set_index(date_col, inplace=True)
    working.sort_index(inplace=True)
    for lower, title in [("open", "Open"), ("high", "High"), ("low", "Low"), ("close", "Close"), ("volume", "Volume")]:
        if title not in working.columns and lower in working.columns:
            working[title] = working[lower]
    rows = []
    for date, group in working.groupby(working.index.normalize()):
        group = group.dropna(subset=["Open", "High", "Low", "Close"], how="any")
        if group.empty:
            continue
        close = pd.to_numeric(group["Close"], errors="coerce")
        volume = pd.to_numeric(group.get("Volume", 0.0), errors="coerce").fillna(0.0)
        price = close.ffill()
        day_open = float(pd.to_numeric(group["Open"], errors="coerce").iloc[0])
        day_close = float(price.iloc[-1])
        total_volume = float(volume.sum())
        vwap = float((price * volume).sum() / total_volume) if total_volume > 0 else day_close
        first_n = max(1, min(30, len(group) // 4 or 1))
        last_n = max(1, min(30, len(group) // 4 or 1))
        open_30m_return = float(price.iloc[first_n - 1] / day_open - 1.0) if day_open > 0 else np.nan
        close_30m_return = float(day_close / price.iloc[-last_n] - 1.0) if price.iloc[-last_n] > 0 else np.nan
        returns = price.pct_change().fillna(0.0)
        signed_volume = np.sign(returns).replace(0, np.nan).ffill().fillna(0.0) * volume
        large_trade_threshold = volume.quantile(0.80) if len(volume) > 3 else volume.max()
        large_trade_imbalance = float(signed_volume[volume >= large_trade_threshold].sum() / max(volume[volume >= large_trade_threshold].sum(), 1.0))
        expected_u = np.linspace(1.25, 0.75, len(volume))
        expected_u = expected_u / expected_u.sum() * max(total_volume, 1.0)
        volume_u_shape_deviation = float(np.abs(volume.to_numpy(dtype=float) - expected_u).sum() / max(total_volume, 1.0))
        rows.append({
            "trade_date": pd.Timestamp(date),
            "stock_code": str(stock_code or group.get("stock_code", pd.Series([""])).iloc[0]).zfill(5),
            "open_30m_return": open_30m_return,
            "close_30m_return": close_30m_return,
            "intraday_vwap_gap": float(day_close / vwap - 1.0) if vwap > 0 else np.nan,
            "volume_u_shape_deviation": volume_u_shape_deviation,
            "large_trade_imbalance": large_trade_imbalance,
            "turnover_burst_score": float(volume.max() / max(volume.median(), 1.0)) if len(volume) else np.nan,
            "closing_auction_pressure": float(volume.tail(last_n).sum() / max(total_volume, 1.0)),
            "intraday_realized_vol": float(returns.std(ddof=0) * np.sqrt(max(len(returns), 1))),
            "intraday_bar_count": int(len(group)),
        })
    return pd.DataFrame(rows)


def microstructure_features_to_long(frame: pd.DataFrame, *, feature_set: str = "intraday_microstructure", feature_version: str = "v1") -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    id_cols = {"trade_date", "stock_code"}
    feature_cols = [col for col in frame.columns if col not in id_cols]
    long = frame.melt(id_vars=["trade_date", "stock_code"], value_vars=feature_cols, var_name="feature_name", value_name="feature_value")
    long["market"] = "HK"
    long["frequency"] = "daily"
    long["feature_set"] = feature_set
    long["feature_version"] = feature_version
    long["available_at"] = pd.to_datetime(long["trade_date"], errors="coerce")
    return long
