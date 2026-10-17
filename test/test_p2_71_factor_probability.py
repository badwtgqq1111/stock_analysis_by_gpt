import numpy as np
import pandas as pd
import pytest

from backend.services.factor_probability import EmpiricalProbability, conditional_probability, event_probability, joint_probability


def test_joint_probability_is_not_silent_independence_product():
    frame = pd.DataFrame({"a": [True, True, False, False], "b": [True, False, True, False]})
    assert event_probability(frame.a) == pytest.approx(.5)
    assert joint_probability(frame) == pytest.approx(.25)
    assert conditional_probability(frame.a, frame.b) == pytest.approx(.5)


def test_train_fitted_empirical_probability_has_finite_scores_and_no_future_fit():
    train = pd.DataFrame({"a": [1., 2., 3., 4.], "b": [4., 3., 2., 1.]})
    mapper = EmpiricalProbability.fit(train, ["a", "b"], direction=[1., -1.])
    test = pd.DataFrame({"a": [2., 5.], "b": [3., 0.]})
    p = mapper.transform(test)
    assert np.isfinite(p.to_numpy()).all()
    assert p.a.iloc[1] > p.a.iloc[0]
    assert np.isfinite(mapper.independent_product(test)).all()
    assert np.isfinite(mapper.logit_sum(test)).all()
    with pytest.raises(ValueError):
        EmpiricalProbability.fit(pd.DataFrame({"a": [np.nan]}), ["a"])
