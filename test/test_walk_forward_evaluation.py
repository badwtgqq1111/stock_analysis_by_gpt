#!/usr/bin/env python3
"""Tests for the overlap-corrected walk-forward evaluation (P1.17 11.B2)."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factor_engine.ml.walk_forward import (
    _annualize,
    _label_horizon,
    evaluate_walk_forward_predictions,
    write_walk_forward_report,
)


def _predictions(*, dates, stocks=40, horizon_days=20, industries=4, seed=0, signal=0.02):
    rng = np.random.default_rng(seed)
    rows = []
    for trade_date in dates:
        for index in range(stocks):
            score = rng.normal()
            rows.append(
                {
                    "trade_date": trade_date,
                    "stock_code": f"S{index:02d}",
                    "model_score": score,
                    f"forward_excess_return_{horizon_days}d": signal * score + rng.normal(0, 0.02),
                    "industry": f"IND{index % industries}",
                }
            )
    return pd.DataFrame(rows)


def _industry_map(stocks=40, industries=4):
    return {f"S{index:02d}": f"IND{index % industries}" for index in range(stocks)}


def test_overlap_correction_reports_nav_drawdown_and_cost_metadata():
    dates = pd.bdate_range("2026-01-05", periods=60)
    frame = _predictions(dates=dates, horizon_days=20)
    report, summary = evaluate_walk_forward_predictions(
        frame, target_col="forward_excess_return_20d", top_k=10,
        industry_map=_industry_map(), n_splits=2, min_train_days=20, purge_days=21,
    )
    assert (report["overlapping_forward_windows"]).all()
    assert set(report["overlap_correction"]) == {"split_1_over_h"}
    assert set(report["horizon_days"]) == {20}
    assert set(report["book_definition"]) == {"top_k"}
    assert report["round_trip_cost_bps"].iloc[0] == 25.0
    assert report["cumulative_top_return"].notna().all()
    assert report["max_drawdown"].notna().all()
    assert summary["max_drawdown"] is not None


def test_non_overlapping_windows_keep_plain_compounding():
    dates = pd.bdate_range("2026-01-05", periods=40, freq="30B")
    frame = _predictions(dates=dates, horizon_days=20)
    report, _ = evaluate_walk_forward_predictions(
        frame, target_col="forward_excess_return_20d", top_k=5,
        n_splits=2, min_train_days=5, purge_days=21, cost_bps=0.0,
    )
    assert not report["overlapping_forward_windows"].any()
    assert set(report["overlap_correction"]) == {"not_required"}
    assert report["cumulative_top_return"].notna().all()


def test_costs_reduce_the_net_nav_monotonically():
    dates = pd.bdate_range("2026-01-05", periods=60)
    frame = _predictions(dates=dates, horizon_days=20)
    results = {}
    for cost in (0.0, 25.0, 100.0):
        report, _ = evaluate_walk_forward_predictions(
            frame, target_col="forward_excess_return_20d", top_k=10,
            cost_bps=cost, n_splits=2, min_train_days=20, purge_days=21,
        )
        results[cost] = float(report["cumulative_top_return"].mean())
    assert results[0.0] > results[25.0] > results[100.0]


def test_industry_hhi_tracks_concentration():
    dates = pd.bdate_range("2026-01-05", periods=40)
    frame = _predictions(dates=dates, horizon_days=5, industries=1)
    _, summary_single = evaluate_walk_forward_predictions(
        frame, target_col="forward_excess_return_5d", top_k=10,
        industry_map=_industry_map(industries=1), n_splits=2, min_train_days=5, purge_days=6,
    )
    frame_many = _predictions(dates=dates, horizon_days=5, stocks=40, industries=40)
    _, summary_diverse = evaluate_walk_forward_predictions(
        frame_many, target_col="forward_excess_return_5d", top_k=10,
        industry_map=_industry_map(industries=40), n_splits=2, min_train_days=5, purge_days=6,
    )
    assert summary_single["industry_hhi"] == pytest.approx(1.0)
    assert summary_diverse["industry_hhi"] < 0.2


def test_multi_period_horizons_change_the_annualization():
    dates = pd.bdate_range("2026-01-05", periods=60)
    rows = []
    for horizon in (5, 20):
        frame = _predictions(dates=dates, horizon_days=horizon, seed=horizon)
        report, summary = evaluate_walk_forward_predictions(
            frame, target_col=f"forward_excess_return_{horizon}d", top_k=10,
            n_splits=2, min_train_days=20, purge_days=21,
        )
        rows.append({"horizon": horizon, "reported": int(report["horizon_days"].iloc[0]),
                     "annualized": summary["annualized_return"], "label_horizon": _label_horizon(f"forward_excess_return_{horizon}d")})
    table = pd.DataFrame(rows).set_index("horizon")
    assert table.loc[5, "reported"] == 5 and table.loc[20, "reported"] == 20
    assert table.loc[5, "label_horizon"] == 5
    assert table["annualized"].notna().all()


def test_annualize_scales_with_elapsed_sessions_and_never_extrapolates_short_windows():
    # A window shorter than a year reports its own cumulative return.
    assert _annualize(0.05, periods=10, horizon_days=20) == pytest.approx(0.05)
    # A multi-year window is scaled to one year.
    assert _annualize(0.10, periods=504, horizon_days=20) == pytest.approx((1.10 ** (252 / 504)) - 1)
    assert _annualize(None, periods=10, horizon_days=20) is None
    assert _annualize(0.1, periods=0, horizon_days=20) is None
    assert _annualize(-1.5, periods=504, horizon_days=20) == -1.0


def test_report_markdown_carries_the_new_columns(tmp_path):
    dates = pd.bdate_range("2026-01-05", periods=40)
    frame = _predictions(dates=dates, horizon_days=20)
    report, summary = evaluate_walk_forward_predictions(
        frame, target_col="forward_excess_return_20d", top_k=10, model_name="unit_model",
        industry_map=_industry_map(), n_splits=2, min_train_days=20, purge_days=21,
    )
    paths = write_walk_forward_report(report, {"models": [summary], "ranking": []}, output_dir=tmp_path, prefix="unit")
    markdown = Path(paths["markdown"]).read_text(encoding="utf-8")
    for token in ("Round-trip bps", "Annualized (net)", "Max drawdown", "Industry HHI", "Book"):
        assert token in markdown
