#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Academic style factors for HK equities."""

from __future__ import annotations

import numpy as np
import pandas as pd

from factor_engine.base import BaseFactorSet, FactorSetMetadata
from factor_engine.catalog import FactorManifestEntry
from factor_engine.expressions.operators import corr, safe_divide, ts_mean, ts_std
from factor_engine.expressions.qlib_alpha import _prepare_qlib_frame
from factor_engine.registry import register_factor_set


ACADEMIC_FEATURE_NAMES = [
    "academic_mkt_beta_proxy",
    "academic_smb_size_proxy",
    "academic_hml_value_proxy",
    "academic_rmw_quality_proxy",
    "academic_cma_investment_proxy",
    "academic_carhart_mom",
]


def _zscore(series):
    mean = series.rolling(120, min_periods=60).mean()
    std = series.rolling(120, min_periods=60).std()
    return safe_divide(series - mean, std)


@register_factor_set("academic_hk")
class AcademicHKFactorSet(BaseFactorSet):
    """Fama-French/Carhart-inspired local proxies."""

    name = "academic_hk"
    description = "Academic style factor proxies for HK equities"
    version = "0.1.0"

    def transform(self, frame, context=None):
        qlib_frame = _prepare_qlib_frame(frame)
        if qlib_frame.empty:
            return pd.DataFrame(columns=ACADEMIC_FEATURE_NAMES)

        close = qlib_frame["close"]
        volume = qlib_frame["volume"].replace(0, np.nan)
        high = qlib_frame["high"]
        low = qlib_frame["low"]
        returns = safe_divide(close, close.shift(1)) - 1.0
        market_proxy = ts_mean(returns, 60)
        dollar_volume = close * volume
        volatility = ts_std(returns, 60)

        columns = {
            "academic_mkt_beta_proxy": corr(returns, market_proxy, 60),
            "academic_smb_size_proxy": -_zscore(np.log(ts_mean(dollar_volume, 60) + 1.0)),
            "academic_hml_value_proxy": -_zscore(safe_divide(close, ts_mean(close, 252))),
            "academic_rmw_quality_proxy": _zscore(safe_divide(close - low, high - low) - volatility),
            "academic_cma_investment_proxy": -_zscore(ts_std(safe_divide(close, close.shift(20)) - 1.0, 120)),
            "academic_carhart_mom": safe_divide(close.shift(21), close.shift(252)) - 1.0,
        }
        return pd.DataFrame(columns, index=qlib_frame.index).reindex(columns=ACADEMIC_FEATURE_NAMES)

    def metadata(self):
        manifest = [
            FactorManifestEntry(
                factor_id=name,
                factor_set=self.name,
                family="academic",
                source="native_factor_proxy",
                status="implemented",
                exactness="price_volume_proxy",
                input_fields=("open", "high", "low", "close", "volume"),
                lookback=252,
                notes="Proxy for academic style exposure; PIT financial fields can replace proxy components later.",
            ).to_dict()
            for name in ACADEMIC_FEATURE_NAMES
        ]
        return FactorSetMetadata(
            name=self.name,
            description=self.description,
            version=self.version,
            assumptions=("Academic factors are local price/volume proxies, not official portfolio factor returns.",),
            extra={
                "feature_count": len(ACADEMIC_FEATURE_NAMES),
                "feature_names": ACADEMIC_FEATURE_NAMES,
                "exactness": "price_volume_proxy",
                "manifest": manifest,
            },
        )
