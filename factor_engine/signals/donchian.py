#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Donchian 通道突破与回踩信号 recipe。

通道定义（与 Donchian channel 标准定义一致）：

    upper_t = max(High[t-N .. t-1])
    lower_t = min(Low[t-N .. t-1])
    pos_t   = (Close_t - lower_t) / (upper_t - lower_t)

突破事件包含两种口径：

    close_break    : Close_t > upper_t * (1 + breakout_min_pct)   # 收盘确认突破
    intraday_break : High_t  > upper_t 且未收盘突破                 # 盘中上穿、收盘落回

当日的 ``donchian_breakout`` 是"次日回调买入"的入场信号日；突破后
``1..max_sessions_since_breakout`` 个交易日内收盘守在上轨附近且缩量，
即为 ``donchian_pullback`` 的加仓/确认状态。
"""

import numpy as np
import pandas as pd

from factor_engine.signals.base import SignalRecipe, SignalRecipeResult
from factor_engine.signals.registry import register_signal_recipe


def _empty_donchian_snapshot(window=20):
    return {
        "setup_type": "neutral",
        "setup_score": 0.0,
        "sideways_penalty": 0.0,
        "donchian_score": 0.0,
        "donchian_window": int(window),
        "donchian_upper": np.nan,
        "donchian_lower": np.nan,
        "donchian_mid": np.nan,
        "donchian_pos": np.nan,
        "channel_width": np.nan,
        "breakout_count_20": 0,
        "close_break_count_20": 0,
        "last_breakout_date": None,
        "sessions_since_breakout": np.nan,
        "fresh_breakout": False,
        "close_break_today": False,
        "intraday_break_today": False,
        "pullback_holding": False,
        "pullback_depth": np.nan,
        "volume_dryup": False,
        "volume_ratio_20": np.nan,
        "liquidity_ok": False,
        "median_turnover_20": np.nan,
        "latest_close": np.nan,
        "recipe_scores": {"donchian": 0.0, "sideways_penalty": 0.0},
    }


@register_signal_recipe("donchian_pullback")
class DonchianChannelRecipe(SignalRecipe):
    """识别 Donchian 通道突破日与突破后的缩量回踩。"""

    name = "donchian_pullback"

    def __init__(
        self,
        window=20,
        breakout_min_pct=0.005,
        tol_break=0.015,
        tol_low=0.03,
        tol_high=0.05,
        max_sessions_since_breakout=5,
        volume_dryup_ratio=0.70,
        volume_expansion_min=1.30,
        channel_width_max=0.35,
        min_median_amount_20d=10_000_000.0,
        liquidity_turnover_floor=1_000_000.0,
        min_score=60.0,
        **kwargs,
    ):
        self.window = max(2, int(window))
        self.breakout_min_pct = float(breakout_min_pct)
        self.tol_break = float(tol_break)
        self.tol_low = float(tol_low)
        self.tol_high = float(tol_high)
        self.max_sessions_since_breakout = max(1, int(max_sessions_since_breakout))
        self.volume_dryup_ratio = float(volume_dryup_ratio)
        self.volume_expansion_min = float(volume_expansion_min)
        self.channel_width_max = float(channel_width_max)
        self.min_median_amount_20d = float(min_median_amount_20d)
        self.liquidity_turnover_floor = float(liquidity_turnover_floor)
        self.min_score = float(min_score)
        self.extra_config = dict(kwargs)

    @staticmethod
    def _column(working, *names):
        for name in names:
            if name in working.columns:
                return pd.to_numeric(working[name], errors="coerce")
        return None

    def evaluate(self, data, context=None):
        snapshot = _empty_donchian_snapshot(self.window)
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
        if close is None or high is None or low is None or volume is None:
            return self._to_result(snapshot)
        if close.dropna().empty or len(close) < self.window + 2:
            return self._to_result(snapshot)

        upper = high.shift(1).rolling(self.window, min_periods=self.window).max()
        lower = low.shift(1).rolling(self.window, min_periods=self.window).min()
        if pd.isna(upper.iloc[-1]) or pd.isna(lower.iloc[-1]):
            return self._to_result(snapshot)

        close_break = close > upper * (1.0 + self.breakout_min_pct)
        intraday_break = (~close_break) & (high > upper)
        breakout = (close_break | intraday_break).fillna(False)

        latest_close = float(close.iloc[-1])
        latest_volume = float(volume.iloc[-1])
        upper_now = float(upper.iloc[-1])
        lower_now = float(lower.iloc[-1])

        breakout_count_20 = int(breakout.tail(20).sum())
        close_break_count_20 = int(close_break.fillna(False).tail(20).sum())
        breakout_positions = np.flatnonzero(breakout.to_numpy(dtype=bool))
        sessions_since = int(len(working) - 1 - int(breakout_positions[-1])) if breakout_positions.size else np.nan
        last_breakout_date = working.index[-1 - sessions_since] if breakout_positions.size else None

        channel_width = (upper_now - lower_now) / latest_close if latest_close > 0 else np.nan
        donchian_pos = (latest_close - lower_now) / (upper_now - lower_now) if upper_now > lower_now else np.nan
        distance_to_upper = latest_close / upper_now - 1.0 if upper_now else np.nan

        turnover_series = close * volume
        median_turnover_20 = float(turnover_series.tail(20).median()) if not turnover_series.tail(20).dropna().empty else np.nan
        if amount is not None and amount.notna().any():
            median_amount_20 = float(amount.tail(20).median())
        else:
            median_amount_20 = median_turnover_20
        volume_ma20 = float(volume.tail(20).mean()) if not volume.tail(20).dropna().empty else np.nan
        volume_ratio_20 = latest_volume / volume_ma20 if pd.notna(volume_ma20) and volume_ma20 else np.nan

        # 回踩判定只在突破之后的交易日成立：突破当日盘中必然先跌破上轨。
        pullback_holding = False
        pullback_depth = np.nan
        if pd.notna(sessions_since) and int(sessions_since) >= 1:
            pullback_holding = bool(
                latest_close >= upper_now * (1.0 - self.tol_low)
                and latest_close <= upper_now * (1.0 + self.tol_high)
                and float(low.iloc[-1]) >= upper_now * (1.0 - self.tol_break)
            )
            breakout_close = float(close.iloc[-1 - int(sessions_since)]) if len(close) > int(sessions_since) else np.nan
            if pd.notna(breakout_close) and breakout_close:
                pullback_depth = latest_close / breakout_close - 1.0

        volume_dryup = bool(pd.notna(volume_ratio_20) and volume_ratio_20 <= self.volume_dryup_ratio)
        volume_expansion = bool(pd.notna(volume_ratio_20) and volume_ratio_20 >= self.volume_expansion_min)
        liquidity_ok = bool(pd.notna(median_amount_20) and median_amount_20 >= self.min_median_amount_20d)

        donchian_score = 0.0
        if breakout_count_20 >= 1:
            donchian_score += 20.0
        if pd.notna(sessions_since) and int(sessions_since) <= self.max_sessions_since_breakout:
            donchian_score += 15.0
        if pullback_holding:
            donchian_score += 20.0
        if volume_dryup:
            donchian_score += 15.0
        if pd.notna(donchian_pos) and donchian_pos >= 0.60:
            donchian_score += 10.0
        if pd.notna(channel_width) and channel_width <= self.channel_width_max:
            donchian_score += 10.0
        if liquidity_ok:
            donchian_score += 10.0

        sideways_penalty = 0.0
        if breakout_count_20 == 0:
            sideways_penalty += 8.0
        if not (pd.notna(donchian_pos) and donchian_pos >= 0.60):
            sideways_penalty += 8.0
        if pd.notna(volume_ratio_20) and volume_ratio_20 < 1.05:
            sideways_penalty += 6.0

        fresh_breakout = bool(breakout.iloc[-1])
        if fresh_breakout:
            setup_type = "donchian_breakout" if (volume_expansion or float(close_break.iloc[-1])) else "neutral"
        elif pullback_holding and volume_dryup and donchian_score >= self.min_score:
            setup_type = "donchian_pullback"
        elif sideways_penalty >= 18.0:
            setup_type = "sideways"
        else:
            setup_type = "neutral"

        snapshot.update(
            {
                "setup_type": setup_type,
                "setup_score": float(donchian_score),
                "sideways_penalty": float(sideways_penalty),
                "donchian_score": float(donchian_score),
                "donchian_window": int(self.window),
                "donchian_upper": upper_now,
                "donchian_lower": lower_now,
                "donchian_mid": (upper_now + lower_now) / 2.0,
                "donchian_pos": float(donchian_pos) if pd.notna(donchian_pos) else np.nan,
                "channel_width": float(channel_width) if pd.notna(channel_width) else np.nan,
                "distance_to_upper": float(distance_to_upper) if pd.notna(distance_to_upper) else np.nan,
                "breakout_count_20": breakout_count_20,
                "close_break_count_20": close_break_count_20,
                "last_breakout_date": None if last_breakout_date is None else str(pd.Timestamp(last_breakout_date).date()),
                "sessions_since_breakout": sessions_since,
                "fresh_breakout": fresh_breakout,
                "close_break_today": bool(close_break.iloc[-1]),
                "intraday_break_today": bool(intraday_break.iloc[-1]),
                "pullback_holding": pullback_holding,
                "pullback_depth": float(pullback_depth) if pd.notna(pullback_depth) else np.nan,
                "volume_dryup": volume_dryup,
                "volume_expansion": volume_expansion,
                "volume_ratio_20": float(volume_ratio_20) if pd.notna(volume_ratio_20) else np.nan,
                "liquidity_ok": liquidity_ok,
                "median_turnover_20": median_turnover_20,
                "median_amount_20": median_amount_20,
                "latest_close": latest_close,
                "recipe_scores": {
                    "donchian": float(donchian_score),
                    "sideways_penalty": float(sideways_penalty),
                },
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
