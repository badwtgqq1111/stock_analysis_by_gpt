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
    # score-linked sizing (weighting="score_inverse_vol")
    alpha_power: float = 1.0
    alpha_weight_floor: float = 0.0
    vol_exponent: float = 0.5
    # a forced (signal-override) floor must be earned by the score
    forced_floor_score_scaling: bool = False
    forced_floor_max_weight: float | None = None
    # flow confirmation: tilt the raw weights by a flow z-score column
    flow_tilt_strength: float = 0.0
    flow_column: str = "moneyflow_net_z_5d_clean"
    # Risk control: annualised portfolio volatility ceiling and the largest
    # share of total portfolio variance one name may carry.
    target_volatility: float | None = None
    max_name_risk_share: float | None = None
    risk_vol_column: str = "volatility_20d"
    # Soft risk scalers: names whose speculative / downside profile is stretched
    # are de-weighted (never dropped) and the freed budget moves to calmer names.
    risk_scalers: dict | None = None
    market_deleverage: dict | None = None
    covariance: object | None = None
    # Risk-parity target: blend the optimized weights towards equal risk
    # contribution.  0.0 keeps pure alpha/vol weights, 1.0 is full risk parity.
    risk_parity_blend: float = 0.0
    risk_parity_iterations: int = 150
    # "cap" only de-levers when the target is breached; "budget" also deploys the
    # unused risk budget up to max_gross_exposure and max_weight.
    vol_target_mode: str = "cap"
    max_gross_exposure: float | None = None


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
    cost_frame = pd.DataFrame(costs)
    # Drop columns the cost model re-emits, otherwise the frame carries two
    # columns with the same label and every later `frame[col]` lookup returns a
    # DataFrame instead of a Series.
    frame = frame.drop(columns=[column for column in cost_frame.columns if column in frame.columns], errors="ignore")
    return pd.concat([frame.reset_index(drop=True), cost_frame], axis=1)


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
    weight_caps: dict | None = None,
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
    weighting_mode = str(cfg.weighting).lower()
    if weighting_mode in {"inverse_volatility", "inverse-volatility", "volatility"}:
        raw = np.where(active, 1.0 / np.sqrt(np.maximum(risk, 1e-12)), 0.0)
    elif weighting_mode in {"score_inverse_vol", "score_x_inverse_vol", "rank_inverse_vol"}:
        # Rank-linked sizing: the model score drives the weight, tempered by
        # inverse volatility.  ``alpha_weight_floor`` keeps the weakest selected
        # name from being sized to zero when the book is small.
        span = float(np.nanmax(alpha)) if len(alpha) else 0.0
        normalized = (alpha / span) if span > 0 else np.zeros_like(alpha)
        alpha_floor = min(max(float(getattr(cfg, "alpha_weight_floor", 0.0) or 0.0), 0.0), 1.0)
        score_term = alpha_floor + (1.0 - alpha_floor) * np.power(
            np.maximum(normalized, 0.0), max(float(getattr(cfg, "alpha_power", 1.0) or 1.0), 1e-6)
        )
        # ``vol_exponent`` tempers the risk tilt: 0.5 keeps a mild inverse-vol
        # adjustment, 0.0 makes the book purely score-proportional.
        vol_term = 1.0 / np.power(np.maximum(risk, 1e-12), max(float(getattr(cfg, "vol_exponent", 0.5) or 0.0), 1e-6))
        raw = np.where(active, score_term * vol_term, 0.0)
    elif weighting_mode in {"rank_power", "rank_score", "rank"}:
        # Explicit rank sizing: the Top-1 name receives the largest score term
        # and each following rank decays as ((N-rank+1)/N)^alpha_power.  This is
        # what "排名靠前的拿更多仓位" means; ``score_inverse_vol`` could not
        # deliver it because the model scores of the leading block are nearly
        # identical once the signal candidates are in the same normalization pool.
        active_indices = np.flatnonzero(active)
        count = len(active_indices)
        score_term = np.zeros_like(alpha)
        if count:
            ordered = active_indices[np.argsort(-alpha[active_indices], kind="stable")]
            power = max(float(getattr(cfg, "alpha_power", 1.0) or 1.0), 1e-6)
            alpha_floor = min(max(float(getattr(cfg, "alpha_weight_floor", 0.0) or 0.0), 0.0), 1.0)
            for position, index in enumerate(ordered):
                share = (count - position) / count
                score_term[index] = alpha_floor + (1.0 - alpha_floor) * float(share) ** power
        vol_term = 1.0 / np.power(np.maximum(risk, 1e-12), max(float(getattr(cfg, "vol_exponent", 0.5) or 0.0), 1e-6))
        raw = np.where(active, score_term * vol_term, 0.0)
    else:
        raw = np.where(
            active,
            alpha / (1.0 + float(cfg.risk_aversion) * risk + float(cfg.cost_penalty) * cost),
            0.0,
        )
    flow_tilt_report = None
    if float(getattr(cfg, "flow_tilt_strength", 0.0) or 0.0) != 0.0:
        flow_column = str(getattr(cfg, "flow_column", "") or "")
        if flow_column and flow_column in frame.columns:
            flow_values = pd.to_numeric(frame[flow_column], errors="coerce").fillna(0.0).to_numpy(dtype=float)
            tilt = np.clip(1.0 + float(cfg.flow_tilt_strength) * np.tanh(flow_values), 0.4, 1.6)
            raw = np.where(active, raw * tilt, 0.0)
            flow_tilt_report = {
                "column": flow_column, "strength": float(cfg.flow_tilt_strength),
                "tilt_min": float(tilt.min()), "tilt_max": float(tilt.max()),
                "coverage": float(pd.to_numeric(frame[flow_column], errors="coerce").notna().mean()),
            }
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
    floor_scaling_report: dict = {}
    if forced_floors and bool(getattr(cfg, "forced_floor_score_scaling", False)):
        # A signal override used to keep a full floor even when the model ranked it
        # last (observed: rank 4628 of 5,209 taking 9.5% of the book).  Scale the
        # floor by the name's score percentile so the override still enters, but
        # only claims capital the score supports.
        span = float(np.nanmax(alpha)) if len(alpha) else 0.0
        normalized = (alpha / span) if span > 0 else np.zeros_like(alpha)
        floor_cap = getattr(cfg, "forced_floor_max_weight", None)
        scaled: dict = {}
        for index in forced_index:
            code = str(frame["stock_code"].iloc[index])
            floor = float(forced_floors.get(code, 0.0) or 0.0)
            if floor <= 0:
                scaled[code] = floor
                continue
            earned = floor * float(min(max(normalized[index], 0.0), 1.0))
            if floor_cap is not None:
                earned = min(earned, float(floor_cap))
            scaled[code] = earned
            floor_scaling_report[code] = {"declared": round(floor, 6), "score_percentile": round(float(normalized[index]), 4),
                                          "effective": round(float(earned), 6)}
        forced_floors = scaled
    forced_diagnostics = {
        "forced_min_weight_effective": {}, "forced_floor_scale": 1.0,
        "forced_floor_feasible": True, "forced_floor_honoured": True, "forced_floor_gross": 0.0,
    }
    if forced_index.size and (any(value > 0 for value in forced_floors.values()) or any(value is not None for value in forced_caps.values())):
        target, forced_diagnostics = _apply_forced_bounds(
            target, forced_index, active, codes, forced_floors, forced_caps,
            float(cfg.gross_exposure), float(cfg.max_weight),
        )
    if floor_scaling_report:
        forced_diagnostics["floor_score_scaling"] = floor_scaling_report
    cap_report = None
    if weight_caps:
        caps = {str(code): float(value) for code, value in weight_caps.items() if value is not None}
        if caps:
            gross_before = float(target.sum())
            capped = target.copy()
            hit = []
            for index, code in enumerate(codes.astype(str)):
                cap = caps.get(code)
                if cap is not None and capped[index] > cap:
                    capped[index] = cap
                    hit.append(code)
            # `_renormalize_capped` only scales down, so the budget freed by a cap
            # must be handed back explicitly: redistribute proportionally to the
            # uncapped active names, respecting the per-name ceiling.
            freed = gross_before - float(capped.sum())
            if freed > 1e-12:
                eligible = active & ~np.isin(codes.astype(str), list(caps))
                base = np.where(eligible, capped, 0.0)
                if base.sum() > 0:
                    room = np.where(eligible, np.maximum(0.0, float(cfg.max_weight) - capped), 0.0)
                    add = base / base.sum() * freed
                    capped = capped + np.minimum(add, room)
            target = _renormalize_capped(capped, float(cfg.gross_exposure), float(cfg.max_weight))
            cap_report = {"capped_codes": sorted(hit), "cap_count": len(caps),
                          "gross_before": gross_before, "gross_after": float(target.sum())}
    frame["current_weight"] = current
    frame["target_weight"] = target
    target, risk_control = _apply_risk_control(frame, target, cfg)
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
        "flow_tilt": flow_tilt_report,
        "weight_caps": cap_report,
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
        "risk_control": risk_control,
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
        # pandas 3 不再接受 set 形式的 to_replace（旧写法会把 ""/unknown/nan/None 视为缺失），
        # 用 where+isin 表达同样的语义：先屏蔽无效标签，再看还有几个不同行业。
        informative_l1 = industry.where(~industry.isin({"", "__unknown__", "nan", "None"})).nunique() > 1
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


def _apply_risk_scalers(frame: pd.DataFrame, weights: np.ndarray, cfg) -> tuple[np.ndarray, dict]:
    """De-weight - never drop - names whose risk profile is stretched.

    Each scaler in ``cfg.risk_scalers`` is ``{column, threshold, slope, floor}``:
    a name is scaled by ``max(floor, 1 - slope * (value - threshold))`` once the
    column exceeds the threshold.  The freed weight is redistributed across the
    remaining names so the book stays fully invested; the total risk ceiling is
    still enforced afterwards by the volatility target.

    Evidence behind the defaults (see output/verification/turnover_20260917):
    turnover-rate z > 2 cuts the 20-day excess from +2.10% to +1.22% (win rate
    43%), turnover volatility > 0.7 cuts it to +1.47%, and both tails of the
    volatility distribution under-perform the middle.
    """
    report: dict = {"applied": [], "scaled_names": [], "scale_by_name": {}}
    scalers = cfg.risk_scalers or {}
    if not scalers or float(weights.sum()) <= 0:
        return weights, report
    result = weights.copy()
    live = result > 0
    for key, spec in scalers.items():
        column = str(spec.get("column", key))
        if column not in frame.columns:
            continue
        series = frame[column]
        if isinstance(series, pd.DataFrame):
            series = series.iloc[:, 0]
        values = pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)
        threshold = spec.get("threshold")
        if threshold is None:
            continue
        slope = float(spec.get("slope", 1.0) or 1.0)
        floor = float(spec.get("floor", 0.5) or 0.5)
        direction = str(spec.get("direction", "above")).lower()
        scales = np.ones(len(result), dtype=float)
        for index in np.flatnonzero(live):
            value = values[index]
            if not np.isfinite(value):
                continue
            excess = (value - float(threshold)) if direction != "below" else (float(threshold) - value)
            if excess <= 0:
                continue
            scales[index] = max(floor, 1.0 - slope * excess)
        if bool((scales < 1.0 - 1e-12).any()):
            report["applied"].append(column)
            report["scale_by_name"].update({
                str(frame["stock_code"].iloc[index]): round(float(scales[index]), 4)
                for index in np.flatnonzero(scales < 1.0 - 1e-12)
            })
            report["scaled_names"] = sorted(set(report["scaled_names"]) | set(report["scale_by_name"]))
            # shrink the offenders, then hand the freed weight to the rest
            freed = float((result * (1.0 - scales)).sum())
            result = result * scales
            keep = (result > 0) & (scales >= 1.0 - 1e-12)
            if freed > 0 and bool(keep.any()):
                base = float(result[keep].sum())
                if base > 0:
                    result[keep] += freed * (result[keep] / base)
    total = float(result.sum())
    if total > 0 and weights.sum() > 0:      # keep the book at its previous gross
        result = result * (float(weights.sum()) / total)
    return result, report


def _apply_risk_control(frame: pd.DataFrame, weights: np.ndarray, cfg) -> tuple[np.ndarray, dict]:
    """Volatility ceiling and per-name risk-share cap on the finished book.

    The covariance snapshot is diagonal, so portfolio variance is the sum of the
    squared weighted vols.  Two guards are applied in order:

    1. per-name risk share - one name may not carry more than
       ``max_name_risk_share`` of total variance (cap, redistribute to the rest);
    2. portfolio volatility - if the ex-ante vol exceeds ``target_volatility``
       the whole book is scaled down (cash is left idle rather than levered up).
    """
    report = {"target_volatility": cfg.target_volatility, "max_name_risk_share": cfg.max_name_risk_share,
              "enforced": False, "vol_before": None, "vol_after": None, "scale": 1.0, "capped_names": []}
    if weights.size == 0 or float(weights.sum()) <= 0:
        return weights, report
    column = cfg.risk_vol_column if cfg.risk_vol_column in frame.columns else "volatility_20d"
    if column not in frame.columns:
        return weights, report
    raw_vol = frame[column]
    if isinstance(raw_vol, pd.DataFrame):   # duplicate labels: keep the first
        raw_vol = raw_vol.iloc[:, 0]
    vol = pd.to_numeric(raw_vol, errors="coerce").to_numpy(dtype=float)
    vol = np.where(np.isfinite(vol) & (vol > 0), vol, np.nan)
    live = weights > 0
    if not bool(live.any()):
        return weights, report
    result = weights.copy()
    result, scaler_report = _apply_risk_scalers(frame, result, cfg)
    report["scalers"] = scaler_report
    covariance = getattr(cfg, "covariance", None)
    covariance_note = "diagonal"
    if isinstance(covariance, dict):
        # 形如 {"codes": [...], "matrix": [[...]]}：按当前 frame 重建子矩阵，
        # 这样即使组合在修复循环中被裁剪（名字数变化）也不会维度错配。
        codes = [str(code) for code in covariance.get("codes") or []]
        matrix = np.asarray(covariance.get("matrix"), dtype=float)
        index_of = {code: position for position, code in enumerate(codes)}
        order = [str(code) for code in frame["stock_code"]]
        if matrix.ndim == 2 and matrix.shape[0] == matrix.shape[1] == len(codes) and order:
            subset = np.full((len(order), len(order)), np.nan)
            vols = pd.to_numeric(frame.get(cfg.risk_vol_column, frame.get("volatility_20d")),
                                 errors="coerce").to_numpy(dtype=float)
            for row, code_row in enumerate(order):
                for column, code_column in enumerate(order):
                    if code_row in index_of and code_column in index_of:
                        subset[row, column] = matrix[index_of[code_row]][index_of[code_column]]
            for position in range(len(order)):
                if not np.isfinite(subset[position, position]):
                    # 同时清掉整行与整列，否则残留 NaN 会让特征值分解失败
                    subset[position, :] = 0.0
                    subset[:, position] = 0.0
                    subset[position, position] = (vols[position] ** 2) if np.isfinite(vols[position]) else np.nan
            if np.isfinite(np.diag(subset)).all():
                covariance = (subset + subset.T) / 2.0
                minimum = float(np.min(np.linalg.eigvalsh(covariance)))
                if minimum < 1e-10:
                    covariance = covariance + np.eye(len(order)) * (1e-8 - minimum)
                covariance_note = "full"
            else:
                covariance = None
        else:
            covariance = None
    elif covariance is not None:
        covariance = np.asarray(covariance, dtype=float)
        if covariance.ndim == 2 and covariance.shape[0] == covariance.shape[1] == len(frame):
            covariance_note = "full"
        else:
            covariance = None                     # 形状不匹配时退回对角线，绝不静默算错
    report["covariance"] = covariance_note

    def _variance(vector: np.ndarray) -> float:
        if covariance is not None:
            return float(max(vector @ covariance @ vector, 0.0))
        return float(np.nansum((vector * np.where(np.isfinite(vol), vol, 0.0)) ** 2))

    def _risk_contributions(vector: np.ndarray) -> np.ndarray:
        """Marginal risk contribution of each name (w_i (Σw)_i)."""
        if covariance is not None:
            return vector * (covariance @ vector)
        return (vector * np.where(np.isfinite(vol), vol, 0.0)) ** 2

    total_variance = _variance(result)
    report["vol_before"] = round(float(np.sqrt(max(total_variance, 0.0))), 6) if total_variance > 0 else None

    cap = cfg.max_name_risk_share
    live_count = int(live.sum())
    if cap is not None and 0 < float(cap) < 1 and total_variance > 0 and live_count * float(cap) < 1.0 - 1e-9:
        # Fewer names than the cap can be satisfied with (e.g. two names, 35% cap):
        # shrinking everyone would drain the book without changing the shares.
        report["cap_infeasible"] = True
        cap = None
    if cap is not None and 0 < float(cap) < 1 and total_variance > 0:
        live_index = np.flatnonzero(live)
        # Iterative risk budgeting: shrink the offenders and hand the freed weight
        # to the names below the cap, otherwise the offender's share never falls
        # (shrinking alone shrinks the denominator too).
        for _ in range(50):
            # 只取有持仓的名字：frame 里可能包含权重为 0 的候选，
            # 全量 contributions 与 live_index 长度不一致会导致布尔索引错配。
            contributions = _risk_contributions(result)[live_index]
            total_variance = float(np.nansum(contributions))
            if total_variance <= 0:
                break
            shares = contributions / total_variance
            over = shares > float(cap)
            if not bool(over.any()):
                break
            positions = live_index[over]
            keep = live_index[~over]
            if keep.size == 0:
                break                      # nothing can absorb the freed weight
            freed = 0.0
            for index in positions:
                reduction = result[index] * 0.05
                result[index] -= reduction
                freed += reduction
                report["capped_names"].append(int(index))
            if keep.size and freed > 0:
                base = result[keep].sum()
                if base > 0:
                    result[keep] += freed * (result[keep] / base)
            report["enforced"] = True

    # Risk-parity target: iterative fixed point on the covariance so each name
    # contributes the same amount of risk, then blend with the alpha weights.
    if float(getattr(cfg, "risk_parity_blend", 0.0) or 0.0) > 0 and live_count >= 2:
        blend = float(np.clip(cfg.risk_parity_blend, 0.0, 1.0))
        parity = result.copy()
        gross_target = float(result.sum())
        for _ in range(int(getattr(cfg, "risk_parity_iterations", 150) or 150)):
            contributions = _risk_contributions(parity)
            contributions = np.where(np.isfinite(contributions) & (contributions > 0), contributions, np.nan)
            if not np.isfinite(contributions[live]).all():
                break
            target = float(np.nansum(contributions)) / max(live_count, 1)
            parity = np.where(live, parity * np.sqrt(target / np.maximum(contributions, 1e-18)) ** 0.5, 0.0)
            total = float(parity.sum())
            if total <= 0:
                break
            parity = parity * (gross_target / total)
        result = (1.0 - blend) * result + blend * parity
        report["risk_parity"] = {"blend": blend, "applied": True}
    else:
        report["risk_parity"] = {"blend": 0.0, "applied": False}
    contributions = _risk_contributions(result)
    total_contributions = float(np.nansum(contributions))
    if total_contributions > 0 and live_count > 0:
        shares = contributions / total_contributions
        report["risk_shares_before_deleverage"] = {
            str(frame["stock_code"].iloc[index]): round(float(shares[index]), 4)
            for index in np.flatnonzero(live)
        }
        report["risk_share_max"] = round(float(np.nanmax(shares[live])), 4)

    # Market-state de-leverage: when the book's own turnover state is stretched
    # (market-wide speculation), cut the whole book rather than one name.
    scale = 1.0
    market = cfg.market_deleverage or {}
    if market:
        column = str(market.get("column", "tr_zscore_20"))
        series = frame[column] if column in frame.columns else None
        if series is not None:
            if isinstance(series, pd.DataFrame):
                series = series.iloc[:, 0]
            values = pd.to_numeric(series, errors="coerce")
            state = float(values.median()) if values.notna().any() else float("nan")
            threshold = market.get("threshold")
            if np.isfinite(state) and threshold is not None and state > float(threshold):
                slope = float(market.get("slope", 0.3) or 0.3)
                floor = float(market.get("floor", 0.6) or 0.6)
                scale = max(floor, 1.0 - slope * (state - float(threshold)))
                result = result * scale
                report["enforced"] = True
            report["market_state"] = {"column": column, "value": None if not np.isfinite(state) else round(state, 4),
                                      "threshold": threshold, "scale": round(float(scale), 4)}

    if cfg.target_volatility is not None and float(cfg.target_volatility) > 0:
        portfolio_vol = float(np.sqrt(max(_variance(result), 0.0)))
        mode = str(getattr(cfg, "vol_target_mode", "cap") or "cap").lower()
        gross_cap = float(getattr(cfg, "max_gross_exposure", None) or 0.0)
        if portfolio_vol > float(cfg.target_volatility):
            scale = scale * (float(cfg.target_volatility) / portfolio_vol)
            result = result * scale
            report["enforced"] = True
        elif mode == "budget" and portfolio_vol > 0:
            # Risk budget instead of a hard ceiling: deploy the unused volatility
            # headroom, bounded by the gross ceiling and the per-name cap.
            headroom = float(cfg.target_volatility) / portfolio_vol
            gross_now = float(result.sum())
            if gross_cap > 0 and gross_now > 0:
                headroom = min(headroom, gross_cap / gross_now)
            max_weight = float(getattr(cfg, "max_weight", 0.0) or 0.0)
            if max_weight > 0:
                live_weights = result[result > 0]
                if live_weights.size:
                    headroom = min(headroom, float(max_weight) / float(live_weights.max()))
            if headroom > 1.0 + 1e-9:
                result = result * headroom
                scale = scale * headroom
                report["budget_deployed"] = True
                report["enforced"] = True
    report["scale"] = round(float(scale), 6)
    report["gross_after"] = round(float(result.sum()), 6)
    report["vol_after"] = round(float(np.sqrt(max(_variance(result), 0.0))), 6)
    report["capped_names"] = sorted({str(frame["stock_code"].iloc[index]) for index in report["capped_names"]})
    return result, report
