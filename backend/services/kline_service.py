import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.data_service import (
    get_all_stock_codes, load_stock_ohlcv, load_selected_data,
    load_stock_signals, get_stock_names_batch,
)


def _enrich_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["change_pct"] = df["Close"].pct_change() * 100
    df["MA5"] = df["Close"].rolling(5).mean()
    df["MA10"] = df["Close"].rolling(10).mean()
    df["MA20"] = df["Close"].rolling(20).mean()
    df["MA60"] = df["Close"].rolling(60).mean()
    df["VOL_MA5"] = df["Volume"].rolling(5).mean()
    df["VOL_MA20"] = df["Volume"].rolling(20).mean()
    df["amplitude"] = (df["High"] - df["Low"]) / df["Close"].shift(1) * 100
    return df


def _compute_lgbm_signals(df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
    df = df.copy()
    df["buy_signal"] = False
    df["sell_signal"] = False

    signals_df = load_stock_signals(stock_code)
    if signals_df is not None and not signals_df.empty and "date" in signals_df.columns:
        signals_df["date"] = pd.to_datetime(signals_df["date"])

        if "actionable" in signals_df.columns:
            buy_dates = set(signals_df[signals_df["actionable"] == True]["date"].dt.date)  # noqa: E712
            for idx in df.index:
                if idx.date() in buy_dates:
                    df.loc[idx, "buy_signal"] = True

        if "actionable" in signals_df.columns:
            signals_sorted = signals_df.sort_values("date")
            signals_sorted["prev_actionable"] = signals_sorted["actionable"].shift(1)
            sell_dates = set(
                signals_sorted[
                    (signals_sorted["prev_actionable"] == True) &  # noqa: E712
                    (signals_sorted["actionable"] == False)  # noqa: E712
                ]["date"].dt.date
            )
            for idx in df.index:
                if idx.date() in sell_dates:
                    df.loc[idx, "sell_signal"] = True

        return df

    if len(df) < 60:
        return df

    ma20 = df["Close"].rolling(20).mean()
    ma60 = df["Close"].rolling(60).mean()
    vol_ma20 = df["Volume"].rolling(20).mean()

    price_above_ma20 = df["Close"] > ma20
    cross_up = price_above_ma20 & (~price_above_ma20).shift(1).fillna(False)
    cross_down = (~price_above_ma20) & price_above_ma20.shift(1).fillna(False)
    vol_expand = df["Volume"] > vol_ma20 * 1.3

    df["buy_signal"] = cross_up & (ma20 > ma60) & vol_expand
    df["sell_signal"] = cross_down & ((ma20 < ma60) | (df["Volume"] < vol_ma20 * 0.7))

    return df


def _compute_chip_distribution(df: pd.DataFrame, n_bins: int = 50) -> dict:
    if df is None or len(df) < 20:
        return {"prices": [], "volumes": [], "current_price": 0}

    recent = df.tail(120).copy()
    current_price = float(recent["Close"].iloc[-1])

    price_min = recent["Low"].min() * 0.95
    price_max = recent["High"].max() * 1.05
    bins = np.linspace(price_min, price_max, n_bins + 1)
    bin_centers = (bins[:-1] + bins[1:]) / 2

    n = len(recent)
    decay_weights = np.exp(-0.02 * np.arange(n)[::-1])

    chip_volume = np.zeros(n_bins)
    for i in range(n):
        row = recent.iloc[i]
        low, high, vol = row["Low"], row["High"], row["Volume"]
        if high <= low or vol <= 0:
            continue
        mask = (bin_centers >= low) & (bin_centers <= high)
        n_covered = mask.sum()
        if n_covered > 0:
            chip_volume[mask] += vol * decay_weights[i] / n_covered

    total = chip_volume.sum()
    if total > 0:
        chip_volume = chip_volume / total * 100

    return {
        "prices": bin_centers.tolist(),
        "volumes": chip_volume.tolist(),
        "current_price": current_price,
    }


def _build_signal_stats(df, buy_points, sell_points) -> dict:
    trades = []
    buy_dates = buy_points.index.tolist()
    sell_dates = sell_points.index.tolist()
    si = 0
    for bd in buy_dates:
        while si < len(sell_dates) and sell_dates[si] <= bd:
            si += 1
        if si < len(sell_dates):
            buy_price = df.loc[bd, "Close"]
            sell_price = df.loc[sell_dates[si], "Close"]
            ret = (sell_price - buy_price) / buy_price * 100
            trades.append(ret)
            si += 1

    if trades:
        return {
            "buy_count": len(buy_points),
            "sell_count": len(sell_points),
            "paired_trades": len(trades),
            "avg_return": round(float(np.mean(trades)), 2),
            "win_rate": round(float(np.mean([t > 0 for t in trades]) * 100), 1),
            "max_win": round(float(max(trades)), 1),
            "max_loss": round(float(min(trades)), 1),
        }
    return {
        "buy_count": len(buy_points),
        "sell_count": len(sell_points),
        "paired_trades": 0,
    }


def _df_to_ohlcv_list(df: pd.DataFrame) -> list[dict]:
    records = []
    for idx in df.index:
        row = df.iloc[df.index.get_loc(idx)]
        records.append({
            "time": idx.strftime("%Y-%m-%d"),
            "open": _safe_float(row["Open"]),
            "high": _safe_float(row["High"]),
            "low": _safe_float(row["Low"]),
            "close": _safe_float(row["Close"]),
            "volume": _safe_float(row["Volume"]),
            "ma5": _safe_float_or_none(row.get("MA5")),
            "ma10": _safe_float_or_none(row.get("MA10")),
            "ma20": _safe_float_or_none(row.get("MA20")),
            "ma60": _safe_float_or_none(row.get("MA60")),
            "vol_ma5": _safe_float_or_none(row.get("VOL_MA5")),
            "vol_ma20": _safe_float_or_none(row.get("VOL_MA20")),
            "change_pct": _safe_float_or_none(row.get("change_pct")),
            "amplitude": _safe_float_or_none(row.get("amplitude")),
            "buy_signal": bool(row.get("buy_signal", False)),
            "sell_signal": bool(row.get("sell_signal", False)),
        })
    return records


def _safe_float(val) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _safe_float_or_none(val) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        if np.isnan(f) or np.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


class KlineService:
    def get_stocks(self) -> list[dict]:
        codes = get_all_stock_codes()
        if not codes:
            codes = ["00700", "09988", "01810", "02318", "00388"]

        selected_df = load_selected_data()
        selected_codes = set()
        if selected_df is not None and "stock_code" in selected_df.columns:
            selected_codes = {str(c).zfill(5) for c in selected_df["stock_code"].tolist()}

        all_codes = list(selected_codes) + [c for c in codes if c not in selected_codes]
        names_map = get_stock_names_batch(all_codes[:200])

        stocks = []
        for code in all_codes:
            stocks.append({
                "code": code,
                "name": names_map.get(code, ""),
                "is_selected": code in selected_codes,
            })
        return stocks

    def get_ohlcv(
        self, code: str, days: int = 365,
        with_signals: bool = True, with_chips: bool = False,
    ) -> dict:
        df = load_stock_ohlcv(code, days=days)
        if df is None or df.empty:
            return {
                "code": code, "name": "", "data": [],
                "signal_stats": None, "chips": None,
                "latest": {"close": 0, "change_pct": 0, "total_bars": 0},
            }

        df = _enrich_dataframe(df)
        if with_signals:
            df = _compute_lgbm_signals(df, code)

        signal_stats = None
        if with_signals:
            buy_points = df[df["buy_signal"] == True]  # noqa: E712
            sell_points = df[df["sell_signal"] == True]  # noqa: E712
            signal_stats = _build_signal_stats(df, buy_points, sell_points)

        chips = None
        if with_chips:
            chips = _compute_chip_distribution(df)

        from backend.services.data_service import get_stock_name
        name = get_stock_name(code) or ""

        latest_close = float(df["Close"].iloc[-1])
        latest_chg = float(df["change_pct"].iloc[-1]) if "change_pct" in df.columns else 0
        if pd.isna(latest_chg):
            latest_chg = 0

        return {
            "code": code,
            "name": name,
            "data": _df_to_ohlcv_list(df),
            "signal_stats": signal_stats,
            "chips": chips,
            "latest": {
                "close": round(latest_close, 3),
                "change_pct": round(latest_chg, 2),
                "total_bars": len(df),
            },
        }
