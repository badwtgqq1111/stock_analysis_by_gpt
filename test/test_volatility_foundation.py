"""Regression locks for point-in-time volatility and regime calculations."""

import numpy as np
import pandas as pd
import pytest

from factor_engine.expressions.volume_volatility import compute_volatility_features
from factor_engine.ml.regime import _apply_hysteresis


def test_yang_zhang_includes_intraday_rogers_satchell_range():
    frame = pd.DataFrame(
        {"open": [100.0] * 40, "high": [102.0] * 40,
         "low": [98.0] * 40, "close": [100.0] * 40},
        index=pd.bdate_range("2026-01-01", periods=40),
    )
    actual = compute_volatility_features(frame)["vol_yang_zhang_20"]
    rs = np.log(102.0 / 100.0) * np.log(102.0 / 100.0) + np.log(98.0 / 100.0) * np.log(98.0 / 100.0)
    k = 0.34 / (1.34 + (20 + 1) / (20 - 1))
    expected = np.sqrt((1.0 - k) * rs * 252.0)
    assert actual.iloc[-1] == pytest.approx(expected)
    assert actual.iloc[-1] > 0.0
    assert np.isnan(actual.iloc[9])  # only nine overnight observations
    assert actual.iloc[10] > 0.0
    pd.testing.assert_series_equal(
        actual.iloc[:30],
        compute_volatility_features(frame.iloc[:30])["vol_yang_zhang_20"],
    )


def test_hysteresis_never_rewrites_past_and_changes_on_confirmation_day():
    labels = pd.Series(["sideways"] * 3 + ["bull"] * 3 + ["bear"] * 3)
    full = _apply_hysteresis(labels, hysteresis_days=3)
    assert full.tolist() == ["insufficient", "insufficient", "sideways",
                             "sideways", "sideways", "bull", "bull", "bull", "bear"]
    for length in range(1, len(labels) + 1):
        pd.testing.assert_series_equal(
            _apply_hysteresis(labels.iloc[:length], hysteresis_days=3),
            full.iloc[:length],
        )


def test_hysteresis_missing_data_resets_candidate_without_backfill():
    labels = pd.Series(["sideways", "sideways", "insufficient", "sideways", "sideways", "sideways"])
    assert _apply_hysteresis(labels, hysteresis_days=3).tolist() == [
        "insufficient", "insufficient", "insufficient", "insufficient", "insufficient", "sideways",
    ]


@pytest.mark.parametrize("length", [11, 15, 21, 45])
def test_yang_zhang_matches_direct_reference_with_variable_prices(length):
    rng = np.random.default_rng(19)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.02, length)))
    open_ = close * np.exp(rng.normal(0, 0.01, length))
    high = np.maximum(close, open_) * 1.02
    low = np.minimum(close, open_) * 0.98
    frame = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close},
                         index=pd.bdate_range("2026-01-01", periods=length))
    # Use a scalar, trailing-slice reference independent of pandas rolling.
    start = max(1, length - 20)
    o = np.log(open_[start:] / close[start - 1:-1])
    c = np.log(close[start:] / open_[start:])
    rs = (np.log(high[start:] / close[start:]) * np.log(high[start:] / open_[start:])
          + np.log(low[start:] / close[start:]) * np.log(low[start:] / open_[start:]))
    n = len(o)
    k = 0.34 / (1.34 + (n + 1) / (n - 1))
    expected = np.sqrt((o.var(ddof=1) + k * c.var(ddof=1) + (1 - k) * rs.mean()) * 252)
    assert compute_volatility_features(frame).iloc[-1]["vol_yang_zhang_20"] == pytest.approx(expected)


def test_hysteresis_interrupted_candidate_and_missing_active_state():
    labels = pd.Series(["bull"] * 3 + ["bear", "bull", "bear", "bear", "insufficient", "bull", "bull", "bull"])
    assert _apply_hysteresis(labels, hysteresis_days=3).tolist() == [
        "insufficient", "insufficient", "bull", "bull", "bull", "bull", "bull",
        "insufficient", "insufficient", "insufficient", "bull",
    ]
    pd.testing.assert_series_equal(_apply_hysteresis(labels, hysteresis_days=1), labels)


def test_market_regime_complete_outputs_are_prefix_invariant():
    from factor_engine.ml.regime import build_market_regime

    dates = pd.bdate_range("2026-01-01", periods=45)
    frame = pd.DataFrame([
        {"stock_code": code, "trade_date": date, "close": 100 * (1.03 ** min(i, 25)) * (0.96 ** max(i - 25, 0))}
        for code in ("A", "B") for i, date in enumerate(dates)
    ])
    kwargs = dict(min_stocks=2, trend_window=5, breadth_window=3, volatility_window=3, hysteresis_days=3)
    full = build_market_regime(frame, **kwargs)
    for date in dates:
        partial = build_market_regime(frame[frame.trade_date <= date], **kwargs)
        pd.testing.assert_frame_equal(full[full.trade_date <= date], partial)
    assert set(full["hysteresis_version"]) == {"causal.v2"}
