import numpy as np
import pandas as pd
import pytest

from backend.services.factor_portfolio_optimizer import OptimizerConfig, attach_target_weights, optimize_long_only, trailing_covariance


def test_optimizer_respects_long_only_caps_and_budget():
    scores = pd.Series([2., 1., .5], index=['A', 'B', 'C'])
    w = optimize_long_only(scores, config=OptimizerConfig(max_weight=.6, target_gross=.9))
    assert np.isfinite(w).all() and (w >= 0).all() and (w <= .6 + 1e-9).all()
    assert w.sum() == pytest.approx(.9, abs=1e-6)


def test_covariance_and_target_weight_attachment_are_deterministic():
    returns = pd.DataFrame(np.arange(30).reshape(10, 3), columns=['A', 'B', 'C']).pct_change().dropna()
    cov = trailing_covariance(returns, ['A', 'B', 'C'], lookback=5)
    assert cov.shape == (3, 3) and np.linalg.eigvalsh(cov).min() > 0
    signals = pd.DataFrame({'trade_date': pd.to_datetime(['2024-01-01'] * 3), 'stock_code': ['A', 'B', 'C'], 'score': [3., 2., 1.]})
    out = attach_target_weights(signals, config=OptimizerConfig(target_gross=.9))
    assert out.target_weight.sum() == pytest.approx(.9, abs=1e-6)
    pd.testing.assert_series_equal(out.target_weight, attach_target_weights(signals).target_weight, check_names=False)


def test_optimizer_rejects_bad_covariance():
    with pytest.raises(ValueError): optimize_long_only(pd.Series([1., 2.]), np.ones((3, 3)))
