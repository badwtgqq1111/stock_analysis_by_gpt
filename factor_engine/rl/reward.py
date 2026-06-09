"""Reward helpers for portfolio RL research."""

from __future__ import annotations


def portfolio_reward(
    gross_return: float,
    *,
    transaction_cost: float = 0.0,
    turnover: float = 0.0,
    drawdown: float = 0.0,
    concentration: float = 0.0,
    constraint_violation: float = 0.0,
    turnover_penalty: float = 0.10,
    drawdown_penalty: float = 0.50,
    concentration_penalty: float = 0.10,
    violation_penalty: float = 1.00,
) -> float:
    """Cost/risk-adjusted reward for offline portfolio experiments."""
    return float(
        gross_return
        - transaction_cost
        - turnover_penalty * abs(turnover)
        - drawdown_penalty * max(drawdown, 0.0)
        - concentration_penalty * max(concentration, 0.0)
        - violation_penalty * max(constraint_violation, 0.0)
    )
