"""Independent, deterministic model adapters for the P2.71 factor path.

The adapters intentionally expose a small pre-registered set of models.  They
consume the same six causal factor columns and return a cross-sectional score;
they do not import the legacy selector, preselect, risk, or replay code.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from .factor_lightgbm import FEATURES


@dataclass(frozen=True)
class ModelSpec:
    name: str
    kind: str
    parameters: dict[str, Any]


def model_specs() -> tuple[ModelSpec, ...]:
    """Return the frozen pilot matrix; parameters are not selected on holdout."""
    return (
        ModelSpec("ridge", "regression", {"alpha": 10.0}),
        ModelSpec("elasticnet", "regression", {"alpha": 0.0005, "l1_ratio": 0.15}),
        ModelSpec("logistic", "classification", {"C": 0.25}),
        ModelSpec("extra_trees", "regression", {"n_estimators": 300, "min_samples_leaf": 30, "max_features": 0.8}),
        ModelSpec("random_forest", "regression", {"n_estimators": 300, "min_samples_leaf": 30, "max_features": 0.8}),
        ModelSpec("hist_gradient_boosting", "regression", {"max_iter": 150, "max_leaf_nodes": 15, "learning_rate": 0.03, "l2_regularization": 1.0}),
    )


def _xy(frame: pd.DataFrame, classification: bool = False):
    missing = set(FEATURES + ["label"]) - set(frame.columns)
    if missing:
        raise ValueError(f"训练集缺列: {sorted(missing)}")
    x = frame[FEATURES].astype(float)
    y = (frame.label > 0).astype(int) if classification else frame.label.astype(float)
    if not np.isfinite(x.to_numpy()).all() or not np.isfinite(np.asarray(y)).all():
        raise ValueError("模型输入含非有限数")
    if classification and y.nunique() < 2:
        raise ValueError("分类训练集只有一个类别")
    return x, y


def fit_model(spec: ModelSpec, train: pd.DataFrame):
    """Fit one frozen model using training rows only."""
    from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor, RandomForestRegressor
    from sklearn.linear_model import ElasticNet, LogisticRegression, Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    classification = spec.kind == "classification"
    x, y = _xy(train, classification)
    p = dict(spec.parameters)
    if spec.name == "ridge":
        model = make_pipeline(StandardScaler(), Ridge(**p))
    elif spec.name == "elasticnet":
        model = make_pipeline(StandardScaler(), ElasticNet(max_iter=5000, random_state=71, **p))
    elif spec.name == "logistic":
        model = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000, solver="lbfgs", random_state=71, **p))
    elif spec.name == "extra_trees":
        model = ExtraTreesRegressor(random_state=71, n_jobs=2, **p)
    elif spec.name == "random_forest":
        model = RandomForestRegressor(random_state=71, n_jobs=2, **p)
    elif spec.name == "hist_gradient_boosting":
        model = HistGradientBoostingRegressor(random_state=71, **p)
    else:
        raise ValueError(f"未知模型: {spec.name}")
    model.fit(x, y)
    return model


def score_model(model, spec: ModelSpec, frame: pd.DataFrame) -> pd.DataFrame:
    """Score candidates and apply the common deterministic replay ordering."""
    out = frame.copy()
    x = out[FEATURES].astype(float)
    if spec.kind == "classification":
        score = model.predict_proba(x)[:, 1]
    else:
        score = model.predict(x)
    if not np.isfinite(score).all():
        raise ValueError(f"{spec.name}产生非有限分数")
    out["score"] = score
    out["cause_width"] = -out["score"]
    return out.sort_values(["trade_date", "cause_width", "stock_code"], kind="stable").reset_index(drop=True)


def validation_gate(candidate: dict, baseline: dict, tolerance: float = 0.0) -> bool:
    """Simple validation-only gate used to freeze a winner before holdout."""
    return (
        np.isfinite(candidate.get("calendar_cagr", np.nan))
        and candidate["calendar_cagr"] + tolerance >= baseline["calendar_cagr"]
        and abs(candidate["max_drawdown"]) <= abs(baseline["max_drawdown"]) + 0.03
    )

