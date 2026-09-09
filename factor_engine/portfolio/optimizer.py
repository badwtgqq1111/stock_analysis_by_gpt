"""Auditable long-only portfolio construction with risk, cost and turnover limits."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from factor_engine.portfolio.costs import estimate_row_transaction_cost


@dataclass(frozen=True)
class PortfolioConstraints:
    gross_exposure: float = 0.95
    max_weight: float = 0.10
    max_industry_weight: float = 0.30
    max_turnover: float = 0.50
    max_participation: float = 0.05
    risk_aversion: float = 2.0
    turnover_penalty: float = 0.10
    cost_penalty: float = 0.10


def build_risk_snapshot(candidates: pd.DataFrame, *, asof_date=None) -> pd.DataFrame:
    """Create a minimal, inspectable diagonal risk snapshot from available features."""
    frame = candidates.copy()
    raw_volatility = frame.get("volatility_20d", frame.get("recent_volatility", pd.Series(0.30, index=frame.index)))
    volatility = pd.to_numeric(raw_volatility, errors="coerce").fillna(0.30)
    frame["specific_variance"] = volatility.clip(lower=0.05, upper=1.50).pow(2)
    frame["risk_asof_date"] = pd.to_datetime(asof_date or frame.get("trade_date")).max()
    frame["covariance_version"] = "diagonal-volatility.v1"
    return frame


def build_cost_snapshot(candidates: pd.DataFrame, *, initial_capital=1_000_000.0) -> pd.DataFrame:
    """Attach deterministic ADV, impact and participation estimates to candidates."""
    frame = candidates.copy()
    provisional = 1.0 / max(1, len(frame))
    costs = [estimate_row_transaction_cost(row, target_weight=provisional, initial_capital=initial_capital) for row in frame.to_dict("records")]
    return pd.concat([frame.reset_index(drop=True), pd.DataFrame(costs)], axis=1)


def optimize_long_only(
    candidates: pd.DataFrame,
    *,
    score_col="model_score",
    current_weights: dict[str, float] | None = None,
    constraints: PortfolioConstraints | None = None,
    initial_capital=1_000_000.0,
) -> tuple[pd.DataFrame, dict]:
    """Return target weights under explicit long-only, industry and capacity limits.

    The implementation uses projected score weights rather than a hidden solver.
    This is deterministic, handles missing optional inputs, and records every
    active constraint in the returned manifest.
    """
    cfg = constraints or PortfolioConstraints()
    frame = candidates.copy()
    if frame.empty or score_col not in frame.columns:
        return frame, {"status": "empty", "constraints": asdict(cfg)}
    frame[score_col] = pd.to_numeric(frame[score_col], errors="coerce").fillna(0.0)
    frame = build_risk_snapshot(frame)
    frame = build_cost_snapshot(frame, initial_capital=initial_capital)
    codes = frame["stock_code"].astype(str)
    current = np.array([(current_weights or {}).get(code, 0.0) for code in codes], dtype=float)
    alpha = frame[score_col].to_numpy(dtype=float)
    alpha = alpha - np.nanmin(alpha)
    alpha = np.maximum(alpha, 0.0) + 1e-8
    risk = frame["specific_variance"].to_numpy(dtype=float)
    cost = pd.to_numeric(frame["expected_transaction_cost_bps"], errors="coerce").fillna(100.0).to_numpy(dtype=float) / 10_000.0
    raw = alpha / (1.0 + float(cfg.risk_aversion) * risk + float(cfg.cost_penalty) * cost)
    raw = raw / max(float(raw.sum()), 1e-12) * float(cfg.gross_exposure)
    target = np.minimum(raw, float(cfg.max_weight))
    tradable = frame.get("tradable_flag", pd.Series(True, index=frame.index)).fillna(True).astype(bool).to_numpy()
    target = np.where(tradable, target, 0.0)
    adv = pd.to_numeric(frame["adv_20d"], errors="coerce").to_numpy(dtype=float)
    capacity_weight = np.where(np.isfinite(adv) & (adv > 0), adv * float(cfg.max_participation) / max(float(initial_capital), 1.0), 0.0)
    target = np.minimum(target, capacity_weight)
    target = _apply_industry_caps(frame, target, float(cfg.max_industry_weight))
    target = _limit_turnover(current, target, float(cfg.max_turnover))
    target = _renormalize_capped(target, float(cfg.gross_exposure), float(cfg.max_weight))
    frame["current_weight"] = current
    frame["target_weight"] = target
    frame["trade_weight"] = target - current
    frame["portfolio_mode"] = "mean_variance_cost_aware"
    frame["constraint_status"] = np.where(target > 0, "eligible", "excluded")
    manifest = {
        "status": "completed", "portfolio_mode": "mean_variance_cost_aware", "constraints": asdict(cfg),
        "gross_exposure": float(target.sum()), "turnover": float(np.abs(target - current).sum()),
        "candidate_count": int(len(frame)), "selected_count": int((target > 0).sum()),
        "covariance_version": "diagonal-volatility.v1", "cost_version": "costs.v1",
    }
    return frame.sort_values("target_weight", ascending=False).reset_index(drop=True), manifest


def _apply_industry_caps(frame: pd.DataFrame, weights: np.ndarray, cap: float) -> np.ndarray:
    industry = frame.get("industry_l1", pd.Series("__unknown__", index=frame.index)).fillna("__unknown__").astype(str)
    result = weights.copy()
    for group in industry.unique():
        indices = np.flatnonzero(industry.to_numpy() == group)
        total = result[indices].sum()
        if total > cap and total > 0:
            result[indices] *= cap / total
    return result


def _limit_turnover(current: np.ndarray, target: np.ndarray, max_turnover: float) -> np.ndarray:
    turnover = np.abs(target - current).sum()
    if turnover <= max_turnover or turnover <= 0:
        return target
    return current + (target - current) * (max_turnover / turnover)


def _renormalize_capped(weights: np.ndarray, gross: float, max_weight: float) -> np.ndarray:
    result = np.clip(weights, 0.0, max_weight)
    total = result.sum()
    if total > gross and total > 0:
        result *= gross / total
    return result
