#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""半年低点右侧确认后的短期动量信号。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from factor_engine.signals.base import SignalRecipe, SignalRecipeResult
from factor_engine.signals.registry import register_signal_recipe


def _num(frame, *names):
    for name in names:
        if name in frame.columns:
            return pd.to_numeric(frame[name], errors="coerce")
    return None


@register_signal_recipe("bottom_momentum")
class BottomMomentumRecipe(SignalRecipe):
    """识别过去 126 个交易日形成低点、并出现右侧短期动量的股票。

    信号在收盘后计算，调用方应使用下一交易日成交。仅依赖历史窗口，
    因而适合作为 Signal 候选层；ML 可在候选结果上继续排序。
    """
    name = "bottom_momentum"

    def __init__(self, lookback=126, momentum_windows=(5, 10, 20),
                 min_rebound_atr=1.0, min_relative20=0.0,
                 min_volume_ratio=1.1, min_score=55.0,
                 expected_holding_days=10, stop_atr=1.5, **kwargs):
        self.lookback = max(30, int(lookback))
        self.momentum_windows = tuple(int(x) for x in momentum_windows)
        self.min_rebound_atr = float(min_rebound_atr)
        self.min_relative20 = float(min_relative20)
        self.min_volume_ratio = float(min_volume_ratio)
        self.min_score = float(min_score)
        self.expected_holding_days = int(expected_holding_days)
        self.stop_atr = float(stop_atr)
        self.extra_config = dict(kwargs)

    def evaluate(self, data, context=None):
        if data is None or len(data) == 0:
            return self._result({})
        frame = pd.DataFrame(data).copy().sort_index()
        close = _num(frame, "Close", "close")
        low = _num(frame, "Low", "low")
        high = _num(frame, "High", "high")
        volume = _num(frame, "Volume", "volume")
        if low is None: low = close
        if high is None: high = close
        if close is None or len(close.dropna()) < self.lookback + 1:
            return self._result({})
        close = close.reset_index(drop=True)
        low = (low if low is not None else close).reset_index(drop=True)
        high = (high if high is not None else close).reset_index(drop=True)
        volume = (volume if volume is not None else pd.Series(np.nan, index=frame.index)).reset_index(drop=True)
        latest = float(close.iloc[-1])
        if not np.isfinite(latest) or latest <= 0:
            return self._result({})

        low126 = float(low.iloc[-self.lookback:].min())
        prior_high = float(high.iloc[-self.lookback:].max())
        returns = close.pct_change()
        tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
        atr20 = float(tr.tail(20).mean()) if tr.tail(20).notna().any() else np.nan
        rebound_atr = (latest - low126) / atr20 if np.isfinite(atr20) and atr20 > 0 else np.nan
        ma20 = float(close.tail(20).mean())
        ma60 = float(close.tail(60).mean())
        # right-side confirmation: low occurred before the latest 3 bars and was not re-tested
        low_pos = int(low.iloc[-self.lookback:].values.argmin())
        bars_since_low = self.lookback - 1 - low_pos
        no_new_low_3d = bool(low.tail(3).min() > low126 * 0.995)
        vol_ma20 = float(volume.tail(20).mean()) if volume.tail(20).notna().any() else np.nan
        vol_ratio = float(volume.iloc[-1] / vol_ma20) if np.isfinite(vol_ma20) and vol_ma20 > 0 else np.nan

        def ret(n):
            return float(close.iloc[-1] / close.iloc[-n-1] - 1.0) if len(close) > n and close.iloc[-n-1] else np.nan
        mom5, mom10, mom20 = ret(5), ret(10), ret(20)
        rel20 = _num(frame, "relative20", "stock_vs_industry_ret_20d", "rel20")
        industry_return_20d = (context or {}).get("industry_return_20d")
        if rel20 is not None and pd.notna(rel20.iloc[-1]):
            relative20 = float(rel20.iloc[-1])
        elif industry_return_20d is not None and np.isfinite(industry_return_20d):
            relative20 = float(mom20 - float(industry_return_20d))
        else:
            # Stand-alone research callers may not supply industry context.
            # Keep the output usable, while marking the fallback explicitly.
            relative20 = mom20
        accel = mom5 - mom20 / 4.0 if np.isfinite(mom5) and np.isfinite(mom20) else np.nan

        score = 0.0
        if bars_since_low >= 3 and no_new_low_3d: score += 20
        if np.isfinite(rebound_atr) and rebound_atr >= self.min_rebound_atr: score += 20
        if latest > ma20: score += 15
        if latest > ma60: score += 10
        if np.isfinite(vol_ratio) and vol_ratio >= self.min_volume_ratio: score += 10
        if np.isfinite(relative20) and relative20 > self.min_relative20: score += 10
        if np.isfinite(accel) and accel > 0: score += 5
        triggered = (bars_since_low >= 3 and no_new_low_3d
                     and np.isfinite(rebound_atr) and rebound_atr >= self.min_rebound_atr
                     and latest > ma20 and relative20 > self.min_relative20
                     and score >= self.min_score)
        stop = latest - self.stop_atr * atr20 if np.isfinite(atr20) else np.nan
        payload = {
            "setup_type": "bottom_momentum" if triggered else "neutral",
            "setup_score": float(score), "bottom_momentum_score": float(score),
            "low126": low126, "distance_to_low126": latest / low126 - 1,
            "rebound_atr": rebound_atr, "bars_since_low": bars_since_low,
            "no_new_low_3d": no_new_low_3d, "ma20": ma20, "ma60": ma60,
            "momentum_5d": mom5, "momentum_10d": mom10, "momentum_20d": mom20,
            "acceleration": accel, "relative20": relative20,
            "industry_return_20d": industry_return_20d,
            "volume_ratio_20": vol_ratio, "atr20": atr20, "stop_price": stop,
            "expected_holding_days": self.expected_holding_days,
            "recipe_scores": {"bottom_momentum": float(score)},
        }
        return self._result(payload)

    def _result(self, payload):
        defaults = {"setup_type": "neutral", "setup_score": 0.0,
                    "bottom_momentum_score": 0.0,
                    "recipe_scores": {"bottom_momentum": 0.0},
                    "expected_holding_days": self.expected_holding_days}
        defaults.update(payload)
        return SignalRecipeResult(self.name, defaults["setup_type"],
                                  float(defaults["setup_score"]), defaults)
