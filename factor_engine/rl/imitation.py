"""Lightweight imitation policies for portfolio RL research."""

from __future__ import annotations

import numpy as np
import pandas as pd


class LinearImitationPolicy:
    """Behavior-cloning style linear policy fitted to expert weights."""

    def __init__(self, feature_columns: list[str], coef: np.ndarray, intercept: float = 0.0, max_weight: float = 0.08):
        self.feature_columns = list(feature_columns)
        self.coef = np.asarray(coef, dtype=float)
        self.intercept = float(intercept)
        self.max_weight = float(max_weight)

    @classmethod
    def fit(cls, training_rows: pd.DataFrame, feature_columns: list[str], *, target_col: str = "expert_weight", ridge: float = 1e-3, max_weight: float = 0.08):
        frame = training_rows.copy()
        x = frame.reindex(columns=feature_columns).replace([np.inf, -np.inf], np.nan).fillna(0.0).to_numpy(dtype=float)
        y = pd.to_numeric(frame.get(target_col), errors="coerce").fillna(0.0).to_numpy(dtype=float)
        x_aug = np.column_stack([np.ones(len(x)), x])
        reg = np.eye(x_aug.shape[1]) * float(ridge)
        reg[0, 0] = 0.0
        beta = np.linalg.pinv(x_aug.T @ x_aug + reg) @ x_aug.T @ y
        return cls(feature_columns, coef=beta[1:], intercept=beta[0], max_weight=max_weight)

    def predict_weights(self, frame: pd.DataFrame) -> np.ndarray:
        x = frame.reindex(columns=self.feature_columns).replace([np.inf, -np.inf], np.nan).fillna(0.0).to_numpy(dtype=float)
        raw = np.maximum(x @ self.coef + self.intercept, 0.0)
        if raw.sum() > 0:
            raw = raw / raw.sum()
        return np.clip(raw, 0.0, self.max_weight)

    def __call__(self, observation: dict) -> np.ndarray:
        return self.predict_weights(observation["features"])


def build_expert_training_rows(panel: pd.DataFrame, *, score_col: str = "ranking_score", top_n: int = 10, max_weight: float = 0.08) -> pd.DataFrame:
    """Create expert target weights from date-wise TopN score policy."""
    if panel is None or panel.empty:
        return pd.DataFrame()
    frame = panel.copy()
    if "trade_date" not in frame.columns:
        frame = frame.reset_index().rename(columns={frame.index.name or "index": "trade_date"})
    rows = []
    for _date, group in frame.groupby("trade_date"):
        group = group.copy()
        scores = pd.to_numeric(group.get(score_col), errors="coerce").fillna(0.0).to_numpy(dtype=float)
        weights = np.zeros(len(group), dtype=float)
        if len(group):
            top_idx = np.argsort(scores)[-max(1, min(int(top_n), len(scores))):]
            positive = np.maximum(scores[top_idx], 0.0)
            if positive.sum() > 0:
                weights[top_idx] = positive / positive.sum()
            else:
                weights[top_idx] = 1.0 / len(top_idx)
            weights = np.clip(weights, 0.0, max_weight)
        group["expert_weight"] = weights
        rows.append(group)
    return pd.concat(rows, ignore_index=True, sort=False)
