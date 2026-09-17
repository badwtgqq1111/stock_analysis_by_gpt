#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""量能路径与波动率因子集。

两个来源：

* 量能路径（volume regime）—— 现有因子只有"当日量比"这类点值，没有"缩量持续了多久、
  缩到什么程度、是否已经出现首次放量"的路径信息。2026-09-16 的复盘显示，
  300671.SZ 在 09-15 处于"持续缩量"（量比 0.52~0.78），次日放量 +14.74%，
  而模型只有当日量比可用。这里把路径显式参数化。
* 波动率与盈亏比 —— OHLCV 多口径波动率（收盘/极差/跳空/上下行分解/波动率的波动）
  以及"上行空间 vs 下行空间"的盈亏比代理，用于选股时的风险刻画与 PK 阶段的
  风险预算（波动目标、单名风险贡献上限）。

所有特征只用当日及之前的 bar，不做任何前视。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from factor_engine.base import BaseFactorSet, FactorSetMetadata
from factor_engine.registry import register_factor_set


VOLUME_VOLATILITY_FEATURES = [
    # ---- volume path ----
    "vr_amount_ratio_20",
    "vr_dryup_depth_5",
    "vr_dryup_depth_10",
    "vr_dryup_days_10",
    "vr_expansion_first_5",
    "vr_expansion_ratio_5",
    "vr_zscore_60",
    "vr_amount_trend_5_20",
    "vr_amount_trend_20_60",
    "vr_volatility_20",
    "vr_obv_slope_20",
    "vr_amount_price_corr_20",
    # ---- volatility (OHLCV) ----
    "vol_cc_5",
    "vol_cc_20",
    "vol_cc_60",
    "vol_term_5_60",
    "vol_parkinson_20",
    "vol_garman_klass_20",
    "vol_rogers_satchell_20",
    "vol_yang_zhang_20",
    "vol_atr_pct_14",
    "vol_downside_20",
    "vol_updown_ratio_20",
    "vol_of_vol_20",
    "vol_gap_20",
    "vol_intraday_share_20",
    "vol_max_drawdown_60",
    "vol_ret_skew_20",
    "vol_ret_kurt_20",
    # ---- payoff / reward-risk ----
    "payoff_updown_mean_ratio_20",
    "payoff_reward_risk_20",
    "payoff_target_room_atr_20",
    "payoff_stop_room_atr_20",
    # ---- true turnover rate (free-float basis) ----
    "tr_true",
    "tr_zscore_20",
    "tr_zscore_60",
    "tr_rel_ma20",
    "tr_accel_5",
    "tr_dryup_days_10",
    "tr_expansion_first_5",
    "tr_volatility_20",
    "tr_price_corr_20",
    "tr_percentile_252",
    "tr_down_day_share_20",
    "tr_implied_float_value",
]

TRADING_DAYS = 252.0

def _frame_index(frame: pd.DataFrame) -> pd.Index:
    """Date index for the returned features.

    The factor worker hands components a frame with a RangeIndex and a
    ``trade_date`` column, while research callers pass a DatetimeIndex.  Using
    ``frame.index`` directly in the first case emits integer labels that collide
    with other components' date index and break ``pd.concat`` alignment (observed
    as ``DateParseError: day is out of range for month: 0`` during the retrain).
    """
    if "trade_date" in frame.columns:
        dates = pd.to_datetime(frame["trade_date"], errors="coerce")
        if dates.notna().any():
            return pd.DatetimeIndex(dates)
    if isinstance(frame.index, pd.DatetimeIndex):
        return frame.index
    return pd.RangeIndex(len(frame))



def _numeric(frame: pd.DataFrame, *names: str) -> pd.Series | None:
    for name in names:
        if name in frame.columns:
            series = pd.to_numeric(frame[name], errors="coerce")
            if series.notna().any():
                return series.astype(float)
    return None


def _safe_divide(numerator, denominator, fill=np.nan):
    result = numerator / denominator.replace(0.0, np.nan)
    return result.replace([np.inf, -np.inf], np.nan).fillna(fill) if fill == fill else result.replace(
        [np.inf, -np.inf], np.nan)


def compute_volume_regime_features(frame: pd.DataFrame) -> pd.DataFrame:
    """量能路径特征：缩量深度/持续天数/首次放量/量能趋势。"""
    close = _numeric(frame, "close", "Close")
    volume = _numeric(frame, "volume", "Volume")
    amount = _numeric(frame, "amount", "Amount")
    if close is None:
        return pd.DataFrame()
    turnover = amount if amount is not None else volume
    if turnover is None:
        return pd.DataFrame()
    turnover = turnover.astype(float)
    # Attach the date index *positionally* before any arithmetic: the worker
    # hands us a RangeIndex, and computing on it while returning a DatetimeIndex
    # would align label-wise and silently produce all-NaN columns.
    date_index = _frame_index(frame)
    close.index = date_index
    turnover.index = date_index
    if volume is not None:
        volume.index = date_index
    columns: dict[str, pd.Series] = {}

    median20 = turnover.rolling(20, min_periods=10).median()
    ratio = _safe_divide(turnover, median20)
    columns["vr_amount_ratio_20"] = ratio
    columns["vr_dryup_depth_5"] = ratio.rolling(5, min_periods=3).min()
    columns["vr_dryup_depth_10"] = ratio.rolling(10, min_periods=5).min()
    columns["vr_dryup_days_10"] = (ratio <= 0.7).astype(float).rolling(10, min_periods=5).sum()
    dryup5 = columns["vr_dryup_depth_5"]
    columns["vr_expansion_first_5"] = ((ratio >= 1.5) & (dryup5 <= 0.7)).astype(float)
    columns["vr_expansion_ratio_5"] = _safe_divide(ratio, dryup5)
    mean60 = turnover.rolling(60, min_periods=30).mean()
    std60 = turnover.rolling(60, min_periods=30).std()
    columns["vr_zscore_60"] = _safe_divide(turnover - mean60, std60, fill=0.0)
    mean5 = turnover.rolling(5, min_periods=3).mean()
    mean20 = turnover.rolling(20, min_periods=10).mean()
    columns["vr_amount_trend_5_20"] = _safe_divide(mean5, mean20) - 1.0
    columns["vr_amount_trend_20_60"] = _safe_divide(mean20, mean60) - 1.0
    columns["vr_volatility_20"] = ratio.rolling(20, min_periods=10).std()

    # OBV slope: signed volume accumulated, normalised by its own scale
    direction = np.sign(close.diff()).fillna(0.0)
    obv = (direction * turnover).cumsum()
    obv_scale = turnover.rolling(20, min_periods=10).sum().replace(0.0, np.nan)
    columns["vr_obv_slope_20"] = _safe_divide(obv.diff(20), obv_scale)

    log_turnover_change = np.log(turnover.replace(0.0, np.nan)).diff()
    columns["vr_amount_price_corr_20"] = close.pct_change().rolling(20, min_periods=10).corr(log_turnover_change)
    return pd.DataFrame(columns, index=date_index)


def compute_volatility_features(frame: pd.DataFrame) -> pd.DataFrame:
    """OHLCV 多口径波动率 + 盈亏比代理（全部为当日及之前信息）。"""
    close = _numeric(frame, "close", "Close")
    high = _numeric(frame, "high", "High")
    low = _numeric(frame, "low", "Low")
    open_ = _numeric(frame, "open", "Open")
    if close is None:
        return pd.DataFrame()
    high = high if high is not None else close
    low = low if low is not None else close
    open_ = open_ if open_ is not None else close
    date_index = _frame_index(frame)
    for series in (close, high, low, open_):
        series.index = date_index
    ret = close.pct_change()
    log_hl = np.log(_safe_divide(high, low))
    columns: dict[str, pd.Series] = {}

    for window in (5, 20, 60):
        columns[f"vol_cc_{window}"] = ret.rolling(window, min_periods=max(3, window // 3)).std() * np.sqrt(TRADING_DAYS)
    columns["vol_term_5_60"] = _safe_divide(columns["vol_cc_5"], columns["vol_cc_60"]) - 1.0

    parkinson = (log_hl ** 2).rolling(20, min_periods=10).mean() / (4.0 * np.log(2.0))
    columns["vol_parkinson_20"] = np.sqrt(parkinson.clip(lower=0.0)) * np.sqrt(TRADING_DAYS)
    gk = (0.5 * log_hl ** 2 - (2.0 * np.log(2.0) - 1.0) * (np.log(_safe_divide(close, open_)) ** 2))
    columns["vol_garman_klass_20"] = np.sqrt(gk.rolling(20, min_periods=10).mean().clip(lower=0.0)) * np.sqrt(TRADING_DAYS)
    rs = (np.log(_safe_divide(high, close)) * np.log(_safe_divide(high, open_))
          + np.log(_safe_divide(low, close)) * np.log(_safe_divide(low, open_)))
    columns["vol_rogers_satchell_20"] = np.sqrt(rs.rolling(20, min_periods=10).mean().clip(lower=0.0)) * np.sqrt(TRADING_DAYS)

    overnight = np.log(_safe_divide(open_, close.shift(1)))
    open_close = np.log(_safe_divide(close, open_))
    var_o = overnight.rolling(20, min_periods=10).var()
    var_c = open_close.rolling(20, min_periods=10).var()
    cov_oc = overnight.rolling(20, min_periods=10).cov(open_close)
    columns["vol_yang_zhang_20"] = np.sqrt((var_o + var_c + 2.0 * cov_oc.clip(upper=0.0)).clip(lower=0.0)) * np.sqrt(TRADING_DAYS)

    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr14 = tr.rolling(14, min_periods=7).mean()
    columns["vol_atr_pct_14"] = _safe_divide(atr14, close)

    downside = ret.where(ret < 0.0)
    upside = ret.where(ret > 0.0)
    # min_periods=5: a trending name can have fewer than 8 down days in 20 sessions,
    # and dropping the row would make the risk features unusable exactly when the
    # trend is strongest.
    down_vol = downside.rolling(20, min_periods=5).std() * np.sqrt(TRADING_DAYS)
    up_vol = upside.rolling(20, min_periods=5).std() * np.sqrt(TRADING_DAYS)
    columns["vol_downside_20"] = down_vol
    columns["vol_updown_ratio_20"] = _safe_divide(up_vol, down_vol).clip(upper=10.0)
    columns["vol_of_vol_20"] = _safe_divide(ret.rolling(5, min_periods=3).std().rolling(20, min_periods=10).std(),
                                            ret.rolling(20, min_periods=10).std())
    columns["vol_gap_20"] = overnight.rolling(20, min_periods=10).std() * np.sqrt(TRADING_DAYS)
    columns["vol_intraday_share_20"] = _safe_divide(var_c, var_c + var_o)
    running_max = close.cummax()
    columns["vol_max_drawdown_60"] = (close / running_max - 1.0).rolling(60, min_periods=20).min()
    columns["vol_ret_skew_20"] = ret.rolling(20, min_periods=10).skew()
    columns["vol_ret_kurt_20"] = ret.rolling(20, min_periods=10).kurt()

    mean_up = upside.rolling(20, min_periods=5).mean()
    mean_down = downside.rolling(20, min_periods=5).mean().abs()
    columns["payoff_updown_mean_ratio_20"] = _safe_divide(mean_up, mean_down).clip(upper=10.0)
    high20 = high.rolling(20, min_periods=10).max()
    low20 = low.rolling(20, min_periods=10).min()
    columns["payoff_reward_risk_20"] = _safe_divide(high20 - close, (close - low20).replace(0.0, np.nan)).clip(0.0, 20.0)
    columns["payoff_target_room_atr_20"] = _safe_divide(high20 - close, atr14)
    columns["payoff_stop_room_atr_20"] = _safe_divide(close - low20, atr14)
    return pd.DataFrame(columns, index=date_index)


def compute_turnover_features(frame: pd.DataFrame) -> pd.DataFrame:
    """真实换手率（流通口径）及其 Z 分、状态与容量代理。

    数据来源：日线 ``turnover`` 字段（腾讯口径的流通换手率，小数形式，
    例如 0.0033 = 0.33%）。它和"成交额 / 20 日均额"不同：换手率剔除了价格
    变动，因此跨股票可比，也更接近"真实筹码交换强度"。

    指标含义：

    * ``tr_true``      当日真实换手率（保留小数口径）
    * ``tr_zscore_*``  相对**自身**历史均值/标准差的 Z（换手率 Z）
    * ``tr_rel_ma20``  相对自身 20 日均值的偏离比例
    * ``tr_accel_5``   换手率 Z 的 5 日变化（放量加速度）
    * ``tr_dryup_days_10`` / ``tr_expansion_first_5``  缩量天数与"缩量后首次放量"
    * ``tr_volatility_20``  换手率的波动（投机度）
    * ``tr_price_corr_20``  换手率变化与收益的相关（量价配合度）
    * ``tr_percentile_252`` 当日换手率在自身近一年的分位（长期极值位置）
    * ``tr_implied_float_value`` 隐含流通市值 = 成交额 / 换手率（容量与规模代理）
    * ``tr_down_day_share_20``  近 20 日下跌日的换手占比（放量下跌 = 派发信号）
    """
    turnover = _numeric(frame, "turnover", "Turnover")
    if turnover is None:
        return pd.DataFrame()
    close = _numeric(frame, "close", "Close")
    amount = _numeric(frame, "amount", "Amount")
    date_index = _frame_index(frame)
    turnover = turnover.astype(float)
    turnover.index = date_index
    turnover = turnover.where(turnover > 0.0)
    if close is not None:
        close.index = date_index
    if amount is not None:
        amount.index = date_index
    columns: dict[str, pd.Series] = {}
    columns["tr_true"] = turnover

    mean20 = turnover.rolling(20, min_periods=10).mean()
    std20 = turnover.rolling(20, min_periods=10).std()
    mean60 = turnover.rolling(60, min_periods=30).mean()
    std60 = turnover.rolling(60, min_periods=30).std()
    columns["tr_zscore_20"] = _safe_divide(turnover - mean20, std20, fill=0.0)
    columns["tr_zscore_60"] = _safe_divide(turnover - mean60, std60, fill=0.0)
    columns["tr_rel_ma20"] = _safe_divide(turnover, mean20) - 1.0
    columns["tr_accel_5"] = columns["tr_zscore_20"] - columns["tr_zscore_20"].shift(5)

    ratio = _safe_divide(turnover, mean20)
    columns["tr_dryup_days_10"] = (ratio <= 0.7).astype(float).rolling(10, min_periods=5).sum()
    dryup5 = ratio.rolling(5, min_periods=3).min()
    columns["tr_expansion_first_5"] = ((ratio >= 1.5) & (dryup5 <= 0.7)).astype(float)
    columns["tr_volatility_20"] = _safe_divide(turnover.rolling(20, min_periods=10).std(), mean20)

    if close is not None:
        returns = close.pct_change()
        columns["tr_price_corr_20"] = returns.rolling(20, min_periods=10).corr(turnover.pct_change())
        columns["tr_percentile_252"] = turnover.rolling(252, min_periods=60).rank(pct=True)
        down_days = (returns < 0.0).astype(float)
        down_turnover = (down_days * turnover).rolling(20, min_periods=10).sum()
        all_turnover = turnover.rolling(20, min_periods=10).sum()
        columns["tr_down_day_share_20"] = _safe_divide(down_turnover, all_turnover)
    else:
        for name in ("tr_price_corr_20", "tr_percentile_252", "tr_down_day_share_20"):
            columns[name] = pd.Series(np.nan, index=date_index)

    if amount is not None:
        columns["tr_implied_float_value"] = np.log(
            _safe_divide(amount, turnover).clip(lower=1e7)
        )
    else:
        columns["tr_implied_float_value"] = pd.Series(np.nan, index=date_index)
    return pd.DataFrame(columns, index=date_index)


@register_factor_set("volume_volatility_hk")
class VolumeVolatilityFactorSet(BaseFactorSet):
    """量能路径 + 波动率 + 盈亏比因子集。"""

    name = "volume_volatility_hk"
    description = "Volume-path regime, OHLCV volatility measures and reward/risk payoff proxies"
    version = "0.1.0"

    def transform(self, frame, context=None):
        if frame is None or len(frame) == 0:
            return pd.DataFrame()
        volume_features = compute_volume_regime_features(frame)
        volatility_features = compute_volatility_features(frame)
        turnover_features = compute_turnover_features(frame)
        frames = [part for part in (volume_features, volatility_features, turnover_features) if not part.empty]
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, axis=1)

    def metadata(self):
        return FactorSetMetadata(
            name=self.name,
            description=self.description,
            version=self.version,
            assumptions=(
                "量比口径为成交额 / 近 20 日成交额中位数；缺成交额时退化为成交量。",
                "波动率含收盘、极差(Parkinson)、Garman-Klass、Rogers-Satchell、Yang-Zhang 五种口径。",
                "盈亏比代理为 20 日上沿空间 / 下沿空间（并用 ATR14 归一）。",
                "所有窗口只使用当日及之前的 bar。",
            ),
            extra={"feature_names": list(VOLUME_VOLATILITY_FEATURES), "exactness": "native",
                   "input_fields": ("close", "high", "low", "open", "volume", "amount", "turnover"),
                   "manifest": [
                {"factor_id": name, "factor_set": self.name, "family": "volume_volatility",
                 "source": "OHLCV", "status": "implemented"}
                for name in VOLUME_VOLATILITY_FEATURES
            ]},
        )
