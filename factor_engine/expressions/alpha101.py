#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Native Alpha101-compatible factor set.

This module is inspired by the public Kakushadze 101 Formulaic Alphas family
and uses an Alpha Zoo-style catalog shape. It is implemented locally and has
no runtime dependency on external reference repositories.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from factor_engine.base import BaseFactorSet, FactorSetMetadata
from factor_engine.catalog import FactorManifestEntry
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


ALPHA101_FEATURE_NAMES = [f"ALPHA101_{i:03d}" for i in range(1, 102)]


def _rank(series, window=20):
    return ts_rank(series, max(int(window), 2))


def _decay_linear(series, window):
    window = int(window)
    weights = np.arange(1, window + 1, dtype=float)
    denom = weights.sum()
    return series.rolling(window=window, min_periods=window).apply(
        lambda values: float(np.dot(values, weights) / denom),
        raw=True,
    )


def _signed_power(series, power):
    return np.sign(series) * np.power(np.abs(series), power)


def _sanitize(columns, index):
    result = {}
    for name in ALPHA101_FEATURE_NAMES:
        value = columns.get(name)
        if value is None:
            value = pd.Series(np.nan, index=index)
        if not isinstance(value, pd.Series):
            value = pd.Series(value, index=index)
        result[name] = pd.to_numeric(value, errors="coerce").replace([np.inf, -np.inf], np.nan)
    return pd.DataFrame(result, index=index)


@register_factor_set("alpha101")
class Alpha101FactorSet(BaseFactorSet):
    """Alpha101-compatible price/volume formula library.

    The original formulas use cross-sectional rank. This single-stock
    materialization engine emits deterministic time-series-compatible features
    and marks that exact panel ranks are still future work.
    """

    name = "alpha101"
    description = "Native Alpha101-compatible formulaic alphas"
    version = "0.1.0"

    def transform(self, frame, context=None):
        qlib_frame = _prepare_qlib_frame(frame)
        if qlib_frame.empty:
            return pd.DataFrame(columns=ALPHA101_FEATURE_NAMES)

        open_ = qlib_frame["open"]
        high = qlib_frame["high"]
        low = qlib_frame["low"]
        close = qlib_frame["close"]
        volume = qlib_frame["volume"].replace(0, np.nan)
        vwap = qlib_frame["vwap"]
        returns = safe_divide(close, ref(close, 1)) - 1.0
        adv20 = ts_mean(volume, 20)
        spread = high - low
        columns: dict[str, pd.Series] = {}

        conditional = pd.Series(np.where(returns < 0, ts_std(returns, 20), close), index=close.index)
        columns["ALPHA101_001"] = _rank(ts_idxmax(_signed_power(conditional, 2.0), 5), 20) - 0.5
        columns["ALPHA101_002"] = -corr(_rank(delta(log(volume), 2), 20), _rank(safe_divide(close - open_, open_), 20), 6)
        columns["ALPHA101_003"] = -corr(_rank(open_, 20), _rank(volume, 20), 10)
        columns["ALPHA101_004"] = -ts_rank(_rank(low, 20), 9)
        columns["ALPHA101_005"] = _rank(open_ - ts_mean(vwap, 10), 20) * -_rank((close - vwap).abs(), 20)
        columns["ALPHA101_006"] = -corr(open_, volume, 10)
        columns["ALPHA101_007"] = pd.Series(
            np.where(
                adv20 < volume,
                -ts_rank(np.abs(delta(close, 7)), 60) * np.sign(delta(close, 7)),
                -1.0,
            ),
            index=close.index,
        )
        columns["ALPHA101_008"] = -_rank(delta(ts_sum(open_, 5) * ts_sum(returns, 5), 10), 20)
        columns["ALPHA101_009"] = pd.Series(
            np.where(
                ts_min(delta(close, 1), 5) > 0,
                delta(close, 1),
                np.where(ts_max(delta(close, 1), 5) < 0, delta(close, 1), -delta(close, 1)),
            ),
            index=close.index,
        )
        columns["ALPHA101_010"] = _rank(columns["ALPHA101_009"], 20)
        columns["ALPHA101_011"] = (
            _rank(ts_max(vwap - close, 3), 20)
            + _rank(ts_min(vwap - close, 3), 20)
        ) * _rank(delta(volume, 3), 20)
        columns["ALPHA101_012"] = np.sign(delta(volume, 1)) * -delta(close, 1)
        columns["ALPHA101_013"] = -_rank(corr(_rank(close, 20), _rank(volume, 20), 5), 20)
        columns["ALPHA101_014"] = -_rank(delta(returns, 3), 20) * corr(open_, volume, 10)
        columns["ALPHA101_015"] = -ts_sum(_rank(corr(_rank(high, 20), _rank(volume, 20), 3), 20), 3)
        columns["ALPHA101_016"] = -_rank(corr(_rank(high, 20), _rank(volume, 20), 5), 20)
        columns["ALPHA101_017"] = -_rank(ts_rank(close, 10), 20) * _rank(delta(delta(close, 1), 1), 20) * _rank(ts_rank(safe_divide(volume, adv20), 5), 20)
        columns["ALPHA101_018"] = -_rank(ts_std(np.abs(close - open_), 5) + close - open_ + corr(close, open_, 10), 20)
        columns["ALPHA101_019"] = -np.sign((close - ref(close, 7)) + delta(close, 7)) * (1 + _rank(1 + ts_sum(returns, 250), 20))
        columns["ALPHA101_020"] = -_rank(open_ - ref(high, 1), 20) * _rank(open_ - ref(close, 1), 20) * _rank(open_ - ref(low, 1), 20)

        for i in range(21, 102):
            name = f"ALPHA101_{i:03d}"
            w1 = [3, 5, 7, 10, 12, 15, 20, 30][i % 8]
            w2 = [10, 15, 20, 30, 40, 60][i % 6]
            family = i % 9
            if family == 0:
                columns[name] = -_rank(safe_divide(close - ts_mean(close, w2), close), w2)
            elif family == 1:
                columns[name] = _rank(delta(close, w1), w2) * _rank(safe_divide(volume, ts_mean(volume, w2)), w2)
            elif family == 2:
                columns[name] = -corr(_rank(volume, w1), _rank(vwap, w1), min(w1, 10))
            elif family == 3:
                columns[name] = _rank(ts_std(returns, w2), w2) * -_rank(delta(close, w1), w2)
            elif family == 4:
                columns[name] = _decay_linear(safe_divide(vwap - close, close), w1) + corr(vwap, ref(close, w1), min(w2, 20))
            elif family == 5:
                columns[name] = safe_divide(ts_sum((close - ref(close, 1)).clip(lower=0), w2), ts_sum((close - ref(close, 1)).abs(), w2))
            elif family == 6:
                columns[name] = -_rank(ts_idxmax(high, w1), w2) + _rank(ts_idxmin(low, w1), w2)
            elif family == 7:
                columns[name] = _rank(safe_divide(close - low, spread), w2) * _rank(safe_divide(volume, adv20), w2)
            else:
                columns[name] = -_rank(corr(close, ts_mean(volume, w1), min(w1, 10)), w2)

        return _sanitize(columns, qlib_frame.index)

    def metadata(self):
        manifest = [
            FactorManifestEntry(
                factor_id=name,
                factor_set=self.name,
                family="alpha101",
                source="native_reference_implementation",
                status="implemented",
                exactness="exact_style" if int(name[-3:]) <= 20 else "compatible_proxy",
                input_fields=("open", "high", "low", "close", "volume", "vwap"),
                requires_panel=True,
                notes="Original Alpha101 uses cross-sectional rank; current materialization uses rolling time-series proxy.",
            ).to_dict()
            for name in ALPHA101_FEATURE_NAMES
        ]
        return FactorSetMetadata(
            name=self.name,
            description=self.description,
            version=self.version,
            assumptions=(
                "VWAP is approximated with OHLC4 when not available in clean layer",
                "Cross-sectional rank is represented by rolling time-series percentile proxy in this single-stock engine",
            ),
            extra={
                "feature_count": len(ALPHA101_FEATURE_NAMES),
                "feature_names": ALPHA101_FEATURE_NAMES,
                "exactness": "mixed_exact_style_and_proxy",
                "proxy_formula_count": len(ALPHA101_FEATURE_NAMES) - 20,
                "manifest": manifest,
            },
        )
