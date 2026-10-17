"""Probability semantics for factor predicates and cross-sectional scores.

An event's probability is not the same as a deterministic AND.  In general
P(A∩B) != P(A)P(B); the product is only an independence approximation.  The
classes below make that assumption explicit and fit all empirical transforms
on training data only.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np
import pandas as pd


def event_probability(indicator: pd.Series) -> float:
    """Empirical probability of a boolean event on the supplied sample."""
    values = indicator.astype("boolean")
    if values.empty:
        raise ValueError("事件样本为空")
    return float(values.fillna(False).mean())


def joint_probability(indicators: pd.DataFrame) -> float:
    """Empirical joint probability P(A and B and ...), without independence."""
    if indicators.empty or indicators.shape[1] == 0:
        raise ValueError("联合事件为空")
    return event_probability(indicators.fillna(False).astype(bool).all(axis=1))


def conditional_probability(event: pd.Series, given: pd.Series) -> float:
    """Empirical P(event | given); denominator-zero is an explicit error."""
    mask = given.fillna(False).astype(bool)
    if int(mask.sum()) == 0:
        raise ValueError("条件事件样本为空")
    return event.fillna(False).astype(bool)[mask].mean().item()


@dataclass(frozen=True)
class EmpiricalProbability:
    """Train-fitted empirical CDF mapping a factor value to (0,1)."""

    columns: tuple[str, ...]
    sorted_values: tuple[np.ndarray, ...]
    direction: tuple[float, ...]
    smoothing: float = 0.5

    @classmethod
    def fit(cls, train: pd.DataFrame, columns: Iterable[str], direction=None) -> "EmpiricalProbability":
        columns = tuple(columns)
        direction = tuple(direction or (1.0 for _ in columns))
        if len(columns) == 0 or len(columns) != len(direction):
            raise ValueError("概率映射列/方向不匹配")
        arrays = []
        for col in columns:
            values = pd.to_numeric(train[col], errors="raise").to_numpy(dtype=float)
            if values.size == 0 or not np.isfinite(values).all():
                raise ValueError(f"概率训练列非法: {col}")
            arrays.append(np.sort(values))
        if any(d not in (-1.0, 1.0) for d in direction):
            raise ValueError("方向只能为1或-1")
        return cls(columns, tuple(arrays), direction)

    def transform(self, frame: pd.DataFrame) -> pd.DataFrame:
        out = pd.DataFrame(index=frame.index)
        for col, values, direction in zip(self.columns, self.sorted_values, self.direction):
            x = pd.to_numeric(frame[col], errors="raise").to_numpy(dtype=float)
            if not np.isfinite(x).all():
                raise ValueError(f"概率评分列非法: {col}")
            # Mid-rank empirical CDF with Laplace-style smoothing; values are
            # bounded away from 0/1 so log-odds remain finite.
            rank = np.searchsorted(values, x, side="right")
            p = (rank + self.smoothing) / (len(values) + 2 * self.smoothing)
            out[col] = p if direction > 0 else 1.0 - p
        return out

    def independent_product(self, frame: pd.DataFrame) -> pd.Series:
        """P(A∩B)≈∏P(Aᵢ), explicitly labeled as independence approximation."""
        probs = self.transform(frame)
        return probs.prod(axis=1)

    def logit_sum(self, frame: pd.DataFrame, weights=None) -> pd.Series:
        """Weighted sum of factor log-odds; a ranking score, not calibrated P."""
        probs = self.transform(frame).clip(1e-6, 1 - 1e-6)
        weights = np.ones(len(self.columns)) if weights is None else np.asarray(weights, dtype=float)
        if weights.shape != (len(self.columns),) or not np.isfinite(weights).all():
            raise ValueError("logit权重非法")
        return pd.Series((np.log(probs / (1.0 - probs)) * weights).sum(axis=1), index=frame.index)

