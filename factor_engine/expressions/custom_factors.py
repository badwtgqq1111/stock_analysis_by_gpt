#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""HK-specific manual and GTJA factors extending Alpha158.

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


HK_CUSTOM_FACTOR_NAMES = [
    "price_position_52w_high",
    "pb_ratio_sector_relative",
    "sector_rps_reversal_20d",
    "volume_price_divergence_10d",
    "consecutive_up_days_5d",
    "momentum_12m_skip_1m",
    "short_term_reversal_1m",
    "turnover_rate",
    "buying_pressure",
]


@register_factor_set("alpha158_hk")
class Alpha158HKFactorSet(BaseFactorSet):
    """Alpha158 + HK manual custom factors + GTJA Alpha191."""

    name = "alpha158_hk"
    description = "Alpha158 extended with HK-specific manual factors and GTJA Alpha191"
    version = "3.0.0"

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

        window_52w = 252 if n_bars >= 252 else 120

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

        # 6. momentum_12m_skip_1m — t-12 to t-1 month return (skip last month)
        columns["momentum_12m_skip_1m"] = safe_divide(close.shift(21), close.shift(252)) - 1.0

        # 7. short_term_reversal_1m — last month return (reversal signal)
        columns["short_term_reversal_1m"] = safe_divide(close, close.shift(21)) - 1.0

        # 8. turnover_rate — volume / total_shares * 100 (liquidity normalized by float)
        if "total_shares" in qlib_frame.columns and not qlib_frame["total_shares"].isna().all():
            total_shares = qlib_frame["total_shares"]
            columns["turnover_rate"] = safe_divide(volume, total_shares) * 100.0
        else:
            columns["turnover_rate"] = np.nan

        # 9. buying_pressure — intraday buy pressure proxy (bid/ask ratio substitute)
        hl_range = high - low
        columns["buying_pressure"] = np.clip(safe_divide(close - low, hl_range), 0.0, 1.0)

        custom_df = pd.DataFrame(columns, index=qlib_frame.index)
        gtja = create_factor_set("gtja_alpha191", config=self.config).transform(frame, context=context)

        if base.empty:
            return pd.concat([custom_df, gtja], axis=1) if not gtja.empty else custom_df
        frames = [base, custom_df]
        if not gtja.empty:
            frames.append(gtja)
        return pd.concat(frames, axis=1)

    def metadata(self):
        alpha158_meta = create_factor_set("qlib_alpha158", config=self.config).metadata().to_dict()
        gtja_meta = create_factor_set("gtja_alpha191", config=self.config).metadata().to_dict()
        alpha158_names = list((alpha158_meta.get("extra") or {}).get("feature_names") or [])
        gtja_names = list((gtja_meta.get("extra") or {}).get("feature_names") or [])
        feature_names = alpha158_names + HK_CUSTOM_FACTOR_NAMES + gtja_names
        return FactorSetMetadata(
            name=self.name,
            description=self.description,
            version=self.version,
            assumptions=tuple(
                list(alpha158_meta.get("assumptions") or [])
                + list(gtja_meta.get("assumptions") or [])
            ),
            extra={
                "feature_count": len(feature_names),
                "feature_names": feature_names,
                "base_feature_set": "qlib_alpha158",
                "hk_custom_factor_count": len(HK_CUSTOM_FACTOR_NAMES),
                "gtja_feature_count": len(gtja_names),
                "gtja_proxy_formula_count": (gtja_meta.get("extra") or {}).get("proxy_formula_count"),
            },
        )
