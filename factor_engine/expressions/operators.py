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


def _rolling_last_rank(values):
    if np.isnan(values).any():
        return np.nan
    return pd.Series(values).rank(method="average", pct=True).iloc[-1]


def ts_rank(series, window):
    """当前值在窗口内的分位排名。"""
    return series.rolling(window=window, min_periods=window).apply(_rolling_last_rank, raw=True)


def _rolling_idxmax(values):
    if np.isnan(values).any():
        return np.nan
    return float(np.argmax(values) + 1)


def _rolling_idxmin(values):
    if np.isnan(values).any():
        return np.nan
    return float(np.argmin(values) + 1)


def ts_idxmax(series, window):
    """窗口内最大值位置，返回 1..window。"""
    return series.rolling(window=window, min_periods=window).apply(_rolling_idxmax, raw=True)


def ts_idxmin(series, window):
    """窗口内最小值位置，返回 1..window。"""
    return series.rolling(window=window, min_periods=window).apply(_rolling_idxmin, raw=True)


def _linear_fit(values):
    if np.isnan(values).any():
        return None
    x = np.arange(len(values), dtype=float)
    slope, intercept = np.polyfit(x, values, 1)
    fitted = slope * x + intercept
    return slope, intercept, fitted


def _rolling_slope(values):
    fitted = _linear_fit(values)
    return np.nan if fitted is None else float(fitted[0])


def _rolling_rsquare(values):
    fitted = _linear_fit(values)
    if fitted is None:
        return np.nan
    _, _, predicted = fitted
    mean_value = np.mean(values)
    ss_tot = np.sum((values - mean_value) ** 2)
    if abs(ss_tot) <= EPSILON:
        return np.nan
    ss_res = np.sum((values - predicted) ** 2)
    return float(1 - ss_res / ss_tot)


def _rolling_resi(values):
    fitted = _linear_fit(values)
    if fitted is None:
        return np.nan
    _, _, predicted = fitted
    return float(values[-1] - predicted[-1])


def ts_slope(series, window):
    """线性回归斜率。"""
    return series.rolling(window=window, min_periods=window).apply(_rolling_slope, raw=True)


def ts_rsquare(series, window):
    """线性回归 R^2。"""
    return series.rolling(window=window, min_periods=window).apply(_rolling_rsquare, raw=True)


def ts_resi(series, window):
    """线性回归末点残差。"""
    return series.rolling(window=window, min_periods=window).apply(_rolling_resi, raw=True)


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
