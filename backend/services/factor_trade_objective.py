"""Labels aligned with the independent P2.71 execution contract.

Unlike the first pilot's simple forward-open label, this label follows the
same entry, stop and max-hold clock as the research replay.  It is still a
research label: the public replay remains the authority for portfolio NAV.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def trade_labels(frame: pd.DataFrame, bars: pd.DataFrame, calendar, *, start, end,
                 boundary, horizon: int = 10, stop_pct: float = 0.08,
                 cost_one_way: float = 0.00155) -> pd.DataFrame:
    required = {"stock_code", "trade_date", "rl"}
    if not required <= set(frame.columns):
        raise ValueError(f"信号缺列: {sorted(required-set(frame.columns))}")
    required_bars = {"stock_code", "trade_date", "open", "close"}
    if not required_bars <= set(bars.columns):
        raise ValueError(f"行情缺列: {sorted(required_bars-set(bars.columns))}")
    dates = pd.DatetimeIndex(pd.to_datetime(calendar))
    if dates.has_duplicates or not dates.is_monotonic_increasing:
        raise ValueError("交易日历必须唯一且递增")
    if horizon <= 0 or not 0 <= stop_pct < 1 or not 0 <= cost_one_way < 1:
        raise ValueError("交易标签参数非法")
    prices = bars.set_index(["stock_code", "trade_date"])[["open", "close"]]
    if prices.index.has_duplicates:
        raise ValueError("行情键重复")
    out = frame[frame.trade_date.between(start, end)].copy()
    positions = dates.get_indexer(pd.DatetimeIndex(out.trade_date))
    if (positions < 0).any():
        raise ValueError("信号日期不在交易日历中")
    labels = []
    keep = []
    for row_idx, (idx, row) in enumerate(out.iterrows()):
        pos = int(positions[row_idx])
        entry_pos = pos + 1
        final_close_pos = pos + horizon
        exit_pos = None
        if entry_pos >= len(dates) or final_close_pos >= len(dates):
            continue
        entry_key = (row.stock_code, dates[entry_pos])
        entry = prices.open.get(entry_key, np.nan)
        if not np.isfinite(entry) or entry <= 0:
            continue
        stop_price = float(row.rl) * (1.0 - stop_pct)
        for close_pos in range(entry_pos, final_close_pos + 1):
            close = prices.close.get((row.stock_code, dates[close_pos]), np.nan)
            if not np.isfinite(close) or close <= 0:
                continue
            if close < stop_price or close_pos == final_close_pos:
                exit_pos = close_pos + 1
                break
        if exit_pos is None or exit_pos >= len(dates):
            continue
        exit_px = prices.open.get((row.stock_code, dates[exit_pos]), np.nan)
        if not np.isfinite(exit_px) or exit_px <= 0:
            continue
        net = (float(exit_px) / float(entry)) * ((1.0 - cost_one_way) / (1.0 + cost_one_way)) - 1.0
        if not np.isfinite(net):
            continue
        keep.append(idx)
        labels.append((dates[entry_pos], dates[exit_pos], net, "stop" if close_pos < final_close_pos else "max_hold"))
    result = out.loc[keep].copy()
    if labels:
        result[["label_start", "label_end", "label", "exit_reason"]] = pd.DataFrame(labels, index=result.index)
    else:
        result["label_start"] = pd.NaT
        result["label_end"] = pd.NaT
        result["label"] = np.nan
        result["exit_reason"] = pd.Series(dtype="object")
    result = result[result.label_end < pd.Timestamp(boundary)].copy()
    return result.reset_index(drop=True)

