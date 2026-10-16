import numpy as np
import pandas as pd
import pytest

from factor_engine.portfolio.volatility_sizing import cap_weights, predicted_covariance, risk_contributions


def _sample():
    return pd.DataFrame(np.random.default_rng(11).normal(0, .02, (100, 3)),
                        index=pd.bdate_range("2026-01-01", periods=100), columns=["A", "B", "C"])


def test_predicted_covariance_preserves_historical_correlation_and_psd():
    returns = _sample()
    cov, _ = predicted_covariance(returns)
    vols = np.sqrt(np.diag(cov))
    np.testing.assert_allclose(cov / np.outer(vols, vols), returns.tail(60).corr(), atol=1e-12)
    assert np.linalg.eigvalsh(cov).min() >= -1e-12


def test_cap_only_reduces_exposure_preserves_rank_and_contributions_sum():
    cov, _ = predicted_covariance(_sample())
    w = pd.Series([.5, .3, .2], index=cov.index)
    capped = cap_weights(w, cov, target_vol=.08)
    np.testing.assert_allclose(capped / w, (capped / w).iloc[0])
    assert capped.sum() <= w.sum()
    assert np.sqrt(capped @ cov @ capped) <= .08 + 1e-12
    rc = risk_contributions(capped, cov)
    assert rc.component_vol.sum() == pytest.approx(rc.portfolio_vol.iloc[0])


def test_missing_name_is_not_dropped_and_future_data_not_used():
    r = _sample()
    a, _ = predicted_covariance(r.iloc[:80])
    r.iloc[80:] = -.9
    b, _ = predicted_covariance(r.iloc[:80])
    pd.testing.assert_frame_equal(a, b)
    r.iloc[-1, 0] = np.nan
    with pytest.raises(ValueError):
        predicted_covariance(r)
