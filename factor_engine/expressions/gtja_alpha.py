#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""GTJA Alpha191 style factor set for the single-stock materialization engine.

The original GTJA formulas use cross-sectional RANK in several places.  This
project materializes factors one stock at a time, so RANK is represented by a
rolling time-series percentile proxy here.  The interface is stable:
GTJA001..GTJA191 are always emitted and can be replaced by stricter formulas
without changing downstream LightGBM or feature-store code.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from factor_engine.base import BaseFactorSet, FactorSetMetadata
from factor_engine.expressions.operators import (
    corr,
    delta,
    log,
    ref,
    safe_divide,
    ts_idxmax,
    ts_idxmin,
    ts_max,
    ts_mean,
    ts_min,
    ts_rank,
    ts_std,
    ts_sum,
)
from factor_engine.expressions.qlib_alpha import _prepare_qlib_frame
from factor_engine.registry import register_factor_set


GTJA_ALPHA191_FEATURE_NAMES = [f"GTJA{i:03d}" for i in range(1, 192)]

GTJA_ALPHA191_EXACT_FORMULA_IDS = {
    1, 2, 6, 12, 13, 18, 19, 41, 46, 81, 85, 94, 95, 96, 97, 103,
    104, 106, 107, 108, 110, 111, 116, 117, 120, 122, 153, 155,
    158, 160, 161, 167, 168, 170, 171, 173, 174, 175, 176, 177,
    178, 191,
}


def _sma(series, n, m=1):
    """GTJA-style recursive SMA: y_t = (m*x_t + (n-m)*y_{t-1}) / n."""
    alpha = float(m) / max(float(n), 1.0)
    return series.ewm(alpha=alpha, adjust=False, min_periods=max(int(n), 1)).mean()


def _rank(series, window=20):
    """Single-stock proxy for GTJA cross-sectional RANK."""
    return ts_rank(series, max(int(window), 2))


def _decay_linear(series, window):
    weights = np.arange(1, int(window) + 1, dtype=float)
    denom = weights.sum()
    return series.rolling(window=int(window), min_periods=int(window)).apply(
        lambda values: float(np.dot(values, weights) / denom),
        raw=True,
    )


def _signed_power(base, exponent):
    return np.sign(base) * np.power(np.abs(base).clip(lower=1e-12), exponent)


def _true_range(high, low, close):
    prev_close = ref(close, 1)
    return pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def _sanitize_columns(columns, index):
    result = {}
    for name in GTJA_ALPHA191_FEATURE_NAMES:
        value = columns.get(name)
        if value is None:
            value = pd.Series(np.nan, index=index)
        if not isinstance(value, pd.Series):
            value = pd.Series(value, index=index)
        result[name] = pd.to_numeric(value, errors="coerce").replace([np.inf, -np.inf], np.nan)
    return pd.DataFrame(result, index=index)


@register_factor_set("gtja_alpha191")
class GTJAAlpha191FactorSet(BaseFactorSet):
    """GTJA Alpha191 style short-horizon price/volume feature set."""

    name = "gtja_alpha191"
    description = "GTJA Alpha191 style feature set for HK single-stock materialization"
    version = "0.1.0"

    def transform(self, frame, context=None):
        qlib_frame = _prepare_qlib_frame(frame)
        if qlib_frame.empty:
            return pd.DataFrame(columns=GTJA_ALPHA191_FEATURE_NAMES)

        open_ = qlib_frame["open"]
        high = qlib_frame["high"]
        low = qlib_frame["low"]
        close = qlib_frame["close"]
        volume = qlib_frame["volume"].replace(0, np.nan)
        vwap = qlib_frame["vwap"]
        amount = volume * vwap
        ret = safe_divide(close, ref(close, 1)) - 1.0
        mid = (high + low) / 2.0
        spread = high - low
        columns: dict[str, pd.Series] = {}

        # Representative formulas explicitly listed in local reference docs and
        # public GTJA191 implementations.  RANK is a rolling single-stock proxy.
        columns["GTJA001"] = -corr(_rank(delta(log(volume), 1), 20), _rank(safe_divide(close - open_, open_), 20), 6)
        columns["GTJA002"] = -delta(safe_divide((close - low) - (high - close), spread), 1)
        columns["GTJA006"] = -_rank(np.sign(delta(open_ * 0.85 + high * 0.15, 4)), 20)
        columns["GTJA012"] = _rank(open_ - ts_mean(vwap, 10), 20) * -_rank((close - vwap).abs(), 20)
        columns["GTJA013"] = np.sqrt((high * low).clip(lower=0)) - vwap
        columns["GTJA018"] = safe_divide(close, ref(close, 5))
        columns["GTJA019"] = pd.Series(np.where(
            close < ref(close, 5),
            safe_divide(close - ref(close, 5), ref(close, 5)),
            np.where(close > ref(close, 5), safe_divide(close - ref(close, 5), close), 0.0),
        ), index=close.index)
        columns["GTJA041"] = -((high * low).clip(lower=0).pow(0.5) - vwap)
        columns["GTJA046"] = -safe_divide((ts_mean(close, 3) + ts_mean(close, 6) + ts_mean(close, 12)) / 3.0 - close, close)
        columns["GTJA081"] = _sma(ts_max(volume, 5), 5, 1) * _rank(delta(close, 5), 20)
        columns["GTJA085"] = ts_rank(safe_divide(volume, ts_mean(volume, 20)), 20) * ts_rank(-delta(close, 7), 8)
        obv_delta = pd.Series(
            np.where(close > ref(close, 1), volume, np.where(close < ref(close, 1), -volume, 0.0)),
            index=close.index,
        )
        columns["GTJA094"] = ts_sum(obv_delta, 30)
        columns["GTJA095"] = ts_std(amount, 20)
        stoch = safe_divide(close - ts_min(low, 9), ts_max(high, 9) - ts_min(low, 9)) * 100.0
        columns["GTJA096"] = _sma(_sma(stoch, 3, 1), 3, 1)
        columns["GTJA097"] = ts_std(volume, 10)
        columns["GTJA103"] = safe_divide(20.0 - ts_idxmin(low, 20), 20.0) * 100.0
        columns["GTJA104"] = -delta(corr(high, volume, 5), 5) * _rank(ts_std(close, 20), 20)
        columns["GTJA106"] = close - ref(close, 20)
        columns["GTJA107"] = -_rank(open_ - ref(high, 1), 20) * _rank(open_ - ref(close, 1), 20) * _rank(open_ - ref(low, 1), 20)
        columns["GTJA108"] = -_signed_power(_rank(high - ts_min(high, 2), 20), _rank(corr(vwap, ts_mean(volume, 120), 6), 20))
        columns["GTJA110"] = safe_divide(ts_sum((high - ref(close, 1)).clip(lower=0), 20), ts_sum((ref(close, 1) - low).clip(lower=0), 20)) * 100.0
        money_flow = volume * safe_divide((close - low) - (high - close), spread)
        columns["GTJA111"] = _sma(money_flow, 11, 2) - _sma(money_flow, 4, 2)
        columns["GTJA116"] = safe_divide(ts_sum((close - ts_mean(close, 20)) * pd.Series(np.arange(len(close)), index=close.index), 20), ts_sum(pd.Series(np.arange(len(close)), index=close.index), 20))
        columns["GTJA117"] = ts_rank(volume, 32) * (1.0 - ts_rank(close + high - low, 16)) * (1.0 - ts_rank(ret, 32))
        columns["GTJA120"] = safe_divide(_rank(vwap - close, 20), _rank(vwap + close, 20))
        triple_sma_log = _sma(_sma(_sma(log(close), 13, 2), 13, 2), 13, 2)
        columns["GTJA122"] = safe_divide(triple_sma_log - ref(triple_sma_log, 1), ref(triple_sma_log, 1))
        columns["GTJA153"] = (ts_mean(close, 3) + ts_mean(close, 6) + ts_mean(close, 12) + ts_mean(close, 24)) / 4.0
        columns["GTJA155"] = _sma(volume, 13, 2) - _sma(volume, 27, 6) - ts_mean(ref(volume, 9), 3) - corr(ts_mean(close, 3), ts_mean(volume, 27), 5)
        columns["GTJA158"] = safe_divide((high - _sma(close, 15, 2)) - (low - _sma(close, 15, 2)), close)
        columns["GTJA160"] = _sma(pd.Series(np.where(close <= ref(close, 1), ts_std(close, 20), 0.0), index=close.index), 20, 1)
        columns["GTJA161"] = ts_mean(_true_range(high, low, close), 12)
        columns["GTJA167"] = ts_sum((close - ref(close, 1)).clip(lower=0), 12)
        columns["GTJA168"] = -safe_divide(volume, ts_mean(volume, 20))
        columns["GTJA170"] = safe_divide(_rank(1.0 / close, 20) * volume, ts_mean(volume, 20)) * safe_divide(high * _rank(high - close, 20), ts_mean(high, 5)) - _rank(vwap - ref(vwap, 5), 20)
        columns["GTJA171"] = safe_divide(-1.0 * (low - close) * open_.pow(5), (close - high) * close.pow(5))
        columns["GTJA173"] = 3.0 * _sma(close, 13, 2) - 2.0 * _sma(_sma(close, 13, 2), 13, 2) + _sma(_sma(_sma(log(close), 13, 2), 13, 2), 13, 2)
        columns["GTJA174"] = _sma(pd.Series(np.where(close > ref(close, 1), ts_std(close, 20), 0.0), index=close.index), 20, 1)
        columns["GTJA175"] = ts_mean(_true_range(high, low, close), 6)
        columns["GTJA176"] = corr(_rank(safe_divide(close - ts_min(low, 12), ts_max(high, 12) - ts_min(low, 12)), 20), _rank(volume, 20), 6)
        columns["GTJA177"] = safe_divide(20.0 - ts_idxmax(high, 20), 20.0) * 100.0
        columns["GTJA178"] = safe_divide(close - ref(close, 1), ref(close, 1)) * volume
        columns["GTJA191"] = -(corr(ts_mean(volume, 20), low, 5) + (mid - close))

        # Deterministic proxy formulas for the remaining slots.  They retain the
        # GTJA design vocabulary: short-cycle reversal, volume-price divergence,
        # volatility, trend and decay-linear interactions.
        for i in range(1, 192):
            name = f"GTJA{i:03d}"
            if name in columns:
                continue
            w1 = [3, 5, 6, 8, 10, 12, 15, 20][i % 8]
            w2 = [5, 10, 15, 20, 30, 40, 60][i % 7]
            family = i % 6
            if family == 0:
                columns[name] = -_rank(safe_divide(close - ts_mean(close, w2), close), w2)
            elif family == 1:
                columns[name] = _rank(delta(close, w1), w2) * _rank(safe_divide(volume, ts_mean(volume, w2)), w2)
            elif family == 2:
                columns[name] = -corr(_rank(volume, w1), _rank(high, w1), min(w1, 10))
            elif family == 3:
                columns[name] = safe_divide(ts_std(close, w2), close) * _rank(spread, w2)
            elif family == 4:
                columns[name] = _decay_linear(safe_divide(vwap - close, close), w1) + corr(vwap, ref(close, w1), min(w2, 20))
            else:
                columns[name] = safe_divide(ts_sum((close - ref(close, 1)).clip(lower=0), w2), ts_sum((close - ref(close, 1)).abs(), w2))

        return _sanitize_columns(columns, qlib_frame.index)

    def metadata(self):
        return FactorSetMetadata(
            name=self.name,
            description=self.description,
            version=self.version,
            assumptions=(
                "VWAP is approximated with OHLC4 when not available in clean layer",
                "Original cross-sectional RANK is represented by a rolling time-series percentile proxy",
                "Publicly documented representative formulas are exact-style; remaining slots use deterministic GTJA-style proxy formulas",
            ),
            extra={
                "feature_count": len(GTJA_ALPHA191_FEATURE_NAMES),
                "feature_names": GTJA_ALPHA191_FEATURE_NAMES,
                "exact_style_formula_ids": sorted(GTJA_ALPHA191_EXACT_FORMULA_IDS),
                "proxy_formula_count": len(GTJA_ALPHA191_FEATURE_NAMES) - len(GTJA_ALPHA191_EXACT_FORMULA_IDS),
                "reference": "docs/reference/gtja_191_alpha_factors.md",
            },
        )
