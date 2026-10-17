"""Mathematical composition primitives for the independent factor path.

An ``AND`` of factor predicates is a feasibility constraint, not a vector
embedding.  This module keeps that distinction explicit: hard gates decide
eligibility, while a separately normalized scalar score ranks eligible assets.
PCA is provided only as a train-fitted diagnostic ranker and never replaces a
gate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

import numpy as np
import pandas as pd


def hard_and(predicates: pd.DataFrame | Iterable[pd.Series]) -> pd.Series:
    """Boolean conjunction; missing values fail closed."""
    if isinstance(predicates, pd.DataFrame):
        if predicates.shape[1] == 0:
            raise ValueError("AND至少需要一个谓词")
        frame = predicates
    else:
        frame = pd.concat(list(predicates), axis=1)
        if frame.shape[1] == 0:
            raise ValueError("AND至少需要一个谓词")
    return frame.fillna(False).astype(bool).all(axis=1)


def indicator_and(predicates: pd.DataFrame | Iterable[pd.Series]) -> pd.Series:
    """The numeric Boolean AND: product of 0/1 indicators."""
    return hard_and(predicates).astype(float)


def soft_and(margins: pd.DataFrame, temperature: float = 8.0) -> pd.Series:
    """Smooth product t-norm for optimization diagnostics only.

    ``margins`` are signed distances (positive means the predicate passes).
    It is not used by the executable hard-gate strategy because a soft gate
    can admit an asset that violates a required constraint.
    """
    if margins.shape[1] == 0 or not np.isfinite(margins.to_numpy(dtype=float)).all():
        raise ValueError("soft AND 输入非法")
    if not np.isfinite(temperature) or temperature <= 0:
        raise ValueError("temperature必须为正数")
    sigmoid = 1.0 / (1.0 + np.exp(-np.clip(temperature * margins, -60, 60)))
    return sigmoid.prod(axis=1)


def cross_sectional_rank(frame: pd.DataFrame, column: str, *, ascending: bool = True) -> pd.Series:
    """Stable 0..1 rank per trade date, with deterministic tie handling."""
    if column not in frame or "trade_date" not in frame:
        raise ValueError(f"缺少排名列: {column}")
    values = pd.to_numeric(frame[column], errors="raise")
    if not np.isfinite(values.to_numpy(dtype=float)).all():
        raise ValueError(f"排名列含非有限值: {column}")
    rank = values.groupby(frame.trade_date).rank(method="first", ascending=ascending, pct=True)
    return rank.astype(float)


def weighted_rank_score(frame: pd.DataFrame, weights: Mapping[str, float], *, ascending=None) -> pd.Series:
    """Combine comparable cross-sectional ranks; weights are normalized by L1."""
    if not weights:
        raise ValueError("权重为空")
    ascending = dict(ascending or {})
    parts, used = [], []
    for col, weight in weights.items():
        if not np.isfinite(weight):
            raise ValueError(f"权重非法: {col}")
        if weight == 0:
            continue
        parts.append(cross_sectional_rank(frame, col, ascending=ascending.get(col, False)) * float(weight))
        used.append(abs(float(weight)))
    if not parts or sum(used) <= 0:
        raise ValueError("没有有效权重")
    out = sum(parts) / sum(used)
    if not np.isfinite(out.to_numpy(dtype=float)).all():
        raise ValueError("组合分数非法")
    return out


@dataclass(frozen=True)
class PCAComposer:
    columns: tuple[str, ...]
    mean_: np.ndarray
    scale_: np.ndarray
    component_: np.ndarray
    label_sign: float

    @classmethod
    def fit(cls, train: pd.DataFrame, columns: Iterable[str], label: str = "label") -> "PCAComposer":
        from sklearn.decomposition import PCA

        columns = tuple(columns)
        x = train.loc[:, columns].astype(float).to_numpy()
        y = pd.to_numeric(train[label], errors="raise").to_numpy(dtype=float)
        if len(columns) < 2 or len(train) < 3 or not np.isfinite(x).all() or not np.isfinite(y).all():
            raise ValueError("PCA训练输入非法")
        mean, scale = x.mean(axis=0), x.std(axis=0, ddof=0)
        scale = np.where(scale > 1e-12, scale, 1.0)
        z = (x - mean) / scale
        pca = PCA(n_components=1, svd_solver="full", random_state=71).fit(z)
        component = pca.components_[0]
        raw = z @ component
        corr = np.corrcoef(raw, y)[0, 1] if np.std(raw) > 0 and np.std(y) > 0 else 0.0
        sign = 1.0 if not np.isfinite(corr) or corr >= 0 else -1.0
        return cls(columns, mean, scale, component, sign)

    def score(self, frame: pd.DataFrame) -> pd.Series:
        x = frame.loc[:, self.columns].astype(float).to_numpy()
        if not np.isfinite(x).all():
            raise ValueError("PCA评分输入非法")
        values = self.label_sign * (((x - self.mean_) / self.scale_) @ self.component_)
        return pd.Series(values, index=frame.index, dtype=float)
