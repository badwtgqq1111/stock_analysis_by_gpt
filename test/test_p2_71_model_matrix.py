import numpy as np
import pandas as pd
import pytest

from backend.services.factor_lightgbm import FEATURES
from backend.services.factor_model_matrix import fit_model, model_specs, score_model, validation_gate


def _frame(n=80):
    rng = np.random.default_rng(71)
    dates = pd.bdate_range("2024-01-01", periods=n)
    out = pd.DataFrame({f: rng.normal(size=n) for f in FEATURES})
    out["label"] = 0.02 * out["mf_strength"] - 0.01 * out["price_to_ma20"] + rng.normal(0, .01, n)
    out["trade_date"] = dates
    out["stock_code"] = [f"A{i:04d}" for i in range(n)]
    return out


def test_matrix_is_explicit_and_fits_without_legacy_imports():
    frame = _frame()
    names = [s.name for s in model_specs()]
    assert names == ["ridge", "elasticnet", "logistic", "extra_trees", "random_forest", "hist_gradient_boosting"]
    for spec in model_specs():
        model = fit_model(spec, frame)
        scored = score_model(model, spec, frame)
        assert np.isfinite(scored.score).all()
        assert list(scored.columns).count("cause_width") == 1


def test_logistic_uses_direction_and_is_reproducible():
    frame = _frame()
    spec = next(s for s in model_specs() if s.name == "logistic")
    a = score_model(fit_model(spec, frame), spec, frame)
    b = score_model(fit_model(spec, frame), spec, frame)
    np.testing.assert_array_equal(a.score, b.score)
    assert ((a.score >= 0) & (a.score <= 1)).all()


def test_model_matrix_rejects_nonfinite_and_gate_is_validation_only():
    frame = _frame(); frame.loc[0, FEATURES[0]] = np.nan
    with pytest.raises(ValueError):
        fit_model(model_specs()[0], frame)
    assert validation_gate({"calendar_cagr": .3, "max_drawdown": -.2}, {"calendar_cagr": .2, "max_drawdown": -.2})
    assert not validation_gate({"calendar_cagr": .1, "max_drawdown": -.2}, {"calendar_cagr": .2, "max_drawdown": -.2})
