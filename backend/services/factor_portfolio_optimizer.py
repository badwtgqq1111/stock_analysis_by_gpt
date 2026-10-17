"""Institutional-style long-only optimizer for the independent factor path.

The optimizer is deliberately small and auditable: expected returns come from
the factor score, covariance is a trailing sample estimate, and the objective
penalizes variance, turnover, and concentration.  It returns target weights;
the independent replay remains responsible for fills, stops and fees.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class OptimizerConfig:
    risk_aversion: float = 2.0
    turnover_penalty: float = 0.10
    max_weight: float = 0.35
    min_weight: float = 0.0
    target_gross: float = 0.90
    ridge: float = 1e-4

    def __post_init__(self):
        for name in ('risk_aversion', 'turnover_penalty', 'max_weight', 'min_weight', 'target_gross', 'ridge'):
            value = getattr(self, name)
            if not np.isfinite(value) or value < 0:
                raise ValueError(f'{name}必须为非负有限数')
        if self.max_weight <= 0 or self.min_weight > self.max_weight or self.target_gross > 1 or self.ridge <= 0:
            raise ValueError('优化器约束非法')


def _normalize(values: pd.Series) -> pd.Series:
    values = pd.to_numeric(values, errors='raise').astype(float)
    if not np.isfinite(values).all():
        raise ValueError('优化器分数含非有限值')
    centered = values - values.mean()
    scale = centered.std(ddof=0)
    return centered / scale if scale > 1e-12 else pd.Series(0.0, index=values.index)


def trailing_covariance(returns: pd.DataFrame, columns: list[str], lookback: int = 60, ridge: float = 1e-4) -> np.ndarray:
    if lookback <= 1 or not columns:
        raise ValueError('协方差窗口或列为空')
    x = returns.loc[:, columns].tail(lookback).astype(float)
    if len(x) < 2 or not np.isfinite(x.to_numpy()).all():
        raise ValueError('协方差样本不足或含非有限值')
    cov = x.cov(ddof=1).to_numpy(dtype=float)
    cov = (cov + cov.T) / 2.0
    cov += np.eye(len(columns)) * ridge
    return cov


def optimize_long_only(scores: pd.Series, covariance: np.ndarray | None = None,
                       previous: pd.Series | None = None,
                       config: OptimizerConfig = OptimizerConfig()) -> pd.Series:
    """Deterministic projected-gradient approximation to mean-variance sizing."""
    names = list(scores.index.astype(str))
    n = len(names)
    if n == 0:
        return pd.Series(dtype=float)
    mu = _normalize(scores).to_numpy(dtype=float)
    if covariance is None:
        covariance = np.eye(n)
    covariance = np.asarray(covariance, dtype=float)
    if covariance.shape != (n, n) or not np.isfinite(covariance).all():
        raise ValueError('协方差维度或数值非法')
    covariance = (covariance + covariance.T) / 2.0 + np.eye(n) * config.ridge
    prev = np.zeros(n) if previous is None else pd.Series(previous).reindex(names, fill_value=0.0).to_numpy(dtype=float)
    if not np.isfinite(prev).all() or (prev < 0).any():
        raise ValueError('旧权重非法')
    target = min(config.target_gross, n * config.max_weight)

    def project(values):
        projected = np.clip(values, config.min_weight, config.max_weight)
        for _ in range(50):
            gap = target - projected.sum()
            if abs(gap) < 1e-10:
                break
            if gap > 0:
                active = projected < config.max_weight - 1e-12
            else:
                active = projected > config.min_weight + 1e-12
            if not active.any():
                break
            projected[active] += gap / active.sum()
            projected = np.clip(projected, config.min_weight, config.max_weight)
        return projected

    w = project(np.full(n, target / n))
    step = 0.05 / max(1.0, np.linalg.norm(covariance, ord=2) * config.risk_aversion)
    for _ in range(600):
        grad = mu - config.risk_aversion * (covariance @ w) - config.turnover_penalty * np.sign(w - prev)
        nxt = project(w + step * grad)
        if np.max(np.abs(nxt - w)) < 1e-8:
            break
        w = nxt
    return pd.Series(w, index=names, dtype=float)


def attach_target_weights(signals: pd.DataFrame, covariance_by_day: dict[pd.Timestamp, np.ndarray] | None = None,
                          previous: pd.Series | None = None, config: OptimizerConfig = OptimizerConfig()) -> pd.DataFrame:
    """Convert each day's ranked candidates into target weights."""
    required = {'trade_date', 'stock_code', 'score'}
    if not required <= set(signals.columns):
        raise ValueError(f'信号缺列: {sorted(required-set(signals.columns))}')
    outputs = []
    prev = previous
    for day, group in signals.groupby('trade_date', sort=True):
        scores = group.set_index('stock_code').score
        cov = None if covariance_by_day is None else covariance_by_day.get(day)
        weights = optimize_long_only(scores, cov, prev, config)
        out = group.copy()
        out['target_weight'] = out.stock_code.map(weights).fillna(0.0)
        # The replay's deterministic order is the execution order.  Sort by
        # optimizer target weight while retaining stock code as the tie-break.
        out['cause_width'] = -out['target_weight']
        outputs.append(out)
        prev = weights
    return pd.concat(outputs, ignore_index=True) if outputs else signals.assign(target_weight=0.0)
