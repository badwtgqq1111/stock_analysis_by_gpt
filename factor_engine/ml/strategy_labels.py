"""Point-in-time daily-bar labels for CN startup and path strategies.

The price features in this module are observable at the decision close. Path
labels deliberately start at the next session so they describe an executable
entry rather than rewarding a same-day close known only afterwards.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _forward_window(values: pd.Series, *, delay: int, horizon: int, reducer: str) -> pd.Series:
    """Return a forward rolling max/min without using future rows at t."""
    shifted = values.shift(-int(delay))
    reverse = shifted.iloc[::-1]
    rolling = reverse.rolling(int(horizon), min_periods=int(horizon))
    return getattr(rolling, reducer)().iloc[::-1]


def _first_barrier_class(
    high: np.ndarray,
    low: np.ndarray,
    entry: np.ndarray,
    *,
    delay: int,
    horizon: int,
    take_profit: float,
    stop_loss: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Vectorise first-touch labels for one security."""
    size = len(entry)
    tp_first = np.full(size, np.inf, dtype=float)
    sl_first = np.full(size, np.inf, dtype=float)
    valid_entry = np.isfinite(entry) & (entry > 0)
    mature = np.arange(size) + int(delay) + int(horizon) <= size
    valid_entry &= mature
    for step in range(int(horizon)):
        offset = int(delay) + step
        if offset >= size:
            break
        active = valid_entry[: size - offset]
        if not active.any():
            continue
        entry_slice = entry[: size - offset]
        high_slice = high[offset:]
        low_slice = low[offset:]
        tp_hit = active & np.isfinite(high_slice) & (high_slice >= entry_slice * (1.0 + take_profit))
        sl_hit = active & np.isfinite(low_slice) & (low_slice <= entry_slice * (1.0 - stop_loss))
        tp_prefix = tp_first[: size - offset]
        sl_prefix = sl_first[: size - offset]
        tp_prefix[tp_hit & ~np.isfinite(tp_prefix)] = float(step)
        sl_prefix[sl_hit & ~np.isfinite(sl_prefix)] = float(step)
    classes = np.zeros(size, dtype=float)
    take = np.isfinite(tp_first) & ((~np.isfinite(sl_first)) | (tp_first < sl_first))
    stop = np.isfinite(sl_first) & ((~np.isfinite(tp_first)) | (sl_first < tp_first))
    classes[take] = 1.0
    classes[stop] = -1.0
    classes[~valid_entry] = np.nan
    hold_days = np.minimum(tp_first, sl_first) + 1.0
    hold_days[~np.isfinite(hold_days)] = float(horizon)
    hold_days[~valid_entry] = np.nan
    barrier_return = np.full(size, np.nan, dtype=float)
    barrier_return[take] = float(take_profit)
    barrier_return[stop] = -float(stop_loss)
    barrier_return[(~take) & (~stop) & valid_entry] = 0.0
    return classes, barrier_return, hold_days


def apply_startup_gate(labels: pd.DataFrame, settings: dict | None = None) -> pd.DataFrame:
    """Return ``(stock_code, startup_eligible)`` for a decision date's labels.

    The gate answers "may this name be bought as a half-year-low startup".  Two
    modes exist because the built-in eligibility rule couples three conditions:

    ``eligibility``
        use ``startup_price_eligible`` as computed by
        :func:`build_cn_strategy_labels` (dist-from-low band + return cap +
        distance from the 60-day high).
    ``thresholds``
        ignore the built-in flag and apply only the explicit caps below, which
        lets a run keep, say, the low-distance rule without the high-distance
        rule.

    A ``thresholds`` (or ``both``) mode applies every cap that is not ``None``.
    """
    settings = dict(settings or {})
    mode = str(settings.get("mode", "eligibility") or "eligibility").strip().lower()
    required = {"stock_code", "startup_price_eligible"}
    if labels is None or labels.empty or not {"stock_code"}.issubset(labels.columns):
        return pd.DataFrame(columns=["stock_code", "startup_eligible"])
    eligible = pd.Series(True, index=labels.index)
    used: list[str] = []
    if mode in {"eligibility", "both"}:
        if "startup_price_eligible" not in labels.columns:
            raise ValueError("startup gate mode 'eligibility' needs a startup_price_eligible column")
        eligible &= labels["startup_price_eligible"].fillna(False).astype(bool)
        used.append("startup_price_eligible")
    tier = pd.Series("core", index=labels.index)
    if mode in {"thresholds", "both"}:
        caps = (
            ("dist_from_120d_low", settings.get("max_dist_from_120d_low"), "le"),
            ("return_60d", settings.get("max_return_60d"), "le"),
            ("dist_from_60d_high", settings.get("max_dist_from_60d_high"), "le"),
        )
        applied = False
        for column, cap, direction in caps:
            if cap is None:
                continue
            if column not in labels.columns:
                raise ValueError(f"startup gate cap needs a {column} column")
            applied = True
            used.append(column)
            values = pd.to_numeric(labels[column], errors="coerce")
            eligible &= values.le(float(cap)) if direction == "le" else values.ge(float(cap))
        if not applied:
            raise ValueError("startup gate mode 'thresholds' requires at least one cap")
    if bool(settings.get("second_tier_enabled", False)):
        # Second tier: names that already left the half-year-low band but are in a
        # quiet pullback with flow support (P1.17 section 0.16: both case studies
        # had their best entry at the end of a shrinking-volume pullback).
        required = ["dist_from_120d_low"]
        missing = [column for column in required if column not in labels.columns]
        if missing:
            raise ValueError(f"second tier needs columns: {','.join(missing)}")
        low = pd.to_numeric(labels["dist_from_120d_low"], errors="coerce")
        second = low.gt(float(settings.get("second_tier_min_dist_from_120d_low", 0.30))) & low.le(
            float(settings.get("second_tier_max_dist_from_120d_low", 0.55))
        )
        if "volume_ratio" in labels.columns:
            ratio = pd.to_numeric(labels["volume_ratio"], errors="coerce")
            second &= ratio.lt(float(settings.get("second_tier_volume_ratio_max", 1.0)))
        if bool(settings.get("second_tier_requires_flow", True)) and "flow_z" in labels.columns:
            flow = pd.to_numeric(labels["flow_z"], errors="coerce")
            second &= flow.ge(float(settings.get("second_tier_flow_z_min", 0.0)))
        tier = tier.where(~second, "second")
        eligible = eligible | second
    return pd.DataFrame({
        "stock_code": labels["stock_code"].astype(str),
        "startup_eligible": eligible,
        "selection_tier": tier,
    })


def build_cn_strategy_labels(
    ohlcv: pd.DataFrame,
    *,
    limit_up_threshold=0.095,
    entry_delay: int = 1,
    path_horizon: int = 60,
    exec_horizons: tuple | list = (5, 10, 20, 60),
    excess_benchmark: str = "market_equal_weight",
    startup_low_window: int = 120,
    startup_min_rise: float = 0.05,
    startup_max_rise: float = 0.30,
    startup_max_return_60d: float = 0.35,
    startup_max_high_distance: float = -0.05,
    take_profit: float = 0.20,
    stop_loss: float = 0.10,
) -> pd.DataFrame:
    """Produce compatible strategy labels plus startup/path labels.

    ``startup_price_eligible`` is intentionally price-only. Money-flow
    activity is joined at panel materialization time and remains a separate
    gate/feature rather than being fabricated from OHLCV.

    Executable returns follow the same T+1 open entry as the path labels:
    ``forward_exec_return_{h}d`` buys the next session's open and marks the
    position at the close ``h`` sessions later. ``forward_excess_return_{h}d``
    subtracts the cross-sectional equal-weight benchmark of the same date, so
    the value is a within-date relative target rather than a market-beta bet.
    The default benchmark is ``market_equal_weight`` because the repository has
    no PIT-valid historical industry mapping (see P1.17 section 1.4); pass
    ``excess_benchmark="none"`` to skip the excess columns.
    """
    required = {"stock_code", "trade_date", "close"}
    if ohlcv is None or ohlcv.empty or required - set(ohlcv.columns):
        return pd.DataFrame()
    frame = ohlcv.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["stock_code", "trade_date", "close"]).sort_values(
        ["stock_code", "trade_date"], kind="stable"
    )
    for column in ("open", "high", "low"):
        if column not in frame.columns:
            frame[column] = frame["close"]
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(frame["close"])

    group = frame.groupby("stock_code", sort=False)
    frame["return_1d"] = group["close"].pct_change()
    frame["return_60d"] = group["close"].pct_change(60)
    frame["ma20"] = group["close"].transform(lambda value: value.rolling(20, min_periods=20).mean())
    frame["ma60"] = group["close"].transform(lambda value: value.rolling(60, min_periods=60).mean())
    frame["drawdown_60d"] = group["close"].transform(
        lambda value: value / value.rolling(60, min_periods=20).max() - 1.0
    )
    frame["dist_from_120d_low"] = group["low"].transform(
        lambda value: frame.loc[value.index, "close"] / value.rolling(
            int(startup_low_window), min_periods=min(int(startup_low_window), 60)
        ).min() - 1.0
    )
    frame["dist_from_60d_high"] = group["high"].transform(
        lambda value: frame.loc[value.index, "close"] / value.rolling(60, min_periods=20).max() - 1.0
    )
    frame["startup_price_eligible"] = (
        frame["dist_from_120d_low"].between(float(startup_min_rise), float(startup_max_rise), inclusive="both")
        & frame["return_60d"].le(float(startup_max_return_60d))
        & frame["dist_from_60d_high"].le(float(startup_max_high_distance))
    ).fillna(False)
    frame["bottom_rebound_candidate"] = (
        (frame["drawdown_60d"] <= -0.20) & (frame["close"] > frame["ma20"]) & (frame["return_1d"] > 0)
    )
    frame["trend_following_candidate"] = (frame["close"] > frame["ma20"]) & (frame["ma20"] > frame["ma60"])
    frame["limit_up_proxy"] = frame["return_1d"] >= float(limit_up_threshold)
    frame["board_count"] = group["limit_up_proxy"].transform(
        lambda value: value.astype(int).groupby((~value).cumsum()).cumsum()
    )
    frame["first_board_candidate"] = frame["limit_up_proxy"] & (frame["board_count"] == 1)
    frame["second_board_candidate"] = frame["limit_up_proxy"] & (frame["board_count"] == 2)

    path_horizon = max(1, int(path_horizon))
    entry_delay = max(1, int(entry_delay))
    frame["entry_price"] = group["open"].shift(-entry_delay)
    for horizon in (20, 40, 60):
        frame[f"label_mfe_{horizon}d"] = np.nan
        frame[f"label_mae_{horizon}d"] = np.nan
    frame["label_tb_class"] = np.nan
    frame["label_tb_ret"] = np.nan
    frame["label_tb_hold_days"] = np.nan

    for _, indices in frame.groupby("stock_code", sort=False).groups.items():
        idx = list(indices)
        entry = frame.loc[idx, "entry_price"].to_numpy(dtype=float)
        high = frame.loc[idx, "high"].to_numpy(dtype=float)
        low = frame.loc[idx, "low"].to_numpy(dtype=float)
        for horizon in (20, 40, 60):
            if horizon > path_horizon:
                continue
            future_high = _forward_window(pd.Series(high, index=idx), delay=entry_delay, horizon=horizon, reducer="max")
            future_low = _forward_window(pd.Series(low, index=idx), delay=entry_delay, horizon=horizon, reducer="min")
            future_high_values = future_high.to_numpy(dtype=float)
            future_low_values = future_low.to_numpy(dtype=float)
            valid_entry = np.isfinite(entry) & (entry > 0)
            mfe = np.full(len(entry), np.nan, dtype=float)
            mae = np.full(len(entry), np.nan, dtype=float)
            np.divide(future_high_values, entry, out=mfe, where=valid_entry & np.isfinite(future_high_values))
            np.divide(future_low_values, entry, out=mae, where=valid_entry & np.isfinite(future_low_values))
            mfe[~(valid_entry & np.isfinite(future_high_values))] = np.nan
            mae[~(valid_entry & np.isfinite(future_low_values))] = np.nan
            frame.loc[idx, f"label_mfe_{horizon}d"] = mfe - 1.0
            frame.loc[idx, f"label_mae_{horizon}d"] = mae - 1.0
        classes, barrier_return, hold_days = _first_barrier_class(
            high, low, entry, delay=entry_delay, horizon=path_horizon,
            take_profit=float(take_profit), stop_loss=float(stop_loss),
        )
        frame.loc[idx, "label_tb_class"] = classes
        frame.loc[idx, "label_tb_ret"] = barrier_return
        frame.loc[idx, "label_tb_hold_days"] = hold_days

    frame["label_path_score_20d"] = frame["label_mfe_20d"] - frame["label_mae_20d"].abs()
    frame["label_path_score_40d"] = frame["label_mfe_40d"] - frame["label_mae_40d"].abs()
    frame["label_path_score_60d"] = frame["label_mfe_60d"] - frame["label_mae_60d"].abs()
    frame["label_mfe_mae_ratio"] = frame["label_mfe_60d"] / frame["label_mae_60d"].abs().clip(lower=0.01)
    frame["execution_ready"] = False

    # Executable forward returns. Entry is the same T+1 open the path labels use,
    # so the column is directly comparable with the triple-barrier outcome.
    horizons = sorted({int(horizon) for horizon in (exec_horizons or ()) if int(horizon) > 0})
    benchmark_mode = str(excess_benchmark or "none").strip().lower()
    entry_series = pd.to_numeric(frame["entry_price"], errors="coerce")
    entry_series = entry_series.where(entry_series > 0)
    exec_columns: list[str] = []
    excess_columns: list[str] = []
    for horizon in horizons:
        exit_close = group["close"].shift(-(entry_delay + horizon))
        exec_column = f"forward_exec_return_{horizon}d"
        frame[exec_column] = exit_close / entry_series - 1.0
        exec_columns.append(exec_column)
        if benchmark_mode in {"market", "market_equal_weight", "equal_weight"}:
            excess_column = f"forward_excess_return_{horizon}d"
            benchmark_return = frame.groupby("trade_date")[exec_column].transform("mean")
            frame[excess_column] = frame[exec_column] - benchmark_return
            excess_columns.append(excess_column)

    frame = frame.drop(columns=["entry_price"])
    ordered = [
        "stock_code", "trade_date", "bottom_rebound_candidate", "trend_following_candidate",
        "first_board_candidate", "second_board_candidate", "limit_up_proxy", "board_count",
        "execution_ready", "startup_price_eligible", "dist_from_120d_low", "dist_from_60d_high",
        "return_60d", "label_mfe_20d", "label_mae_20d", "label_mfe_40d", "label_mae_40d",
        "label_mfe_60d", "label_mae_60d", "label_path_score_20d", "label_path_score_40d",
        "label_path_score_60d", "label_mfe_mae_ratio", "label_tb_class", "label_tb_ret",
        "label_tb_hold_days",
    ]
    ordered.extend(exec_columns)
    ordered.extend(excess_columns)
    return frame[ordered]
