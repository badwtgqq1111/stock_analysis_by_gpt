#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""量能路径 / 波动率 / 盈亏比因子集的测试。"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from factor_engine.expressions.volume_volatility import (  # noqa: E402
    VOLUME_VOLATILITY_FEATURES,
    compute_volatility_features,
    compute_volume_regime_features,
)
from factor_engine.registry import create_factor_set, list_factor_sets  # noqa: E402


def _bars(periods: int = 260, seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end="2026-09-15", periods=periods)
    close = pd.Series(60 * np.exp(np.cumsum(rng.normal(0, 0.018, periods))), index=dates)
    high = close * (1 + np.abs(rng.normal(0, 0.01, periods)))
    low = close * (1 - np.abs(rng.normal(0, 0.01, periods)))
    open_ = close.shift(1).fillna(close) * (1 + rng.normal(0, 0.004, periods))
    amount = pd.Series(rng.lognormal(18.2, 0.45, periods), index=dates)
    turnover = (amount / 1e10).clip(lower=1e-5)     # free-float turnover rate proxy
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": amount / close, "amount": amount, "turnover": turnover})


def test_all_declared_features_are_produced():
    frame = _bars()
    out = create_factor_set("volume_volatility_hk").transform(frame)

    assert list(out.columns) == VOLUME_VOLATILITY_FEATURES
    assert len(out) == len(frame)
    # only warm-up rows may be missing; the last 100 rows must be complete
    assert float(out.tail(100).isna().mean().mean()) < 0.02


def test_features_do_not_look_ahead():
    """Recomputing on a truncated frame must not change earlier values."""
    frame = _bars()
    full = create_factor_set("volume_volatility_hk").transform(frame)
    truncated = frame.iloc[:-5]
    partial = create_factor_set("volume_volatility_hk").transform(truncated)

    overlap = partial.index
    pd.testing.assert_frame_equal(full.loc[overlap].round(10), partial.round(10))


def test_dryup_then_expansion_is_detected():
    frame = _bars(periods=60, seed=3)
    frame.loc[frame.index[-6:-1], "amount"] = frame["amount"].median() * 0.3   # extreme dry-up
    frame.loc[frame.index[-1], "amount"] = frame["amount"].median() * 2.5      # first expansion
    regime = compute_volume_regime_features(frame)

    assert float(regime["vr_dryup_depth_5"].iloc[-1]) <= 0.7
    assert float(regime["vr_expansion_first_5"].iloc[-1]) == 1.0
    assert float(regime["vr_expansion_ratio_5"].iloc[-1]) > 1.5


def test_volatility_and_payoff_measures_are_sane():
    frame = _bars()
    out = compute_volatility_features(frame)
    tail = out.tail(50)

    for column in ("vol_cc_20", "vol_parkinson_20", "vol_garman_klass_20", "vol_yang_zhang_20"):
        assert tail[column].between(0.0, 3.0).all(), column
    assert (tail["vol_max_drawdown_60"] <= 0.0).all()
    assert (tail["payoff_reward_risk_20"] >= 0.0).all()
    assert tail["vol_atr_pct_14"].between(0.0, 1.0).all()


def test_factor_set_is_registered_and_part_of_the_production_bundle():
    assert "volume_volatility_hk" in list_factor_sets()
    frame = _bars()
    produced = create_factor_set("volume_volatility_hk").transform(frame)
    assert set(VOLUME_VOLATILITY_FEATURES).issubset(set(produced.columns))


def test_worker_style_frame_keeps_a_datetime_index():
    """The factor worker passes a RangeIndex plus a trade_date column."""
    frame = _bars(periods=300).reset_index().rename(columns={"index": "trade_date"})
    frame = frame.reset_index(drop=True)
    assert not isinstance(frame.index, pd.DatetimeIndex)

    out = compute_volume_regime_features(frame).join(compute_volatility_features(frame))

    assert isinstance(out.index, pd.DatetimeIndex)
    assert len(out) == len(frame)
    # a concat with other components must align on dates, not on integer labels
    other = pd.DataFrame({"x": np.arange(len(frame))}, index=pd.DatetimeIndex(
        pd.to_datetime(frame["trade_date"])))
    combined = pd.concat([other, out], axis=1)
    assert isinstance(combined.index, pd.DatetimeIndex)
    assert len(combined) == len(frame)
    # values must survive the index change: computing on a RangeIndex and then
    # labelling with dates produced silently all-NaN columns before this guard.
    assert float(out.tail(50).notna().mean().mean()) > 0.9
    assert float(out["vr_amount_ratio_20"].tail(1).iloc[0]) > 0


def test_turnover_features_react_to_a_volume_surge():
    from factor_engine.expressions.volume_volatility import compute_turnover_features

    frame = _bars(periods=120, seed=5)
    turnover = (frame["amount"] / 1e9).clip(lower=1e-4)
    frame["turnover"] = turnover
    frame.loc[frame.index[-6:-1], "turnover"] = float(turnover.tail(40).min()) * 0.5   # 缩量
    frame.loc[frame.index[-1], "turnover"] = float(turnover.tail(40).max()) * 3.0      # 爆量
    features = compute_turnover_features(frame)

    assert float(features["tr_true"].iloc[-1]) > float(features["tr_true"].iloc[-2])
    assert float(features["tr_zscore_20"].iloc[-1]) > 1.0
    assert float(features["tr_expansion_first_5"].iloc[-1]) == 1.0
    assert float(features["tr_dryup_days_10"].iloc[-1]) >= 1.0
    assert float(features["tr_percentile_252"].iloc[-1]) > 0.8


def test_turnover_features_degrade_gracefully_without_the_column():
    from factor_engine.expressions.volume_volatility import compute_turnover_features

    frame = _bars(periods=80).drop(columns=["turnover"])
    assert compute_turnover_features(frame).empty
