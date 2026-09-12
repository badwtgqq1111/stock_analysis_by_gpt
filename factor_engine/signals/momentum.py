#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""短线动量 sleeve 的入场信号 recipe。

事件研究（2024-01 ~ 2026-09，全市场 3.42M 股票日，超额收益口径）显示：

    涨停（当日涨幅 >= 9.5%）       +1 日 +1.30% / +5 日 +0.68% / +10 日 +0.13%
    涨停 且 收盘 > 20 日高          +1 日 +1.44% / +5 日 +0.54%
    20 日通道突破（放量确认）        +1 日 +0.00% / +5 日 -0.56%

即：温和突破与"突破后回踩"在该样本期是负期望，唯一稳健为正的是涨停型
短周期动量，且优势在 5-10 个交易日内衰减到零。因此本 recipe 只负责识别
"强势动量日"，并由组合层以独立小资金额度 + 硬止损 + 固定持有窗口使用，
不作为中期持仓理由。
"""

import numpy as np
import pandas as pd

from factor_engine.signals.base import SignalRecipe, SignalRecipeResult
from factor_engine.signals.registry import register_signal_recipe


def _empty_momentum_snapshot():
    return {
        "setup_type": "neutral",
        "setup_score": 0.0,
        "sideways_penalty": 0.0,
        "momentum_score": 0.0,
        "gain_1d": np.nan,
        "latest_close": np.nan,
        "ma20": np.nan,
        "donchian_window": 20,
        "donchian_upper": np.nan,
        "donchian_lower": np.nan,
        "donchian_pos": np.nan,
        "channel_width": np.nan,
        "distance_to_upper": np.nan,
        "breakout_confirmed": False,
        "volume_ratio_20": np.nan,
        "median_amount_20": np.nan,
        "liquidity_ok": False,
        "stop_price": np.nan,
        "expected_holding_days": 5,
        "recipe_scores": {"limit_momentum": 0.0},
    }


@register_signal_recipe("limit_momentum")
class LimitMomentumRecipe(SignalRecipe):
    """识别涨停/强势动量日，供短周期 sleeve 使用（1-5 个交易日持有窗口）。"""

    name = "limit_momentum"

    def __init__(
        self,
        min_gain=0.095,
        channel_window=20,
        min_median_amount_20d=50_000_000.0,
        require_breakout=False,
        stop_pct=0.05,
        expected_holding_days=5,
        min_score=60.0,
        **kwargs,
    ):
        self.min_gain = float(min_gain)
        self.channel_window = max(2, int(channel_window))
        self.min_median_amount_20d = float(min_median_amount_20d)
        self.require_breakout = bool(require_breakout)
        self.stop_pct = float(stop_pct)
        self.expected_holding_days = int(expected_holding_days)
        self.min_score = float(min_score)
        self.extra_config = dict(kwargs)

    @staticmethod
    def _column(frame, *names):
        for name in names:
            if name in frame.columns:
                return pd.to_numeric(frame[name], errors="coerce")
        return None

    def evaluate(self, data, context=None):
        snapshot = _empty_momentum_snapshot()
        snapshot["donchian_window"] = self.channel_window
        if data is None or len(data) == 0:
            return self._to_result(snapshot)

        working = data.copy()
        try:
            working = working.sort_index()
        except TypeError:
            pass
        close = self._column(working, "Close", "close")
        high = self._column(working, "High", "high")
        low = self._column(working, "Low", "low")
        volume = self._column(working, "Volume", "volume")
        amount = self._column(working, "Amount", "amount")
        if close is None or volume is None or close.dropna().empty:
            return self._to_result(snapshot)
        if len(close) < 21:
            return self._to_result(snapshot)

        latest_close = float(close.iloc[-1])
        previous_close = float(close.iloc[-2])
        if not previous_close:
            return self._to_result(snapshot)
        gain_1d = latest_close / previous_close - 1.0
        ma20 = float(close.tail(20).mean())
        volume_ma20 = float(volume.tail(20).mean()) if volume.notna().any() else np.nan
        volume_ratio_20 = float(volume.iloc[-1] / volume_ma20) if volume_ma20 else np.nan
        channel_upper = float(high.shift(1).tail(self.channel_window).max()) if high is not None else np.nan
        channel_lower = float(low.shift(1).tail(self.channel_window).min()) if low is not None else np.nan
        donchian_pos = (
            (latest_close - channel_lower) / (channel_upper - channel_lower)
            if pd.notna(channel_upper) and pd.notna(channel_lower) and channel_upper > channel_lower
            else np.nan
        )
        channel_width = (
            (channel_upper - channel_lower) / latest_close
            if pd.notna(channel_upper) and pd.notna(channel_lower) and latest_close > 0
            else np.nan
        )
        median_amount_20 = (
            float(amount.tail(20).median()) if amount is not None and amount.notna().any()
            else float((close * volume).tail(20).median())
        )
        breakout_confirmed = bool(pd.notna(channel_upper) and latest_close > channel_upper)
        liquidity_ok = bool(pd.notna(median_amount_20) and median_amount_20 >= self.min_median_amount_20d)
        stop_price = float(min(low.iloc[-1], latest_close * (1.0 - self.stop_pct))) if low is not None and pd.notna(low.iloc[-1]) else float(latest_close * (1.0 - self.stop_pct))

        momentum_score = 0.0
        if gain_1d >= self.min_gain:
            momentum_score += 40.0
        if gain_1d >= 0.099:
            momentum_score += 10.0
        if liquidity_ok:
            momentum_score += 15.0
        if pd.notna(ma20) and latest_close >= ma20:
            momentum_score += 10.0
        if breakout_confirmed:
            momentum_score += 15.0
        if pd.notna(donchian_pos) and donchian_pos >= 0.90:
            momentum_score += 10.0

        triggered = (
            gain_1d >= self.min_gain
            and liquidity_ok
            and momentum_score >= self.min_score
            and (breakout_confirmed or not self.require_breakout)
        )
        snapshot.update(
            {
                "setup_type": "limit_momentum" if triggered else "neutral",
                "setup_score": float(momentum_score),
                "momentum_score": float(momentum_score),
                "gain_1d": float(gain_1d),
                "latest_close": latest_close,
                "ma20": ma20,
                "donchian_upper": channel_upper,
                "donchian_lower": channel_lower,
                "donchian_pos": float(donchian_pos) if pd.notna(donchian_pos) else np.nan,
                "channel_width": float(channel_width) if pd.notna(channel_width) else np.nan,
                "distance_to_upper": float(latest_close / channel_upper - 1.0) if pd.notna(channel_upper) and channel_upper else np.nan,
                "breakout_confirmed": breakout_confirmed,
                "volume_ratio_20": volume_ratio_20,
                "median_amount_20": median_amount_20,
                "liquidity_ok": liquidity_ok,
                "stop_price": stop_price,
                "expected_holding_days": int(self.expected_holding_days),
                "recipe_scores": {"limit_momentum": float(momentum_score)},
            }
        )
        return self._to_result(snapshot)

    def _to_result(self, snapshot):
        return SignalRecipeResult(
            name=self.name,
            signal_type=snapshot["setup_type"],
            score=float(snapshot["setup_score"]),
            features=snapshot,
        )
