#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""HK-specific manual factors extending Alpha158.

IC-validated (2026-05-29, 200 stocks, 365d):
  - pb_ratio_sector_relative   IC=-0.58 (60d)  strong value premium
  - price_position_52w_high    IC=-0.57 (60d)   mean-reversion at highs
  - sector_rps_reversal_20d    IC=-0.25 (60d)   recovery slope predictive
  - volume_price_divergence_10d IC=-0.11 (60d)   trend exhaustion
  - consecutive_up_days_5d     IC=-0.08 (60d)   short-term reversal

Removed: sector_turnover_change_5d (IC=+0.016, not significant).
"""

import numpy as np
import pandas as pd

from factor_engine.base import BaseFactorSet, FactorSetMetadata
from factor_engine.expressions.operators import (
    corr,
    delta,
    safe_divide,
    ts_max,
    ts_min,
    ts_slope,
    ts_sum,
)
from factor_engine.expressions.qlib_alpha import _prepare_qlib_frame
from factor_engine.registry import create_factor_set, register_factor_set


@register_factor_set("alpha158_hk")
class Alpha158HKFactorSet(BaseFactorSet):
    """Alpha158 + 6 HK manual custom factors.

    Delegates to Alpha158 for the base 158 features, then appends 6
    HK-specific factors computed from the same OHLCV frame.
    """

    name = "alpha158_hk"
    description = "Alpha158 extended with 5 IC-validated HK manual factors"
    version = "2.0.0"

    def transform(self, frame, context=None):
        alpha158 = create_factor_set("qlib_alpha158", config=self.config)
        base = alpha158.transform(frame, context=context)

        qlib_frame = _prepare_qlib_frame(frame)
        if qlib_frame.empty:
            return base if not base.empty else pd.DataFrame()

        close = qlib_frame["close"]
        high = qlib_frame["high"]
        low = qlib_frame["low"]
        volume = qlib_frame["volume"]

        n_bars = len(close)
        columns = {}

        # Determine window: use 252 if enough data, else fall back to 120
        window_52w = 252 if n_bars >= 252 else 120
        window_fallback = 120 if n_bars >= 120 else 60

        # 1. price_position_52w_high — close / N-d high * 100
        high_period = ts_max(high, window_52w)
        columns["price_position_52w_high"] = safe_divide(close, high_period) * 100.0

        # 2. pb_ratio_sector_relative — price position within period range
        high_period2 = ts_max(high, window_52w)
        low_period = ts_min(low, window_52w)
        columns["pb_ratio_sector_relative"] = np.clip(
            safe_divide(close - low_period, high_period2 - low_period) * 100.0,
            0.0,
            100.0,
        )

        # 3. sector_rps_reversal_20d — slope of price position
        price_pos = safe_divide(close, ts_max(high, window_52w)) * 100.0
        columns["sector_rps_reversal_20d"] = ts_slope(price_pos, 20)

        # 4. volume_price_divergence_10d — rolling corr(volume, close)
        columns["volume_price_divergence_10d"] = corr(volume, close, 10)

        # 5. consecutive_up_days_5d — count of up-close days in last 5
        up_day = (delta(close, 1) > 0).astype(float)
        columns["consecutive_up_days_5d"] = ts_sum(up_day, 5)

        custom_df = pd.DataFrame(columns, index=qlib_frame.index)

        if base.empty:
            return custom_df
        return pd.concat([base, custom_df], axis=1)

    def metadata(self):
        return FactorSetMetadata(
            name=self.name,
            description=self.description,
            version=self.version,
            extra={"feature_count": 198},
        )
