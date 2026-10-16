"""Causality and fitting checks for the rolling model comparison."""

import numpy as np
import pandas as pd
import pytest

from factor_engine.portfolio.volatility_calibration import _horizon_variance, fit_student_garch, rolling_volatility_comparison


def test_fit_garch_and_gjr_positive_stationary():
    values = np.random.default_rng(42).standard_t(6, 160) * .012
    for asym in (False, True):
        result = fit_student_garch(values, asymmetric=asym)
        assert result["variance_1d"] > 0
        assert result["persistence"] < .995
        assert result["nu"] > 2


def test_rolling_forecasts_use_no_future_returns():
    rng = np.random.default_rng(4)
    values = rng.normal(0, .01, 145)
    dates = pd.bdate_range("2025-01-01", periods=len(values))
    original = pd.Series(values, index=dates)
    altered = original.copy()
    altered.iloc[-1] = -.2
    a, _ = rolling_volatility_comparison(original, window=120, refit_every=20, horizons=(1,))
    b, _ = rolling_volatility_comparison(altered, window=120, refit_every=20, horizons=(1,))
    keys = ["decision_date", "horizon", "model", "forecast_variance"]
    pd.testing.assert_frame_equal(a[keys], b[keys])
    assert a.iloc[-1].realized_variance != b.iloc[-1].realized_variance


def test_reject_missing_returns():
    values = pd.Series([.01] * 150, index=pd.bdate_range("2025-01-01", periods=150))
    values.iloc[4] = np.nan
    with pytest.raises(ValueError):
        rolling_volatility_comparison(values)


def test_multistep_variance_mean_reverts_and_is_in_decimal_units():
    fit = {"parameters": np.array([.02, .05, .90, 0, 8]), "persistence": .95, "variance_1d": .0009}
    long_run = .02 / .05 / 10000
    assert _horizon_variance(fit, 1) == pytest.approx(.0009)
    assert _horizon_variance(fit, 5) == pytest.approx(np.mean([long_run + .95 ** k * (.0009 - long_run) for k in range(5)]))
    assert _horizon_variance(fit, 20) < _horizon_variance(fit, 5)


def test_forecasts_and_refit_dates_are_prefix_invariant_across_refits():
    values = pd.Series(np.random.default_rng(15).normal(0, .02, 175),
                       index=pd.bdate_range("2025-01-01", periods=175))
    full, _ = rolling_volatility_comparison(values, window=120, refit_every=20)
    prefix, _ = rolling_volatility_comparison(values.iloc[:165], window=120, refit_every=20)
    cols = ["decision_date", "horizon", "model", "forecast_variance", "nu", "persistence", "refit_date", "training_start"]
    same = full[full.decision_date.isin(prefix.decision_date.unique())]
    pd.testing.assert_frame_equal(same[cols].reset_index(drop=True), prefix[cols].reset_index(drop=True))
    assert (full.refit_date <= full.decision_date).all()
