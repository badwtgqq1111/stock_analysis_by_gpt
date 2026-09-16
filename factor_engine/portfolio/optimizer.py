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
    max_holdings: int | None = None
    weighting: str = "score_risk"


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
    forced_codes: list[str] | None = None,
    forced_min_weight: float | dict | None = 0.0,
    forced_max_weight: float | dict | None = None,
) -> tuple[pd.DataFrame, dict]:
    """Return target weights under explicit long-only, industry and capacity limits.

    The implementation uses projected score weights rather than a hidden solver.
    This is deterministic, handles missing optional inputs, and records every
    active constraint in the returned manifest.

    ``forced_codes`` admits explicitly triggered names (for example a Donchian
    channel breakout confirmed by the signal layer) ahead of the pure model
    ranking.  ``forced_min_weight`` guarantees each admitted name a floor so a
    lower model percentile cannot reduce it to zero, and ``forced_max_weight``
    caps it so a separate strategy sleeve (for example short-horizon momentum)
    cannot grow beyond its own risk budget.  Both accept either a scalar or a
    ``{stock_code: value}`` mapping.
    """
    cfg = constraints or PortfolioConstraints()
    frame = candidates.copy()
    if frame.empty or score_col not in frame.columns:
        return frame, {"status": "empty", "constraints": asdict(cfg)}
    frame[score_col] = pd.to_numeric(frame[score_col], errors="coerce").fillna(0.0)
    frame = build_risk_snapshot(frame)
    frame = build_cost_snapshot(frame, initial_capital=initial_capital)
    codes = frame["stock_code"].astype(str)
    forced_set = {str(code) for code in (forced_codes or [])}
    forced_index = np.array([i for i, code in enumerate(codes) if code in forced_set], dtype=int)
    current = np.array([(current_weights or {}).get(code, 0.0) for code in codes], dtype=float)
    alpha = frame[score_col].to_numpy(dtype=float)
    alpha = alpha - np.nanmin(alpha)
    alpha = np.maximum(alpha, 0.0) + 1e-8
    risk = frame["specific_variance"].to_numpy(dtype=float)
    cost = pd.to_numeric(frame["expected_transaction_cost_bps"], errors="coerce").fillna(100.0).to_numpy(dtype=float) / 10_000.0
    # Select the strongest candidates first, then size them by inverse
    # volatility when requested.  This keeps the model score responsible for
    # selection while making capital allocation risk-aware.
    active = np.ones(len(frame), dtype=bool)
    if cfg.max_holdings is not None and int(cfg.max_holdings) > 0 and len(frame) > int(cfg.max_holdings):
        active[:] = False
        slots = int(cfg.max_holdings)
        ranked = np.argsort(-alpha, kind="stable")
        # Signal-triggered names claim their slot before the model ranking fills
        # the balance, so an explicit entry trigger is never displaced by the
        # cross-sectional score it is meant to complement.
        chosen = [int(index) for index in forced_index[:slots]]
        chosen_set = set(chosen)
        for index in ranked:
            if len(chosen) >= slots:
                break
            if int(index) not in chosen_set:
                chosen.append(int(index))
                chosen_set.add(int(index))
        active[np.asarray(chosen, dtype=int)] = True
    if str(cfg.weighting).lower() in {"inverse_volatility", "inverse-volatility", "volatility"}:
        raw = np.where(active, 1.0 / np.sqrt(np.maximum(risk, 1e-12)), 0.0)
    else:
        raw = np.where(
            active,
            alpha / (1.0 + float(cfg.risk_aversion) * risk + float(cfg.cost_penalty) * cost),
            0.0,
        )
    raw = raw / max(float(raw.sum()), 1e-12) * float(cfg.gross_exposure)
    target = np.minimum(raw, float(cfg.max_weight))
    tradable = frame.get("tradable_flag", pd.Series(True, index=frame.index)).fillna(True).astype(bool).to_numpy()
    target = np.where(tradable, target, 0.0)
    adv = pd.to_numeric(frame["adv_20d"], errors="coerce").to_numpy(dtype=float)
    capacity_weight = np.where(
        np.isfinite(adv) & (adv > 0),
        adv * float(cfg.max_participation) / max(float(initial_capital), 1.0),
        np.inf,
    )
    target = np.minimum(target, capacity_weight)
    target = _apply_industry_caps(frame, target, float(cfg.max_industry_weight))
    target = _limit_turnover(current, target, float(cfg.max_turnover))
    target = _renormalize_capped(target, float(cfg.gross_exposure), float(cfg.max_weight))
    forced_floors = {str(frame["stock_code"].iloc[index]): _forced_value(forced_min_weight, str(frame["stock_code"].iloc[index]), 0.0) for index in forced_index}
    forced_caps = {str(frame["stock_code"].iloc[index]): _forced_value(forced_max_weight, str(frame["stock_code"].iloc[index]), None) for index in forced_index}
    forced_diagnostics = {
        "forced_min_weight_effective": {}, "forced_floor_scale": 1.0,
        "forced_floor_feasible": True, "forced_floor_honoured": True, "forced_floor_gross": 0.0,
    }
    if forced_index.size and (any(value > 0 for value in forced_floors.values()) or any(value is not None for value in forced_caps.values())):
        target, forced_diagnostics = _apply_forced_bounds(
            target, forced_index, active, codes, forced_floors, forced_caps,
            float(cfg.gross_exposure), float(cfg.max_weight),
        )
    frame["current_weight"] = current
    frame["target_weight"] = target
    frame["trade_weight"] = target - current
    frame["portfolio_mode"] = "mean_variance_cost_aware"
    frame["constraint_status"] = np.where(target > 0, "eligible", "excluded")
    manifest = {
        "status": "completed", "portfolio_mode": "mean_variance_cost_aware", "constraints": asdict(cfg),
        "gross_exposure": float(target.sum()), "turnover": float(np.abs(target - current).sum()),
        "candidate_count": int(len(frame)), "selected_count": int((target > 0).sum()),
        "max_holdings": int(cfg.max_holdings) if cfg.max_holdings is not None else None,
        "weighting": str(cfg.weighting),
        "forced_codes": sorted(str(frame["stock_code"].iloc[index]) for index in forced_index),
        "forced_min_weight": forced_min_weight if isinstance(forced_min_weight, dict) else float(forced_min_weight or 0.0),
        "forced_max_weight": forced_max_weight if isinstance(forced_max_weight, dict) else forced_max_weight,
        # Requested floors and realized floors differ whenever the claims do not
        # fit inside the gross budget; both are published so a report can never
        # present the requested value as if it had been applied.
        "forced_min_weight_effective": forced_diagnostics["forced_min_weight_effective"],
        "forced_floor_scale": forced_diagnostics["forced_floor_scale"],
        "forced_floor_feasible": forced_diagnostics["forced_floor_feasible"],
        "forced_floor_honoured": forced_diagnostics["forced_floor_honoured"],
        "forced_floor_gross": forced_diagnostics["forced_floor_gross"],
        "forced_weighted_count": int(((target > 0) & np.isin(np.arange(len(frame)), forced_index)).sum()),
        "covariance_version": "diagonal-volatility.v1", "cost_version": "costs.v1",
    }
    return frame.sort_values("target_weight", ascending=False).reset_index(drop=True), manifest


def _forced_value(value, code, default):
    """Resolve a scalar-or-mapping forced bound for one stock code."""
    if value is None:
        return default
    if isinstance(value, dict):
        raw = value.get(code, default)
        return default if raw is None else float(raw)
    return float(value)


def _apply_forced_bounds(weights, forced_index, active, codes, floors, caps, gross, max_weight):
    """Apply per-name floors and caps to forced codes without breaching gross exposure.

    Forced sleeves (signal setups, short-horizon momentum) each carry their own
    weight budget: the floor keeps a triggered name from being zeroed by a weak
    model percentile, the cap keeps a small sleeve from dominating the book.
    The budget left over after the forced names is reallocated to the rest.

    Declared floors are honoured literally whenever they fit inside the gross
    budget.  When they do not - five signal names whose floors sum to 0.37
    against a bear-regime budget of 0.35, for example - every floor is scaled by
    one common ``gross / sum(floors)`` factor and that factor is returned in the
    diagnostics, so a book can never silently end up below the floor it claims
    to guarantee.  Returns ``(weights, diagnostics)``.
    """
    result = weights.copy()
    fixed = np.zeros(len(result), dtype=bool)
    resolved: list[tuple[int, float, float]] = []
    requested: dict[str, float] = {}
    for index in np.asarray(forced_index, dtype=int):
        if not bool(active[index]):
            continue
        code = str(codes.iloc[index]) if hasattr(codes, "iloc") else str(codes[index])
        floor = float(np.clip(float(floors.get(code, 0.0) or 0.0), 0.0, max_weight))
        cap_value = caps.get(code)
        cap = float(max_weight) if cap_value is None else float(np.clip(float(cap_value), 0.0, max_weight))
        cap = max(cap, floor)
        fixed[index] = True
        resolved.append((int(index), floor, cap))
        requested[code] = floor
    requested_gross = float(sum(floor for _, floor, _ in resolved))
    floor_scale = 1.0
    if requested_gross > float(gross) > 0:
        floor_scale = float(gross) / requested_gross
    floors_at: dict[int, float] = {}
    for index, floor, cap in resolved:
        effective_floor = floor * floor_scale
        floors_at[index] = effective_floor
        result[index] = min(max(float(result[index]), effective_floor), cap)
    # Only active, non-forced names may absorb the residual budget: an inactive
    # name must not be funded merely because a forced name left room.
    flexible = (~fixed) & np.asarray(active, dtype=bool)
    remaining = max(0.0, float(gross) - float(result[fixed].sum()))
    if remaining > 0:
        if flexible.any():
            current = float(result[flexible].sum())
            if current > 0:
                result[flexible] = np.minimum(result[flexible] * (remaining / current), float(max_weight))
            else:
                result[flexible] = min(remaining / max(int(flexible.sum()), 1), float(max_weight))
        else:
            # Every active name is strategy-forced, so leftover budget would
            # otherwise stay idle.  Fill it up to each name's own cap.
            indices = np.array([index for index, _, _ in resolved], dtype=int)
            headroom = np.clip(
                np.array([cap - float(result[index]) for index, _, cap in resolved], dtype=float), 0.0, None,
            )
            if indices.size and float(headroom.sum()) > 0:
                result[indices] += headroom * (min(remaining, float(headroom.sum())) / float(headroom.sum()))
    total = float(result.sum())
    if total > gross and total > 0:
        # Shed the flexible names first, then only the excess a fixed name holds
        # above its own floor.  Uniformly rescaling the whole book is what used
        # to break the floors silently.
        overflow = total - float(gross)
        flexible_total = float(result[flexible].sum()) if flexible.any() else 0.0
        if flexible_total > 0:
            result[flexible] = result[flexible] * max(0.0, (flexible_total - overflow) / flexible_total)
        total = float(result.sum())
        if total > float(gross) and resolved:
            indices = np.array([index for index, _, _ in resolved], dtype=int)
            floor_vector = np.array([floors_at[int(index)] for index in indices], dtype=float)
            excess = np.clip(result[indices] - floor_vector, 0.0, None)
            excess_total = float(excess.sum())
            if excess_total > 0:
                keep = max(0.0, excess_total - (total - float(gross)))
                result[indices] = floor_vector + excess * (keep / excess_total)
            total = float(result.sum())
        if total > gross and total > 0:
            # Unreachable while the floor scaling above fits the budget; kept as
            # a last-resort feasibility guard and reported via the diagnostics.
            result = result * (float(gross) / total)
    honoured = all(float(result[index]) >= floors_at[index] - 1e-9 for index in floors_at)
    diagnostics = {
        "forced_min_weight_effective": {
            code: round(float(floor) * float(floor_scale), 10) for code, floor in requested.items()
        },
        "forced_floor_scale": round(float(floor_scale), 10),
        "forced_floor_feasible": bool(floor_scale >= 1.0 - 1e-12),
        "forced_floor_honoured": bool(honoured),
        "forced_floor_gross": round(float(sum(floors_at.values())), 10),
    }
    return result, diagnostics


def _apply_industry_caps(frame: pd.DataFrame, weights: np.ndarray, cap: float) -> np.ndarray:
    industry = frame.get("industry_l1", pd.Series("__unknown__", index=frame.index)).fillna("__unknown__").astype(str)
    # Some providers store the taxonomy label (for example, "证监会行业分类")
    # in industry_l1 and the actual sector code/name in industry_l2.  Grouping
    # on that label would incorrectly put the whole universe in one bucket and
    # cap gross exposure at max_industry_weight.  Fall back to l2 when l1 is
    # non-informative or constant across candidates.
    l2 = frame.get("industry_l2")
    if l2 is not None:
        l2 = l2.fillna("").astype(str)
        informative_l1 = industry.replace({"", "__unknown__", "nan", "None"}).nunique() > 1
        if not informative_l1 and int((l2.str.strip() != "").sum()) > 0:
            industry = l2.where(l2.str.strip() != "", industry)
    result = weights.copy()
    for group in industry.unique():
        # Missing industry reference data must not be treated as one giant
        # industry.  Otherwise every otherwise eligible stock is capped by
        # the same bucket and gross exposure collapses to the industry cap.
        if group in {"", "__unknown__", "nan", "None"}:
            continue
        indices = np.flatnonzero(industry.to_numpy() == group)
        total = result[indices].sum()
        if total > cap and total > 0:
            result[indices] *= cap / total
    return result


def _limit_turnover(current: np.ndarray, target: np.ndarray, max_turnover: float) -> np.ndarray:
    # Initial deployment has no existing portfolio to turn over against.
    # Applying a rebalance turnover cap here would silently invest only
    # `max_turnover` of the requested gross exposure on the first run.
    if not np.any(np.abs(current) > 1e-12):
        return target
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
