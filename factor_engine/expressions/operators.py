#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Qlib 风格表达式算子。"""

import numpy as np
import pandas as pd


EPSILON = 1e-12


def safe_divide(left, right, epsilon=EPSILON):
    """安全除法，避免零除。"""
    return left / (right + epsilon)


def ref(series, periods):
    """滞后值。"""
    return series.shift(periods)


def delta(series, periods=1):
    """差分。"""
    return series - ref(series, periods)


def ts_sum(series, window):
    """滚动求和。"""
    return series.rolling(window=window, min_periods=window).sum()


def ts_mean(series, window):
    """滚动均值。"""
    return series.rolling(window=window, min_periods=window).mean()


def ts_std(series, window):
    """滚动标准差。"""
    return series.rolling(window=window, min_periods=window).std()


def ts_max(series, window):
    """滚动最大值。"""
    return series.rolling(window=window, min_periods=window).max()


def ts_min(series, window):
    """滚动最小值。"""
    return series.rolling(window=window, min_periods=window).min()


def ts_quantile(series, window, quantile):
    """滚动分位数。"""
    return series.rolling(window=window, min_periods=window).quantile(quantile)


def _sliding_win(series, window):
    """Return (win, idx, n) — win is the sliding_window_view or None if too short."""
    arr = series.values.astype(np.float64)
    idx = series.index
    n = len(arr)
    if n < window:
        return None, idx, n
    return np.lib.stride_tricks.sliding_window_view(arr, window), idx, n


def _pad_result(values, window, idx):
    """Prepend NaN padding for the first window-1 positions."""
    pad = np.full(window - 1, np.nan)
    return pd.Series(np.concatenate([pad, values]), index=idx)


def ts_rank(series, window):
    """当前值在窗口内的分位排名（向量化）。"""
    win, idx, n = _sliding_win(series, window)
    if win is None:
        return pd.Series(np.full(n, np.nan), index=idx)
    has_nan = np.any(np.isnan(win), axis=1)
    last = win[:, -1:]
    lt = (win < last).sum(axis=1)
    eq = (win == last).sum(axis=1)
    rank_avg = (lt.astype(np.float64) + eq.astype(np.float64) / 2.0) / float(window)
    rank_avg[has_nan] = np.nan
    return _pad_result(rank_avg, window, idx)


def ts_idxmax(series, window):
    """窗口内最大值位置，返回 1..window（向量化）。"""
    win, idx, n = _sliding_win(series, window)
    if win is None:
        return pd.Series(np.full(n, np.nan), index=idx)
    has_nan = np.any(np.isnan(win), axis=1)
    result = win.argmax(axis=1).astype(np.float64) + 1.0
    result[has_nan] = np.nan
    return _pad_result(result, window, idx)


def ts_idxmin(series, window):
    """窗口内最小值位置，返回 1..window（向量化）。"""
    win, idx, n = _sliding_win(series, window)
    if win is None:
        return pd.Series(np.full(n, np.nan), index=idx)
    has_nan = np.any(np.isnan(win), axis=1)
    result = win.argmin(axis=1).astype(np.float64) + 1.0
    result[has_nan] = np.nan
    return _pad_result(result, window, idx)


def _rolling_ols_coefficients(values, window):
    """Vectorized rolling OLS: slope, rsquare, residual for a single window.

    Fits y = slope * x + intercept where x = [0, 1, ..., window-1] at each
    rolling position.  Returns three Series aligned with the input index.
    """
    n = len(values)
    if n < window:
        pad = np.full(n, np.nan)
        idx = values.index if isinstance(values, pd.Series) else pd.RangeIndex(n)
        return (
            pd.Series(pad, index=idx),
            pd.Series(pad, index=idx),
            pd.Series(pad, index=idx),
        )

    arr = values.values.astype(np.float64) if isinstance(values, pd.Series) else np.asarray(values, dtype=np.float64)
    idx = values.index if isinstance(values, pd.Series) else pd.RangeIndex(n)
    w = window
    nw = n - w + 1

    win = np.lib.stride_tricks.sliding_window_view(arr, w)

    S_x = w * (w - 1) / 2.0
    S_xx = w * (w - 1) * (2 * w - 1) / 6.0
    denom = w * S_xx - S_x * S_x

    S_y = win.sum(axis=1)
    x = np.arange(w, dtype=np.float64)
    S_xy = win @ x

    slope = np.full(nw, np.nan)
    if denom > EPSILON:
        slope = (w * S_xy - S_x * S_y) / denom

    mean_y = S_y / w
    S_y2 = (win * win).sum(axis=1)
    var_y = S_y2 / w - mean_y * mean_y
    var_x = (w * w - 1) / 12.0

    rsquare = np.full(nw, np.nan)
    ok = var_y > EPSILON
    rsquare[ok] = (slope[ok] * slope[ok]) * var_x / var_y[ok]
    rsquare = np.clip(rsquare, 0.0, 1.0)

    intercept = mean_y - slope * (w - 1) / 2.0
    y_pred_last = slope * (w - 1) + intercept
    resi = arr[w - 1:] - y_pred_last

    pad = np.full(w - 1, np.nan)
    return (
        pd.Series(np.concatenate([pad, slope]), index=idx, name=idx.name),
        pd.Series(np.concatenate([pad, rsquare]), index=idx, name=idx.name),
        pd.Series(np.concatenate([pad, resi]), index=idx, name=idx.name),
    )


def ts_slope(series, window):
    """线性回归斜率。"""
    s, _, _ = _rolling_ols_coefficients(series, window)
    return s


def ts_rsquare(series, window):
    """线性回归 R^2。"""
    _, r, _ = _rolling_ols_coefficients(series, window)
    return r


def ts_resi(series, window):
    """线性回归末点残差。"""
    _, _, e = _rolling_ols_coefficients(series, window)
    return e


def corr(left, right, window):
    """滚动相关系数。"""
    return left.rolling(window=window, min_periods=window).corr(right)


def log(series):
    """安全对数。"""
    return np.log(series.clip(lower=EPSILON))


def greater(left, right):
    """逐点较大值。"""
    return np.maximum(left, right)


def less(left, right):
    """逐点较小值。"""
    return np.minimum(left, right)
