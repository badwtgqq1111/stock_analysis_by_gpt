"""EWMA analytical reference, causality and data-quality regressions."""

import numpy as np
import pandas as pd
import pytest

from factor_engine.portfolio.volatility import forecast_ewma_volatility


def _returns(values):
    return pd.Series(values, index=pd.bdate_range("2026-01-01", periods=len(values)))


def test_ewma_reference_update_and_horizon_units():
    result = forecast_ewma_volatility(_returns([0.02, -0.02, -0.03]), min_periods=2)
    assert result.iloc[0]["status"] == "insufficient"
    assert result.iloc[1]["forecast_variance_1d"] == pytest.approx(0.0004)
    assert result.iloc[2]["forecast_variance_1d"] == pytest.approx(0.000430)
    for h in (1, 5, 20):
        assert result.iloc[2][f"forecast_annual_vol_{h}d"] == pytest.approx(np.sqrt(0.000430 * 252))
        assert result.iloc[2][f"forecast_horizon_vol_{h}d"] == pytest.approx(np.sqrt(0.000430 * h))


def test_ewma_every_prefix_is_invariant_and_current_shock_only_affects_current_forecast():
    returns = _returns(np.random.default_rng(7).normal(0, 0.02, 60))
    full = forecast_ewma_volatility(returns)
    for length in range(1, len(returns) + 1):
        pd.testing.assert_frame_equal(full.iloc[:length], forecast_ewma_volatility(returns.iloc[:length]))
    shock = returns.copy()
    shock.iloc[-1] = -0.2
    updated = forecast_ewma_volatility(shock)
    pd.testing.assert_frame_equal(full.iloc[:-1], updated.iloc[:-1])
    assert updated.iloc[-1]["forecast_variance_1d"] > full.iloc[-1]["forecast_variance_1d"]


def test_ewma_missing_session_resets_and_zero_risk_is_valid():
    result = forecast_ewma_volatility(_returns([0, 0, np.nan, 0.01, 0.01]), min_periods=2)
    assert result["status"].tolist() == ["insufficient", "ready", "insufficient", "insufficient", "ready"]
    assert result.iloc[1]["forecast_variance_1d"] == 0.0
    assert result.iloc[-1]["forecast_variance_1d"] == pytest.approx(0.0001)


@pytest.mark.parametrize("kwargs", [
    {"decay": 0}, {"decay": 1}, {"decay": np.nan}, {"min_periods": 1},
    {"min_periods": 2.5}, {"annualization": 0}, {"horizons": ()},
    {"horizons": (1, 1)}, {"horizons": (1.5,)},
])
def test_ewma_rejects_invalid_parameters(kwargs):
    with pytest.raises(ValueError):
        forecast_ewma_volatility(_returns([0.01] * 30), **kwargs)


def test_ewma_rejects_bad_dates_and_returns_and_handles_empty_input():
    for returns in [pd.Series([0.01]), _returns([0.01, 0.02]).iloc[::-1],
                    _returns([np.inf]), _returns([-1.1])]:
        with pytest.raises(ValueError):
            forecast_ewma_volatility(returns)
    duplicate = _returns([0.01, 0.02])
    duplicate.index = pd.DatetimeIndex(["2026-01-01", "2026-01-01"])
    with pytest.raises(ValueError):
        forecast_ewma_volatility(duplicate)
    assert forecast_ewma_volatility(_returns([])).empty
