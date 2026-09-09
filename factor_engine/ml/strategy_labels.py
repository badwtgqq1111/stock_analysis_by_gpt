"""Point-in-time daily-bar research labels for CN strategy families."""

from __future__ import annotations

import pandas as pd


def build_cn_strategy_labels(ohlcv: pd.DataFrame, *, limit_up_threshold=0.095) -> pd.DataFrame:
    """Produce conservative daily research labels, never assumed executable.

    Board signals use a configurable percentage proxy only. They carry
    ``execution_ready=False`` because daily bars cannot establish whether an
    order could enter a sealed limit-up board.
    """
    required = {"stock_code", "trade_date", "close"}
    if ohlcv is None or ohlcv.empty or required - set(ohlcv.columns):
        return pd.DataFrame()
    frame = ohlcv.copy(); frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["stock_code", "trade_date", "close"]).sort_values(["stock_code", "trade_date"])
    group = frame.groupby("stock_code", sort=False)
    frame["return_1d"] = group["close"].pct_change()
    frame["ma20"] = group["close"].transform(lambda value: value.rolling(20, min_periods=20).mean())
    frame["ma60"] = group["close"].transform(lambda value: value.rolling(60, min_periods=60).mean())
    frame["drawdown_60d"] = group["close"].transform(lambda value: value / value.rolling(60, min_periods=20).max() - 1.0)
    frame["bottom_rebound_candidate"] = (frame["drawdown_60d"] <= -0.20) & (frame["close"] > frame["ma20"]) & (frame["return_1d"] > 0)
    frame["trend_following_candidate"] = (frame["close"] > frame["ma20"]) & (frame["ma20"] > frame["ma60"])
    frame["limit_up_proxy"] = frame["return_1d"] >= float(limit_up_threshold)
    frame["board_count"] = group["limit_up_proxy"].transform(lambda value: value.astype(int).groupby((~value).cumsum()).cumsum())
    frame["first_board_candidate"] = frame["limit_up_proxy"] & (frame["board_count"] == 1)
    frame["second_board_candidate"] = frame["limit_up_proxy"] & (frame["board_count"] == 2)
    frame["execution_ready"] = False
    return frame[["stock_code", "trade_date", "bottom_rebound_candidate", "trend_following_candidate", "first_board_candidate", "second_board_candidate", "limit_up_proxy", "board_count", "execution_ready"]]
