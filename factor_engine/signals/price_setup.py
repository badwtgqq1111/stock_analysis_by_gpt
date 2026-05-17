#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""价量形态信号 recipe。"""

import numpy as np
import pandas as pd

from factor_engine.signals.base import SignalRecipe, SignalRecipeResult
from factor_engine.signals.registry import register_signal_recipe


def _empty_low_price_snapshot():
    return {
        "setup_type": "neutral",
        "setup_score": 0.0,
        "sideways_penalty": 0.0,
        "low_price_candidate": False,
        "liquidity_ok": False,
        "latest_turnover": np.nan,
        "median_turnover_20": np.nan,
        "distance_to_20d_high": np.nan,
        "distance_from_60d_low": np.nan,
        "volume_ratio_20": np.nan,
        "compression_ratio": np.nan,
        "return_5d": np.nan,
        "return_20d": np.nan,
        "return_60d": np.nan,
        "recipe_scores": {
            "pre_breakout": 0.0,
            "bottom_rebound": 0.0,
            "sideways_penalty": 0.0,
        },
    }


def _empty_range_breakout_snapshot():
    return {
        "setup_type": "neutral",
        "setup_score": 0.0,
        "sideways_penalty": 0.0,
        "range_breakout_score": 0.0,
        "breakout_above_20d_high": False,
        "volume_confirmed": False,
        "compression_confirmed": False,
        "liquidity_ok": False,
        "low_price_candidate": False,
        "latest_turnover": np.nan,
        "median_turnover_20": np.nan,
        "distance_to_20d_high": np.nan,
        "volume_ratio_20": np.nan,
        "compression_ratio": np.nan,
        "range20": np.nan,
        "range60": np.nan,
        "return_5d": np.nan,
        "return_20d": np.nan,
        "recipe_scores": {
            "range_breakout": 0.0,
            "sideways_penalty": 0.0,
        },
    }


def _empty_box_pullback_snapshot():
    return {
        "setup_type": "neutral",
        "setup_score": 0.0,
        "sideways_penalty": 0.0,
        "box_pullback_score": 0.0,
        "box_breakout_confirmed": False,
        "pullback_holding_box_top": False,
        "pullback_volume_dryup": False,
        "liquidity_ok": False,
        "low_price_candidate": False,
        "latest_turnover": np.nan,
        "median_turnover_20": np.nan,
        "box_top": np.nan,
        "box_bottom": np.nan,
        "box_height": np.nan,
        "distance_to_box_top": np.nan,
        "breakout_peak_return": np.nan,
        "pullback_depth": np.nan,
        "volume_ratio_20": np.nan,
        "pullback_volume_ratio": np.nan,
        "return_20d": np.nan,
        "recipe_scores": {
            "box_pullback": 0.0,
            "sideways_penalty": 0.0,
        },
    }


@register_signal_recipe("low_price_setup")
class LowPriceSetupRecipe(SignalRecipe):
    """识别低价股的突破前、底部反弹和横盘惩罚状态。"""

    name = "low_price_setup"

    def evaluate(self, data, context=None):
        snapshot = _empty_low_price_snapshot()
        if data is None or data.empty:
            return self._to_result(snapshot)

        working = data.copy().sort_index()
        if "Close" not in working.columns or "Volume" not in working.columns:
            return self._to_result(snapshot)

        close = pd.to_numeric(working["Close"], errors="coerce")
        volume = pd.to_numeric(working["Volume"], errors="coerce").fillna(0)
        if close.dropna().empty:
            return self._to_result(snapshot)

        latest_close = float(close.iloc[-1])
        latest_volume = float(volume.iloc[-1])
        latest_turnover = latest_close * latest_volume
        turnover_series = close * volume
        median_turnover_20 = float(turnover_series.tail(20).median()) if not turnover_series.tail(20).dropna().empty else np.nan

        returns = close.pct_change()
        return_5d = float(close.iloc[-1] / close.iloc[-6] - 1.0) if len(close) >= 6 and close.iloc[-6] else np.nan
        return_20d = float(close.iloc[-1] / close.iloc[-21] - 1.0) if len(close) >= 21 and close.iloc[-21] else np.nan
        return_60d = float(close.iloc[-1] / close.iloc[-61] - 1.0) if len(close) >= 61 and close.iloc[-61] else np.nan

        high20 = float(close.tail(20).max()) if not close.tail(20).dropna().empty else np.nan
        low20 = float(close.tail(20).min()) if not close.tail(20).dropna().empty else np.nan
        high60 = float(close.tail(60).max()) if not close.tail(60).dropna().empty else np.nan
        low60 = float(close.tail(60).min()) if not close.tail(60).dropna().empty else np.nan
        ma10 = float(close.tail(10).mean()) if not close.tail(10).dropna().empty else np.nan
        ma20 = float(close.tail(20).mean()) if not close.tail(20).dropna().empty else np.nan
        volume_ma20 = float(volume.tail(20).mean()) if not volume.tail(20).dropna().empty else np.nan

        distance_to_20d_high = latest_close / high20 - 1.0 if pd.notna(high20) and high20 else np.nan
        distance_from_60d_low = latest_close / low60 - 1.0 if pd.notna(low60) and low60 else np.nan
        volume_ratio_20 = latest_volume / volume_ma20 if pd.notna(volume_ma20) and volume_ma20 else np.nan

        volatility_20 = float(returns.tail(20).std(ddof=1)) if returns.tail(20).dropna().shape[0] >= 5 else np.nan
        volatility_60 = float(returns.tail(60).std(ddof=1)) if returns.tail(60).dropna().shape[0] >= 10 else np.nan
        compression_ratio = volatility_20 / volatility_60 if pd.notna(volatility_20) and pd.notna(volatility_60) and volatility_60 > 0 else np.nan

        range20 = (high20 - low20) / latest_close if pd.notna(high20) and pd.notna(low20) and latest_close > 0 else np.nan
        range60 = (high60 - low60) / latest_close if pd.notna(high60) and pd.notna(low60) and latest_close > 0 else np.nan

        low_price_candidate = 0.20 <= latest_close <= 8.0
        liquidity_ok = pd.notna(median_turnover_20) and median_turnover_20 >= 1_000_000.0

        pre_breakout_score = 0.0
        if low_price_candidate:
            pre_breakout_score += 16.0
        if liquidity_ok:
            pre_breakout_score += 14.0
        if pd.notna(distance_to_20d_high) and distance_to_20d_high >= -0.035:
            pre_breakout_score += 18.0
        if pd.notna(compression_ratio) and compression_ratio <= 0.85:
            pre_breakout_score += 16.0
        elif pd.notna(range20) and pd.notna(range60) and range20 <= range60 * 0.72:
            pre_breakout_score += 12.0
        if pd.notna(volume_ratio_20) and volume_ratio_20 >= 1.12:
            pre_breakout_score += 16.0
        if pd.notna(return_20d) and return_20d >= 0.04:
            pre_breakout_score += 10.0
        if pd.notna(ma20) and latest_close >= ma20:
            pre_breakout_score += 8.0

        bottom_rebound_score = 0.0
        if low_price_candidate:
            bottom_rebound_score += 16.0
        if liquidity_ok:
            bottom_rebound_score += 12.0
        if pd.notna(return_60d) and return_60d <= -0.18:
            bottom_rebound_score += 18.0
        if pd.notna(distance_from_60d_low) and 0.06 <= distance_from_60d_low <= 0.35:
            bottom_rebound_score += 16.0
        if pd.notna(volume_ratio_20) and volume_ratio_20 >= 1.15:
            bottom_rebound_score += 14.0
        if pd.notna(return_5d) and return_5d >= 0.04:
            bottom_rebound_score += 12.0
        if pd.notna(ma10) and latest_close >= ma10:
            bottom_rebound_score += 10.0
        if pd.notna(ma20) and latest_close >= ma20 * 0.97:
            bottom_rebound_score += 8.0

        sideways_penalty = 0.0
        if pd.notna(return_20d) and abs(return_20d) <= 0.06:
            sideways_penalty += 10.0
        if pd.notna(range20) and range20 <= 0.14:
            sideways_penalty += 8.0
        if pd.notna(volume_ratio_20) and volume_ratio_20 < 1.05:
            sideways_penalty += 8.0
        if pd.notna(distance_to_20d_high) and distance_to_20d_high < -0.05 and (pd.isna(distance_from_60d_low) or distance_from_60d_low < 0.08):
            sideways_penalty += 6.0

        rebound_context = pd.notna(return_60d) and return_60d <= -0.18
        if rebound_context and bottom_rebound_score >= max(52.0, pre_breakout_score - 4.0, sideways_penalty + 6.0):
            setup_type = "bottom_rebound"
            setup_score = bottom_rebound_score
        elif pre_breakout_score >= bottom_rebound_score and pre_breakout_score >= max(55.0, sideways_penalty + 8.0):
            setup_type = "pre_breakout"
            setup_score = pre_breakout_score
        elif bottom_rebound_score > pre_breakout_score and bottom_rebound_score >= max(55.0, sideways_penalty + 8.0):
            setup_type = "bottom_rebound"
            setup_score = bottom_rebound_score
        elif sideways_penalty >= 18.0:
            setup_type = "sideways"
            setup_score = max(pre_breakout_score, bottom_rebound_score)
        else:
            setup_type = "neutral"
            setup_score = max(pre_breakout_score, bottom_rebound_score)

        snapshot.update(
            {
                "setup_type": setup_type,
                "setup_score": float(setup_score),
                "sideways_penalty": float(sideways_penalty),
                "low_price_candidate": bool(low_price_candidate and liquidity_ok),
                "liquidity_ok": bool(liquidity_ok),
                "latest_turnover": latest_turnover,
                "median_turnover_20": median_turnover_20,
                "distance_to_20d_high": distance_to_20d_high,
                "distance_from_60d_low": distance_from_60d_low,
                "volume_ratio_20": volume_ratio_20,
                "compression_ratio": compression_ratio,
                "return_5d": return_5d,
                "return_20d": return_20d,
                "return_60d": return_60d,
                "recipe_scores": {
                    "pre_breakout": float(pre_breakout_score),
                    "bottom_rebound": float(bottom_rebound_score),
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


@register_signal_recipe("box_pullback")
class BoxPullbackRecipe(SignalRecipe):
    """识别突破箱体后的缩量回踩。"""

    name = "box_pullback"

    def evaluate(self, data, context=None):
        snapshot = _empty_box_pullback_snapshot()
        if data is None or data.empty:
            return self._to_result(snapshot)

        working = data.copy().sort_index()
        if "Close" not in working.columns or "Volume" not in working.columns:
            return self._to_result(snapshot)

        close = pd.to_numeric(working["Close"], errors="coerce")
        volume = pd.to_numeric(working["Volume"], errors="coerce").fillna(0)
        if close.dropna().empty or len(close) < 45:
            return self._to_result(snapshot)

        latest_close = float(close.iloc[-1])
        latest_volume = float(volume.iloc[-1])
        latest_turnover = latest_close * latest_volume
        turnover_series = close * volume
        median_turnover_20 = float(turnover_series.tail(20).median()) if not turnover_series.tail(20).dropna().empty else np.nan

        box_window = close.iloc[:-20].tail(40)
        breakout_window = close.iloc[-20:-5]
        pullback_window = close.iloc[-5:]
        box_top = float(box_window.max()) if not box_window.dropna().empty else np.nan
        box_bottom = float(box_window.min()) if not box_window.dropna().empty else np.nan
        breakout_peak = float(breakout_window.max()) if not breakout_window.dropna().empty else np.nan
        pullback_low = float(pullback_window.min()) if not pullback_window.dropna().empty else np.nan

        box_height = box_top / box_bottom - 1.0 if pd.notna(box_top) and pd.notna(box_bottom) and box_bottom else np.nan
        breakout_peak_return = breakout_peak / box_top - 1.0 if pd.notna(breakout_peak) and pd.notna(box_top) and box_top else np.nan
        distance_to_box_top = latest_close / box_top - 1.0 if pd.notna(box_top) and box_top else np.nan
        pullback_depth = latest_close / breakout_peak - 1.0 if pd.notna(breakout_peak) and breakout_peak else np.nan
        volume_ma20 = float(volume.tail(20).mean()) if not volume.tail(20).dropna().empty else np.nan
        breakout_volume_window = volume.iloc[-20:-5]
        breakout_volume = float(breakout_volume_window.median()) if not breakout_volume_window.dropna().empty else np.nan
        volume_ratio_20 = latest_volume / volume_ma20 if pd.notna(volume_ma20) and volume_ma20 else np.nan
        pullback_volume_ratio = latest_volume / breakout_volume if pd.notna(breakout_volume) and breakout_volume else np.nan
        return_20d = float(close.iloc[-1] / close.iloc[-21] - 1.0) if len(close) >= 21 and close.iloc[-21] else np.nan

        low_price_candidate = 0.20 <= latest_close <= 8.0
        liquidity_ok = pd.notna(median_turnover_20) and median_turnover_20 >= 1_000_000.0
        compact_box = pd.notna(box_height) and box_height <= 0.18
        box_breakout_confirmed = compact_box and pd.notna(breakout_peak_return) and breakout_peak_return >= 0.035
        pullback_holding_box_top = (
            pd.notna(distance_to_box_top)
            and distance_to_box_top >= -0.015
            and distance_to_box_top <= 0.045
            and pd.notna(pullback_low)
            and pd.notna(box_top)
            and pullback_low >= box_top * 0.985
        )
        pullback_volume_dryup = (
            (pd.notna(volume_ratio_20) and volume_ratio_20 <= 0.85)
            or (pd.notna(pullback_volume_ratio) and pullback_volume_ratio <= 0.70)
        )

        box_pullback_score = 0.0
        if low_price_candidate:
            box_pullback_score += 8.0
        if liquidity_ok:
            box_pullback_score += 12.0
        if compact_box:
            box_pullback_score += 14.0
        if box_breakout_confirmed:
            box_pullback_score += 20.0
        if pullback_holding_box_top:
            box_pullback_score += 22.0
        if pullback_volume_dryup:
            box_pullback_score += 16.0
        if pd.notna(pullback_depth) and -0.12 <= pullback_depth <= -0.015:
            box_pullback_score += 8.0
        if pd.notna(return_20d) and return_20d >= 0.02:
            box_pullback_score += 4.0

        sideways_penalty = 0.0
        if not box_breakout_confirmed:
            sideways_penalty += 8.0
        if not pullback_holding_box_top:
            sideways_penalty += 8.0
        if pd.notna(volume_ratio_20) and volume_ratio_20 > 1.15:
            sideways_penalty += 6.0

        if box_breakout_confirmed and pullback_holding_box_top and pullback_volume_dryup and box_pullback_score >= 60.0:
            setup_type = "box_pullback"
        elif sideways_penalty >= 18.0:
            setup_type = "sideways"
        else:
            setup_type = "neutral"

        snapshot.update(
            {
                "setup_type": setup_type,
                "setup_score": float(box_pullback_score),
                "sideways_penalty": float(sideways_penalty),
                "box_pullback_score": float(box_pullback_score),
                "box_breakout_confirmed": bool(box_breakout_confirmed),
                "pullback_holding_box_top": bool(pullback_holding_box_top),
                "pullback_volume_dryup": bool(pullback_volume_dryup),
                "liquidity_ok": bool(liquidity_ok),
                "low_price_candidate": bool(low_price_candidate and liquidity_ok),
                "latest_turnover": latest_turnover,
                "median_turnover_20": median_turnover_20,
                "box_top": box_top,
                "box_bottom": box_bottom,
                "box_height": box_height,
                "distance_to_box_top": distance_to_box_top,
                "breakout_peak_return": breakout_peak_return,
                "pullback_depth": pullback_depth,
                "volume_ratio_20": volume_ratio_20,
                "pullback_volume_ratio": pullback_volume_ratio,
                "return_20d": return_20d,
                "recipe_scores": {
                    "box_pullback": float(box_pullback_score),
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


@register_signal_recipe("range_breakout")
class RangeBreakoutRecipe(SignalRecipe):
    """识别横盘压缩后的放量突破。"""

    name = "range_breakout"

    def evaluate(self, data, context=None):
        snapshot = _empty_range_breakout_snapshot()
        if data is None or data.empty:
            return self._to_result(snapshot)

        working = data.copy().sort_index()
        if "Close" not in working.columns or "Volume" not in working.columns:
            return self._to_result(snapshot)

        close = pd.to_numeric(working["Close"], errors="coerce")
        volume = pd.to_numeric(working["Volume"], errors="coerce").fillna(0)
        if close.dropna().empty:
            return self._to_result(snapshot)

        latest_close = float(close.iloc[-1])
        latest_volume = float(volume.iloc[-1])
        latest_turnover = latest_close * latest_volume
        turnover_series = close * volume
        median_turnover_20 = float(turnover_series.tail(20).median()) if not turnover_series.tail(20).dropna().empty else np.nan

        previous_close = close.iloc[:-1]
        high20_previous = float(previous_close.tail(20).max()) if not previous_close.tail(20).dropna().empty else np.nan
        high20 = float(close.tail(20).max()) if not close.tail(20).dropna().empty else np.nan
        low20 = float(close.tail(20).min()) if not close.tail(20).dropna().empty else np.nan
        high60 = float(close.tail(60).max()) if not close.tail(60).dropna().empty else np.nan
        low60 = float(close.tail(60).min()) if not close.tail(60).dropna().empty else np.nan
        ma20 = float(close.tail(20).mean()) if not close.tail(20).dropna().empty else np.nan
        volume_ma20 = float(volume.tail(20).mean()) if not volume.tail(20).dropna().empty else np.nan
        previous_volume_ma20 = float(volume.iloc[:-1].tail(20).mean()) if not volume.iloc[:-1].tail(20).dropna().empty else np.nan

        returns = close.pct_change()
        volatility_20 = float(returns.tail(20).std(ddof=1)) if returns.tail(20).dropna().shape[0] >= 5 else np.nan
        volatility_60 = float(returns.tail(60).std(ddof=1)) if returns.tail(60).dropna().shape[0] >= 10 else np.nan
        compression_ratio = volatility_20 / volatility_60 if pd.notna(volatility_20) and pd.notna(volatility_60) and volatility_60 > 0 else np.nan

        range20 = (high20 - low20) / latest_close if pd.notna(high20) and pd.notna(low20) and latest_close > 0 else np.nan
        range60 = (high60 - low60) / latest_close if pd.notna(high60) and pd.notna(low60) and latest_close > 0 else np.nan
        distance_to_20d_high = latest_close / high20_previous - 1.0 if pd.notna(high20_previous) and high20_previous else np.nan
        volume_base = previous_volume_ma20 if pd.notna(previous_volume_ma20) and previous_volume_ma20 else volume_ma20
        volume_ratio_20 = latest_volume / volume_base if pd.notna(volume_base) and volume_base else np.nan
        return_5d = float(close.iloc[-1] / close.iloc[-6] - 1.0) if len(close) >= 6 and close.iloc[-6] else np.nan
        return_20d = float(close.iloc[-1] / close.iloc[-21] - 1.0) if len(close) >= 21 and close.iloc[-21] else np.nan

        low_price_candidate = 0.20 <= latest_close <= 8.0
        liquidity_ok = pd.notna(median_turnover_20) and median_turnover_20 >= 1_000_000.0
        breakout_above_20d_high = pd.notna(distance_to_20d_high) and distance_to_20d_high >= 0.005
        near_20d_high = pd.notna(distance_to_20d_high) and distance_to_20d_high >= -0.015
        volume_confirmed = pd.notna(volume_ratio_20) and volume_ratio_20 >= 1.30
        compression_confirmed = (
            (pd.notna(compression_ratio) and compression_ratio <= 0.82)
            or (pd.notna(range20) and pd.notna(range60) and range20 <= range60 * 0.70)
            or (pd.notna(range20) and range20 <= 0.12)
        )

        range_breakout_score = 0.0
        if low_price_candidate:
            range_breakout_score += 8.0
        if liquidity_ok:
            range_breakout_score += 12.0
        if breakout_above_20d_high:
            range_breakout_score += 24.0
        elif near_20d_high:
            range_breakout_score += 14.0
        if compression_confirmed:
            range_breakout_score += 18.0
        if volume_confirmed:
            range_breakout_score += 18.0
        if pd.notna(return_5d) and return_5d >= 0.025:
            range_breakout_score += 10.0
        if pd.notna(return_20d) and return_20d >= 0.02:
            range_breakout_score += 6.0
        if pd.notna(ma20) and latest_close >= ma20:
            range_breakout_score += 4.0

        sideways_penalty = 0.0
        if not breakout_above_20d_high:
            sideways_penalty += 8.0
        if pd.notna(volume_ratio_20) and volume_ratio_20 < 1.05:
            sideways_penalty += 8.0
        if pd.notna(return_20d) and abs(return_20d) <= 0.04:
            sideways_penalty += 6.0

        if breakout_above_20d_high and compression_confirmed and volume_confirmed and range_breakout_score >= 60.0:
            setup_type = "range_breakout"
        elif sideways_penalty >= 18.0:
            setup_type = "sideways"
        else:
            setup_type = "neutral"

        snapshot.update(
            {
                "setup_type": setup_type,
                "setup_score": float(range_breakout_score),
                "sideways_penalty": float(sideways_penalty),
                "range_breakout_score": float(range_breakout_score),
                "breakout_above_20d_high": bool(breakout_above_20d_high),
                "volume_confirmed": bool(volume_confirmed),
                "compression_confirmed": bool(compression_confirmed),
                "liquidity_ok": bool(liquidity_ok),
                "low_price_candidate": bool(low_price_candidate and liquidity_ok),
                "latest_turnover": latest_turnover,
                "median_turnover_20": median_turnover_20,
                "distance_to_20d_high": distance_to_20d_high,
                "volume_ratio_20": volume_ratio_20,
                "compression_ratio": compression_ratio,
                "range20": range20,
                "range60": range60,
                "return_5d": return_5d,
                "return_20d": return_20d,
                "recipe_scores": {
                    "range_breakout": float(range_breakout_score),
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


def summarize_low_price_setup(data):
    """返回兼容现有选股链路的低价形态快照。"""
    return LowPriceSetupRecipe().evaluate(data).to_dict()
