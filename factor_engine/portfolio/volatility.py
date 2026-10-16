"""Point-in-time risk forecasts for research/shadow portfolios (no routing)."""

from __future__ import annotations

from numbers import Integral

import numpy as np
import pandas as pd


def forecast_ewma_volatility(
    returns: pd.Series,
    *,
    decay: float = 0.94,
    min_periods: int = 20,
    horizons: tuple[int, ...] = (1, 5, 20),
    annualization: int = 252,
) -> pd.DataFrame:
    """At each session close t, forecast risk for sessions t+1 ... t+h.

    Input is a sorted, unique date-indexed Series of decimal simple returns.
    Initialize with the mean squared return over the first ``min_periods``
    consecutive observations (zero conditional mean); thereafter update with
    today's squared return. NaN resets the seed and emits insufficient data,
    rather than imputing a missing session as a zero return.

    EWMA has no mean reversion: all future daily variance forecasts equal the
    latest estimate. Annualized vol is therefore flat across horizons, while
    cumulative horizon vol scales by sqrt(h). These are risk forecasts, not
    predicted price returns. A t-close forecast must not size a t-open trade.
    """
    if not isinstance(returns.index, pd.DatetimeIndex):
        raise ValueError("returns must have a DatetimeIndex")
    if returns.index.hasnans or not returns.index.is_unique or not returns.index.is_monotonic_increasing:
        raise ValueError("return dates must be valid, unique and sorted")
    if not np.isfinite(decay) or not 0.0 < decay < 1.0:
        raise ValueError("decay must be between zero and one")
    if isinstance(min_periods, bool) or not isinstance(min_periods, Integral) or min_periods < 2:
        raise ValueError("min_periods must be an integer >= 2")
    if isinstance(annualization, bool) or not isinstance(annualization, Integral) or annualization < 1:
        raise ValueError("annualization must be a positive integer")
    if not horizons or any(isinstance(h, bool) or not isinstance(h, Integral) or h < 1 for h in horizons):
        raise ValueError("horizons must contain positive integers")
    if len(set(horizons)) != len(horizons):
        raise ValueError("horizons must be unique")
    values = pd.to_numeric(returns, errors="raise").to_numpy(dtype=float, na_value=np.nan)
    if np.isinf(values).any() or (values[np.isfinite(values)] < -1.0).any():
        raise ValueError("simple returns must be finite or NaN and >= -1")
    variance = np.full(len(values), np.nan)
    counts = np.zeros(len(values), dtype=int)
    count, seed, state = 0, 0.0, np.nan
    for i, value in enumerate(values):
        if np.isnan(value):
            count, seed, state = 0, 0.0, np.nan
            continue
        count += 1
        counts[i] = count
        if count <= min_periods:
            seed += value * value
            if count == min_periods:
                state = seed / min_periods
        else:
            state = decay * state + (1.0 - decay) * value * value
        variance[i] = state
    result = pd.DataFrame({
        "forecast_variance_1d": variance,
        "observation_count": counts,
        "status": np.where(np.isnan(variance), "insufficient", "ready"),
        "model_version": "ewma.v1",
        "decay": decay,
        "annualization": annualization,
    }, index=returns.index)
    for horizon in horizons:
        result[f"forecast_annual_vol_{horizon}d"] = np.sqrt(variance * annualization)
        result[f"forecast_horizon_vol_{horizon}d"] = np.sqrt(variance * horizon)
    return result
