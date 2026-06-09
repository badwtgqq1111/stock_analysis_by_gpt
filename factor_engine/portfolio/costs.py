"""Cost-aware portfolio helpers and simulated TCA.

These helpers intentionally use simple, inspectable formulas.  They are not a
broker-grade impact model; they provide the deterministic cost-aware baseline
needed before portfolio or execution RL experiments are meaningful.
"""

from __future__ import annotations

import math
from typing import Iterable

import numpy as np
import pandas as pd


def _float_or_nan(value) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return result if np.isfinite(result) else float("nan")


def estimate_row_transaction_cost(row: dict, *, target_weight: float = 0.0, initial_capital: float = 100000.0) -> dict:
    """Estimate capacity, slippage and impact from row-level liquidity fields.

    Expected input fields are optional.  Missing ADV/turnover falls back to a
    conservative cost so low-coverage names do not look free to trade.
    """
    weight = max(_float_or_nan(target_weight), 0.0)
    capital = max(_float_or_nan(initial_capital), 1.0)
    order_value = capital * weight
    turnover_20d = _float_or_nan(row.get("median_turnover_amount_20d"))
    if not np.isfinite(turnover_20d):
        turnover_20d = _float_or_nan(row.get("turnover_20d"))
    if not np.isfinite(turnover_20d):
        turnover_20d = _float_or_nan(row.get("avg_turnover_amount_20d"))
    if not np.isfinite(turnover_20d):
        turnover_20d = _float_or_nan(row.get("daily_turnover"))

    volatility = _float_or_nan(row.get("recent_volatility"))
    if not np.isfinite(volatility):
        volatility = _float_or_nan(row.get("volatility_20d"))
    if not np.isfinite(volatility):
        volatility = 0.30
    volatility = float(np.clip(volatility, 0.05, 1.50))

    spread_proxy_bps = _float_or_nan(row.get("spread_proxy_bps"))
    if not np.isfinite(spread_proxy_bps):
        market_cap = _float_or_nan(row.get("market_cap"))
        if np.isfinite(market_cap) and market_cap > 0:
            spread_proxy_bps = float(np.clip(80.0 / math.sqrt(max(market_cap, 1.0)), 3.0, 80.0))
        else:
            spread_proxy_bps = 35.0

    if np.isfinite(turnover_20d) and turnover_20d > 0:
        order_size_to_adv = order_value / turnover_20d
        liquidity_capacity_value = turnover_20d * 0.05
        liquidity_capacity_score = float(np.clip(100.0 - order_size_to_adv * 450.0, 0.0, 100.0))
        missing_liquidity = False
    else:
        order_size_to_adv = float("nan")
        liquidity_capacity_value = 0.0
        liquidity_capacity_score = 35.0
        missing_liquidity = True

    participation = order_size_to_adv if np.isfinite(order_size_to_adv) else 0.10
    expected_slippage_bps = float(spread_proxy_bps * 0.5 + volatility * 18.0)
    expected_impact_bps = float(4.0 + 85.0 * math.sqrt(max(participation, 0.0)) * volatility)
    expected_cost_bps = expected_slippage_bps + expected_impact_bps

    return {
        "adv_20d": turnover_20d if np.isfinite(turnover_20d) else None,
        "order_value": round(float(order_value), 4),
        "order_size_to_adv": round(float(order_size_to_adv), 6) if np.isfinite(order_size_to_adv) else None,
        "spread_proxy_bps": round(float(spread_proxy_bps), 4),
        "volatility_20d": round(float(volatility), 6),
        "expected_slippage_bps": round(float(expected_slippage_bps), 4),
        "expected_impact_bps": round(float(expected_impact_bps), 4),
        "expected_transaction_cost_bps": round(float(expected_cost_bps), 4),
        "liquidity_capacity_value": round(float(liquidity_capacity_value), 4),
        "liquidity_capacity_score": round(float(liquidity_capacity_score), 4),
        "missing_liquidity_inputs": missing_liquidity,
    }


def apply_cost_adjusted_scores(rows: Iterable[dict], *, initial_capital: float = 100000.0, cost_penalty_scale: float = 0.25) -> list[dict]:
    """Attach cost estimates and cost-adjusted ranking scores to rows."""
    source_rows = list(rows or [])
    fallback_weight = min(0.08, 1.0 / max(len(source_rows), 1))
    output = []
    for row in source_rows:
        item = dict(row)
        weight = _float_or_nan(item.get("portfolio_weight"))
        if not np.isfinite(weight) or weight <= 0:
            weight = fallback_weight
        cost = estimate_row_transaction_cost(item, target_weight=weight, initial_capital=initial_capital)
        item.update(cost)
        base_score = _float_or_nan(item.get("ranking_score"))
        if not np.isfinite(base_score):
            base_score = 0.0
        item["cost_adjusted_ranking_score"] = round(
            float(base_score - cost["expected_transaction_cost_bps"] * cost_penalty_scale / 10.0),
            6,
        )
        output.append(item)
    return output


def build_liquidity_capacity_report(rows: Iterable[dict], *, initial_capital: float = 100000.0) -> list[dict]:
    report = []
    for row in rows or []:
        item = dict(row)
        weight = _float_or_nan(item.get("portfolio_weight"))
        if not np.isfinite(weight):
            weight = 0.0
        cost = estimate_row_transaction_cost(item, target_weight=weight, initial_capital=initial_capital)
        report.append({
            "stock_code": item.get("stock_code"),
            "selected": bool(item.get("selected", False)),
            "portfolio_weight": weight,
            "allocated_capital": _float_or_nan(item.get("allocated_capital")),
            "ranking_score": item.get("ranking_score"),
            "cost_adjusted_ranking_score": item.get("cost_adjusted_ranking_score"),
            **cost,
        })
    return report


def build_simulated_tca_report(trades: Iterable[dict], *, arrival_price_field: str = "price") -> list[dict]:
    """Build a simulated TCA report from replay trades.

    The current replay only knows executed prices and configured slippage, so
    arrival price is approximated by the trade price unless callers provide a
    richer field.  This keeps the report explicit about simulated provenance.
    """
    rows = []
    for idx, trade in enumerate(trades or [], start=1):
        item = trade.to_dict() if hasattr(trade, "to_dict") else dict(trade)
        price = _float_or_nan(item.get("price"))
        arrival = _float_or_nan(item.get(arrival_price_field))
        if not np.isfinite(arrival):
            arrival = price
        side = str(item.get("type") or "").lower()
        amount = _float_or_nan(item.get("gross_amount"))
        commission = _float_or_nan(item.get("commission"))
        slippage_bps = 0.0
        if np.isfinite(price) and np.isfinite(arrival) and arrival > 0:
            direction = 1.0 if side == "buy" else -1.0
            slippage_bps = (price / arrival - 1.0) * 10000.0 * direction
        commission_bps = commission / amount * 10000.0 if np.isfinite(amount) and amount > 0 and np.isfinite(commission) else 0.0
        rows.append({
            "order_id": f"SIM-{idx:06d}",
            "stock_code": item.get("stock_code"),
            "side": side,
            "decision_time": item.get("signal_date"),
            "fill_time": item.get("date"),
            "arrival_price": round(float(arrival), 6) if np.isfinite(arrival) else None,
            "fill_price": round(float(price), 6) if np.isfinite(price) else None,
            "filled_qty": item.get("shares"),
            "gross_amount": amount if np.isfinite(amount) else None,
            "commission": commission if np.isfinite(commission) else None,
            "slippage_bps": round(float(slippage_bps), 4),
            "commission_bps": round(float(commission_bps), 4),
            "implementation_shortfall_bps": round(float(slippage_bps + commission_bps), 4),
            "simulated": True,
        })
    return rows


def summarize_tca(rows: Iterable[dict]) -> dict:
    frame = pd.DataFrame(list(rows or []))
    if frame.empty:
        return {"trade_count": 0, "avg_implementation_shortfall_bps": None}
    values = pd.to_numeric(frame.get("implementation_shortfall_bps"), errors="coerce").dropna()
    return {
        "trade_count": int(len(frame)),
        "avg_implementation_shortfall_bps": round(float(values.mean()), 4) if not values.empty else None,
        "p90_implementation_shortfall_bps": round(float(values.quantile(0.90)), 4) if not values.empty else None,
    }


class SupervisedExecutionCostModel:
    """Small ridge-regression cost model for execution research."""

    def __init__(self, feature_columns: list[str], coef: np.ndarray, intercept: float = 0.0):
        self.feature_columns = list(feature_columns)
        self.coef = np.asarray(coef, dtype=float)
        self.intercept = float(intercept)

    @classmethod
    def fit(cls, tca_rows: Iterable[dict], feature_columns: list[str], *, target_col: str = "implementation_shortfall_bps", ridge: float = 1e-3):
        frame = pd.DataFrame(list(tca_rows or []))
        if frame.empty:
            return cls(feature_columns, np.zeros(len(feature_columns)), 0.0)
        x = frame.reindex(columns=feature_columns).replace([np.inf, -np.inf], np.nan).fillna(0.0).to_numpy(dtype=float)
        y = pd.to_numeric(frame.get(target_col), errors="coerce").fillna(0.0).to_numpy(dtype=float)
        x_aug = np.column_stack([np.ones(len(x)), x])
        reg = np.eye(x_aug.shape[1]) * float(ridge)
        reg[0, 0] = 0.0
        beta = np.linalg.pinv(x_aug.T @ x_aug + reg) @ x_aug.T @ y
        return cls(feature_columns, beta[1:], beta[0])

    def predict(self, rows: Iterable[dict] | pd.DataFrame) -> np.ndarray:
        frame = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(list(rows or []))
        if frame.empty:
            return np.array([], dtype=float)
        x = frame.reindex(columns=self.feature_columns).replace([np.inf, -np.inf], np.nan).fillna(0.0).to_numpy(dtype=float)
        return x @ self.coef + self.intercept
