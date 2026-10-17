import numpy as np
import pandas as pd
import pytest

from backend.services.factor_composition import PCAComposer, hard_and, indicator_and, soft_and, weighted_rank_score


def test_boolean_and_is_intersection_and_numeric_product():
    p = pd.DataFrame({"a": [True, True, False, np.nan], "b": [True, False, True, True]})
    assert hard_and(p).tolist() == [True, False, False, False]
    assert indicator_and(p).tolist() == [1.0, 0.0, 0.0, 0.0]


def test_soft_and_is_only_a_smooth_diagnostic():
    margins = pd.DataFrame({"a": [1.0, -1.0], "b": [1.0, 1.0]})
    out = soft_and(margins, temperature=8)
    assert out.iloc[0] > out.iloc[1]
    assert out.iloc[1] > 0  # a soft gate may be non-zero despite a failed predicate


def test_rank_combination_is_scale_invariant_and_deterministic():
    frame = pd.DataFrame({"trade_date": ["2024-01-01"] * 3, "a": [1, 2, 3], "b": [300, 200, 100]})
    score = weighted_rank_score(frame, {"a": 1, "b": 1}, ascending={"a": False, "b": True})
    assert score.tolist() == pytest.approx([1.0, 2 / 3, 1 / 3])


def test_pca_is_fit_on_train_and_does_not_change_gate_semantics():
    train = pd.DataFrame({"a": [1., 2., 3., 4.], "b": [1., 2., 2., 4.], "label": [0., .1, .2, .3]})
    composer = PCAComposer.fit(train, ["a", "b"])
    test = train.drop(columns="label").iloc[:2]
    assert np.isfinite(composer.score(test)).all()
    assert composer.columns == ("a", "b")
