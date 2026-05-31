"""仓位分配方法 — equal, score-weighted, Kelly.

参考 qlib TopkDropoutStrategy (等权) 和 SoftTopkStrategy (预算约束再平衡).
"""

from __future__ import annotations

import numpy as np


def equal_weight(scores: dict[str, float]) -> dict[str, float]:
    """等权分配."""
    n = len(scores)
    if n == 0:
        return {}
    return {code: 1.0 / n for code in scores}


def score_weight(scores: dict[str, float]) -> dict[str, float]:
    """按分数比例分配权重."""
    total = sum(max(s, 0.0) for s in scores.values())
    if total <= 0:
        return equal_weight(scores)
    return {code: max(s, 0.0) / total for code, s in scores.items()}


def kelly_weight(
    scores: dict[str, float],
    win_rates: dict[str, float],
    avg_win: float = 0.15,
    avg_loss: float = 0.10,
    half_kelly: bool = True,
) -> dict[str, float]:
    """Kelly 准则仓位分配.

    kelly_f = p - (1-p) / (avg_win/avg_loss)
    半凯利: half_kelly = kelly_f * 0.5
    """
    b_ratio = avg_win / avg_loss if avg_loss > 0 else 1.5
    weights = {}
    for code, score in scores.items():
        p = max(0.01, min(0.99, win_rates.get(code, 0.50)))
        kelly_f = max(0.0, p - (1.0 - p) / b_ratio)
        if half_kelly:
            kelly_f *= 0.5
        weights[code] = kelly_f * max(score, 0.0)

    total = sum(weights.values())
    if total <= 0:
        return equal_weight(scores)
    return {code: w / total for code, w in weights.items()}


def risk_budget_weight(
    scores: dict[str, float],
    volatilities: dict[str, float],
    risk_budget: dict[str, float] | None = None,
) -> dict[str, float]:
    """风险预算分配 (等风险贡献的近似).

    权重 ∝ score / volatility, 使得高波动标的自然获得较低权重.
    """
    if not volatilities:
        return score_weight(scores)

    raw = {}
    for code, score in scores.items():
        vol = max(volatilities.get(code, 0.01), 0.001)
        raw[code] = max(score, 0.0) / vol

    total = sum(raw.values())
    if total <= 0:
        return equal_weight(scores)

    if risk_budget:
        weights = {}
        for code in scores:
            w = raw[code] / total
            rb = risk_budget.get(code, 1.0 / len(scores))
            weights[code] = w * rb
        total_w = sum(weights.values())
        if total_w > 0:
            weights = {c: w / total_w for c, w in weights.items()}
        return weights

    return {code: w / total for code, w in raw.items()}
