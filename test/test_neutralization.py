#!/usr/bin/env python3
"""Tests for cross-sectional neutralization and the W4 residual columns."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factor_engine.ml.neutralization import neutralize_features


def _frame(dates=5, rows=120, seed=3):
    rng = np.random.default_rng(seed)
    records = []
    for date in pd.date_range("2026-01-01", periods=dates):
        size = rng.normal(10.0, 1.0, rows)
        turnover = rng.normal(0.03, 0.01, rows)
        volatility = rng.normal(0.4, 0.05, rows)
        for index in range(rows):
            records.append(
                {
                    "trade_date": date,
                    "stock_code": f"S{index:03d}",
                    "log_market_cap": float(size[index]),
                    "turnover_rate": float(turnover[index]),
                    "tr_volatility_20": float(volatility[index]),
                    "moneyflow_net_z_5d": float(
                        0.7 * size[index] + 15.0 * turnover[index] + 2.0 * volatility[index] + rng.normal(0, 0.3)
                    ),
                    "price_position_52w_high": float(0.5 + 0.2 * size[index] + rng.normal(0, 0.1)),
                }
            )
    return pd.DataFrame(records)


def _per_date_corr(frame, left, right):
    return (
        frame.groupby("trade_date")
        .apply(lambda group: group[left].corr(group[right]), include_groups=False)
        .dropna()
    )


def test_residual_columns_are_orthogonal_to_controls_within_each_date():
    frame = _frame()
    out, meta = neutralize_features(
        frame,
        ["moneyflow_net_z_5d"],
        mode="industry_size",
        control_columns=["turnover_rate", "tr_volatility_20"],
        residual_suffix="_resid",
        compute_correlation_audit=True,
    )
    assert meta["residual_columns"] == ["moneyflow_net_z_5d_resid"]
    for control in ("turnover_rate", "tr_volatility_20", "log_market_cap"):
        correlations = _per_date_corr(out, "moneyflow_net_z_5d_resid", control)
        assert float(correlations.abs().max()) < 1e-6


def test_suffix_keeps_the_raw_column_and_reports_the_exposure_drop():
    frame = _frame()
    out, meta = neutralize_features(
        frame,
        ["moneyflow_net_z_5d"],
        mode="industry_size",
        control_columns=["turnover_rate", "tr_volatility_20"],
        residual_suffix="_resid",
        compute_correlation_audit=True,
    )
    assert "moneyflow_net_z_5d" in out.columns
    pd.testing.assert_series_equal(out["moneyflow_net_z_5d"], frame["moneyflow_net_z_5d"])
    audit = meta["correlation_audit"]["moneyflow_net_z_5d_resid"]
    assert audit["mean_before"] > 0.2
    assert audit["mean_after"] < 1e-6
    assert audit["controls"]["turnover_rate"]["before"] > audit["controls"]["turnover_rate"]["after"]


def test_without_suffix_the_feature_is_overwritten_in_place():
    frame = _frame()
    out, meta = neutralize_features(
        frame, ["moneyflow_net_z_5d"], mode="industry_size",
        control_columns=["turnover_rate"], residual_suffix=None,
    )
    assert meta["residual_columns"] == []
    assert not np.allclose(out["moneyflow_net_z_5d"].to_numpy(), frame["moneyflow_net_z_5d"].to_numpy())
    assert float(_per_date_corr(out, "moneyflow_net_z_5d", "turnover_rate").abs().max()) < 1e-6


def test_control_only_mode_works_without_industry_or_size_columns():
    frame = _frame().drop(columns=["log_market_cap"])
    out, meta = neutralize_features(
        frame, ["price_position_52w_high"], mode="industry_size",
        control_columns=["turnover_rate"], residual_suffix="_resid", compute_correlation_audit=True,
    )
    assert meta["residual_columns"] == ["price_position_52w_high_resid"]
    assert meta["size_column_used"] is None
    assert float(_per_date_corr(out, "price_position_52w_high_resid", "turnover_rate").abs().max()) < 1e-6


def test_missing_controls_are_reported_not_silently_dropped():
    frame = _frame()
    _, meta = neutralize_features(
        frame, ["moneyflow_net_z_5d"], mode="industry_size",
        control_columns=["turnover_rate", "does_not_exist"], residual_suffix="_resid",
    )
    assert meta["control_columns_used"] == ["turnover_rate"]
    assert meta["control_columns_missing"] == ["does_not_exist"]


def test_no_controls_at_all_is_a_no_op():
    frame = _frame().drop(columns=["log_market_cap"])
    out, meta = neutralize_features(frame, ["moneyflow_net_z_5d"], mode="industry_size")
    assert meta["features_neutralized"] == 0
    assert meta.get("warning") == "no_industry_size_or_control_columns"
    pd.testing.assert_frame_equal(out, frame)


def test_row_order_and_row_count_are_preserved():
    frame = _frame(dates=3, rows=40)
    out, _ = neutralize_features(
        frame, ["moneyflow_net_z_5d", "price_position_52w_high"], mode="industry_size",
        control_columns=["turnover_rate", "tr_volatility_20"], residual_suffix="_resid",
    )
    assert len(out) == len(frame)
    assert out["stock_code"].tolist() == frame["stock_code"].tolist()
    assert out["trade_date"].tolist() == frame["trade_date"].tolist()
