"""Causal long-only shadow sizing, preserving names and covariance correlation."""

from __future__ import annotations

import numpy as np
import pandas as pd

from factor_engine.portfolio.volatility import forecast_ewma_volatility


def predicted_covariance(returns: pd.DataFrame, *, lookback: int = 60, min_periods: int = 20) -> tuple[pd.DataFrame, pd.Series]:
    """Caller supplies returns through close t. Pairwise deletion is avoided.

    Historical correlation uses a common observation set (hence PSD), while
    each forecast uses its own known return history. Missing recent observations cause the decision to fail
    closed (cash in the shadow runner), not silently drop a selected name.
    """
    if returns.empty or returns.columns.has_duplicates:
        raise ValueError("risk returns need unique selected names")
    if not isinstance(returns.index, pd.DatetimeIndex) or not returns.index.is_unique or not returns.index.is_monotonic_increasing:
        raise ValueError("risk dates must be sorted and unique")
    recent = returns.tail(lookback)
    common = recent.dropna(how="any")
    if len(common) < min_periods or not recent.tail(min_periods).notna().all().all():
        raise ValueError("insufficient common recent returns")
    historical = common.std(ddof=1) * np.sqrt(252)
    if not np.isfinite(historical).all() or (historical <= 0).any():
        raise ValueError("invalid historical volatility")
    forecasts = pd.Series({code: forecast_ewma_volatility(returns[code]).iloc[-1].forecast_annual_vol_1d for code in returns})
    if not np.isfinite(forecasts).all() or (forecasts <= 0).any():
        raise ValueError("insufficient forecast volatility")
    covariance = common.corr().mul(forecasts, axis=0).mul(forecasts, axis=1)
    return covariance, historical


def cap_weights(weights: pd.Series, covariance: pd.DataFrame, *, target_vol: float = .15) -> pd.Series:
    """Only scale to cash; relative weights and rank are unchanged; no leverage."""
    if not np.isfinite(target_vol) or target_vol <= 0:
        raise ValueError("target_vol must be positive")
    if not np.isfinite(weights).all() or (weights < 0).any() or weights.sum() > 1.000001:
        raise ValueError("invalid long-only weights")
    c = covariance.loc[weights.index, weights.index].to_numpy()
    vol = float(np.sqrt(max(0, weights.to_numpy() @ c @ weights.to_numpy())))
    return weights * min(1.0, target_vol / vol) if vol else weights.copy()


def risk_contributions(weights: pd.Series, covariance: pd.DataFrame) -> pd.DataFrame:
    c = covariance.loc[weights.index, weights.index].to_numpy()
    w = weights.to_numpy()
    vol = float(np.sqrt(max(0, w @ c @ w)))
    marginal = c @ w / vol if vol else np.zeros(len(w))
    return pd.DataFrame({"stock_code": weights.index, "weight": w, "marginal_vol": marginal,
                         "component_vol": w * marginal, "portfolio_vol": vol})
