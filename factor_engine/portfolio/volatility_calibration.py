"""Point-in-time rolling volatility comparison; never used for production orders."""

from __future__ import annotations

from time import perf_counter

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.stats import norm, t as student_t

from factor_engine.portfolio.volatility import forecast_ewma_volatility

MODELS = ("ewma", "garch_t", "gjr_garch_t")


def _filter_variance(x: np.ndarray, p: np.ndarray, asymmetric: bool) -> np.ndarray:
    omega, alpha, beta, gamma, _nu = p
    h = np.empty(len(x))
    h[0] = max(float(np.mean(x * x)), 1e-8)
    for i in range(1, len(x)):
        shock = x[i - 1] * x[i - 1]
        h[i] = omega + (alpha + (gamma if asymmetric and x[i - 1] < 0 else 0.0)) * shock + beta * h[i - 1]
    return h


def fit_student_garch(returns: np.ndarray, *, asymmetric: bool = False) -> dict:
    """Fit zero-mean standardized Student-t GARCH/GJR to decimal returns.

    Internal percent units improve conditioning; the returned variance is in
    decimal-return squared units. Caller must pass observations through t only.
    """
    x = np.asarray(returns, dtype=float) * 100.0
    if len(x) < 120 or not np.isfinite(x).all():
        raise ValueError("GARCH requires >=120 consecutive finite returns")
    v = max(float(np.mean(x * x)), 1e-6)
    start = np.array([v * .05, .06, .88, .04 if asymmetric else 0.0, 8.0])
    bounds = [(1e-8, 10 * v), (0, .5), (0, .995), (0, .5) if asymmetric else (0, 0), (2.05, 100)]

    def objective(p: np.ndarray) -> float:
        persistence = p[1] + p[2] + (p[3] / 2 if asymmetric else 0)
        if persistence >= .995:
            return 1e8 + 1e8 * (persistence - .995)
        h = _filter_variance(x, p, asymmetric)
        if not np.isfinite(h).all() or (h <= 0).any():
            return 1e9
        scale = np.sqrt((p[4] - 2) / p[4])
        logpdf = student_t.logpdf(x / np.sqrt(h) / scale, df=p[4]) - np.log(scale) - .5 * np.log(h)
        return -float(logpdf.sum())

    result = minimize(objective, start, method="SLSQP", bounds=bounds,
                      constraints=[{"type": "ineq", "fun": lambda p: .9949 - p[1] - p[2] - (p[3] / 2 if asymmetric else 0)}],
                      options={"maxiter": 150, "ftol": 1e-6})
    if not result.success or not np.isfinite(result.fun):
        raise ValueError(f"GARCH fit did not converge: {result.message}")
    p = result.x
    h = _filter_variance(x, p, asymmetric)
    next_h = p[0] + (p[1] + (p[3] if asymmetric and x[-1] < 0 else 0)) * x[-1] ** 2 + p[2] * h[-1]
    return {"parameters": p, "variance_1d": next_h / 10000, "persistence": p[1] + p[2] + p[3] / 2,
            "nu": p[4]}


def _horizon_variance(fit: dict, horizon: int) -> float:
    p = fit["parameters"]
    persistence = fit["persistence"]
    long_run = (p[0] / (1 - persistence)) / 10000
    first = fit["variance_1d"]
    return float(np.mean([long_run + persistence ** k * (first - long_run) for k in range(horizon)]))


def rolling_volatility_comparison(
    returns: pd.Series, *, window: int = 252, refit_every: int = 20,
    horizons: tuple[int, ...] = (1, 5, 20), decay: float = .94,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Forecast at close t, score only subsequent t+1..t+h returns.

    GARCH parameters are fit on a trailing window and cached until the next
    refit date. Conditional variance is re-filtered with known data each date.
    All model scores use the intersection of successfully forecast dates.
    """
    if window < 120 or refit_every < 1 or not horizons or any(h < 1 for h in horizons):
        raise ValueError("invalid window, refit interval or horizons")
    if not isinstance(returns.index, pd.DatetimeIndex) or not returns.index.is_monotonic_increasing or not returns.index.is_unique:
        raise ValueError("returns require sorted unique DatetimeIndex")
    vals = pd.to_numeric(returns, errors="raise").to_numpy(dtype=float)
    if not np.isfinite(vals).all() or (vals < -1).any():
        raise ValueError("comparison requires consecutive finite simple returns >= -1")
    begin = perf_counter()
    ewma = forecast_ewma_volatility(returns, decay=decay)
    ewma_seconds = perf_counter() - begin
    rows = []
    fits: dict[str, dict] = {}
    elapsed = {model: 0.0 for model in MODELS}
    elapsed["ewma"] = ewma_seconds
    failures = {model: 0 for model in MODELS}
    for i in range(window - 1, len(vals) - max(horizons)):
        day = returns.index[i]
        sample = vals[i - window + 1:i + 1]
        if (i - (window - 1)) % refit_every == 0:
            for model, asymmetric in (("garch_t", False), ("gjr_garch_t", True)):
                begin = perf_counter()
                try:
                    fits[model] = {**fit_student_garch(sample, asymmetric=asymmetric), "refit_date": day}
                except ValueError:
                    fits.pop(model, None)
                    failures[model] += 1
                elapsed[model] += perf_counter() - begin
        for horizon in horizons:
            realized_var = float(np.mean(vals[i + 1:i + 1 + horizon] ** 2))
            for model in MODELS:
                begin = perf_counter()
                try:
                    if model == "ewma":
                        predicted = float(ewma.iloc[i]["forecast_variance_1d"])
                        nu = np.nan
                    else:
                        # Use parameters last fit no later than t; filter the
                        # current trailing sample including today's return.
                        old = fits[model]
                        p = old["parameters"]
                        asym = model == "gjr_garch_t"
                        h = _filter_variance(sample * 100, p, asym)
                        updated = {**old, "variance_1d": (p[0] + (p[1] + (p[3] if asym and sample[-1] < 0 else 0)) *
                                    (sample[-1] * 100) ** 2 + p[2] * h[-1]) / 10000}
                        predicted = _horizon_variance(updated, horizon)
                        nu = old["nu"]
                    if not np.isfinite(predicted) or predicted <= 0:
                        continue
                    rows.append({"decision_date": day, "realized_end": returns.index[i + horizon],
                                 "horizon": horizon, "model": model, "forecast_variance": predicted,
                                 "realized_variance": realized_var, "next_return": vals[i + 1], "nu": nu,
                                 "persistence": fits[model]["persistence"] if model != "ewma" else 1.0,
                                 "refit_date": fits[model]["refit_date"] if model != "ewma" else day,
                                 "training_start": returns.index[i - window + 1]})
                except KeyError:
                    pass
                elapsed[model] += perf_counter() - begin
    forecasts = pd.DataFrame(rows)
    if forecasts.empty:
        return forecasts, pd.DataFrame()
    summary = []
    for horizon, group in forecasts.groupby("horizon"):
        common = group.groupby("decision_date")["model"].nunique()
        dates = common[common == len(MODELS)].index
        for model, sample in group[group.decision_date.isin(dates)].groupby("model"):
            v = sample.forecast_variance.to_numpy()
            actual = sample.realized_variance.to_numpy()
            qlike = actual / v + np.log(v)
            # 1-day coverage uses model-appropriate standardized t/normal.
            critical = (student_t.ppf(.975, sample.nu.to_numpy()) * np.sqrt((sample.nu.to_numpy() - 2) / sample.nu.to_numpy())
                        if model != "ewma" else np.full(len(sample), norm.ppf(.975)))
            coverage = float(np.mean(np.abs(sample.next_return.to_numpy()) <= critical * np.sqrt(v))) if horizon == 1 else np.nan
            summary.append({"model": model, "horizon": horizon, "n_common": len(sample),
                            "start": str(sample.decision_date.min().date()), "end": str(sample.decision_date.max().date()),
                            "qlike": float(np.mean(qlike)),
                            "mae_annual_vol": float(np.mean(np.abs(np.sqrt(v * 252) - np.sqrt(actual * 252)))),
                            "coverage_95_1d": coverage, "fit_and_forecast_seconds": round(elapsed[model], 3),
                            "fit_failures": failures[model]})
    return forecasts, pd.DataFrame(summary)
