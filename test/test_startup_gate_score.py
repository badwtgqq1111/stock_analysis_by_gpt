#!/usr/bin/env python3
"""Tests for the P1.21 soft startup gate (``mode = "score"``)."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.ingest.service import filter_selection_universe  # noqa: E402
from factor_engine.ml.strategy_labels import apply_startup_gate, startup_gate_score  # noqa: E402
from factor_engine.ml.model_training import select_top_model_scores  # noqa: E402


def _labels(dist, high=None, ret60=None, volume=None, flow=None):
    frame = pd.DataFrame({
        "stock_code": [f"S{index:02d}" for index in range(len(dist))],
        "dist_from_120d_low": dist,
    })
    if high is not None:
        frame["dist_from_60d_high"] = high
    if ret60 is not None:
        frame["return_60d"] = ret60
    if volume is not None:
        frame["volume_ratio"] = volume
    if flow is not None:
        frame["flow_z"] = flow
    return frame


def test_score_peaks_inside_the_startup_band_and_decays_outside_it():
    labels = _labels(dist=[0.0, 0.05, 0.15, 0.30, 0.45, 0.60, 1.20])
    score = startup_gate_score(
        labels, {"score_weight_high": 0.0, "score_weight_volume": 0.0, "score_weight_flow": 0.0}
    )
    values = score.to_numpy(dtype=float)
    assert values[0] == pytest.approx(0.0)
    assert values[1] == pytest.approx(1.0)
    assert values[2] == pytest.approx(1.0)
    assert values[3] == pytest.approx(1.0)
    assert 0.0 < values[4] < 1.0          # decays through the middle band
    assert values[5] == pytest.approx(0.0)
    assert values[6] == pytest.approx(0.0)
    assert values[1] >= values[4] >= values[6]


def test_score_rewards_dry_volume_and_flow_and_is_neutral_when_flow_is_missing():
    base = _labels(dist=[0.15, 0.15], high=[-0.25, -0.25], volume=[0.6, 1.6])
    dry, wet = startup_gate_score(base, {}).to_numpy(dtype=float)
    assert dry > wet
    with_flow = _labels(dist=[0.15, 0.15], high=[-0.25, -0.25], volume=[0.6, 0.6], flow=[1.2, np.nan])
    confirmed, missing = startup_gate_score(with_flow, {}).to_numpy(dtype=float)
    assert confirmed > missing                      # missing flow = neutral, not a deletion
    assert 0.0 <= missing < 1.0


def test_score_mode_keeps_names_and_only_applies_the_hard_rails():
    labels = _labels(dist=[0.02, 0.15, 0.60, 1.40], high=[-0.30] * 4, ret60=[0.05, 0.10, 0.20, 0.90])
    gate = apply_startup_gate(labels, {
        "mode": "score", "hard_max_dist_from_120d_low": 1.0, "hard_max_return_60d": 0.60,
    })
    assert list(gate.columns) >= ["stock_code", "startup_eligible", "selection_tier", "gate_score"]
    # too far above the 120-day low AND up >60% in 60 sessions -> rail
    assert gate["startup_eligible"].tolist() == [True, True, True, False]
    assert gate["selection_tier"].unique().tolist() == ["scored"]
    # the out-of-band names keep a score instead of disappearing from the pool
    assert gate.loc[2, "gate_score"] > 0.0
    assert gate.loc[0, "gate_score"] > 0.0


def test_score_mode_without_rails_is_a_pure_tilt():
    labels = _labels(dist=[0.40, 0.15], high=[-0.30, -0.30], ret60=[0.90, 0.10])
    gate = apply_startup_gate(labels, {"mode": "score"})
    assert gate["startup_eligible"].all()


def test_select_top_model_scores_applies_a_centred_tilt_before_the_cut():
    frames = {"lightgbm": pd.DataFrame({
        "trade_date": pd.to_datetime(["2026-09-22"] * 3),
        "stock_code": ["A", "B", "C"],
        "model_score": [90.0, 89.0, 80.0],
    })}
    plain = select_top_model_scores(frames, model="lightgbm", top_n=1, metadata={})
    tilted = select_top_model_scores(
        frames, model="lightgbm", top_n=1,
        score_adjust={"A": 0.0, "B": 1.0, "C": 0.5}, score_adjust_strength=0.10,
    )
    assert plain.iloc[0]["stock_code"] == "A"          # unchanged without a tilt
    assert tilted.iloc[0]["stock_code"] == "B"          # tilt lifts the in-band name
    assert {"gate_tilt", "model_score_pre_tilt"} <= set(tilted.columns)
    # tilt is centred on the full cross-section: mean(0, 1, 0.5) = 0.5, so
    # B = 89 + 100*0.10*(1 - 0.5) = 94 and its pre-tilt score is preserved.
    assert tilted.iloc[0]["model_score"] == pytest.approx(94.0)
    assert tilted.iloc[0]["model_score_pre_tilt"] == pytest.approx(89.0)
    assert tilted.iloc[0]["gate_tilt"] == pytest.approx(0.5)


def test_breakout_filter_degrades_to_keep_when_flow_coverage_is_broken():
    codes = ["A", "B"]
    breakout = {"A": (0.05, 2.0), "B": (0.05, 2.0)}
    settings = {"enabled": True, "exclude_st": False, "exclude_volume_breakout": True,
                "volume_breakout_ratio": 1.5, "volume_breakout_flow_z_min": 1.0,
                "volume_breakout_missing_flow": "drop",
                "volume_breakout_min_flow_coverage": 0.5}
    dead = filter_selection_universe(codes, names={}, median_amount={}, volume_breakout=breakout,
                                     flow_z={"A": None, "B": None}, flow_coverage=0.0, settings=settings)
    assert dead[0] == [] and dead[1]["volume_breakout_confirmation_skipped"] is True
    assert sorted(dead[1]["volume_breakout_missing_flow_kept"]) == ["A", "B"]
    alive = filter_selection_universe(codes, names={}, median_amount={}, volume_breakout=breakout,
                                      flow_z={"A": 1.4, "B": -0.2}, flow_coverage=1.0, settings=settings)
    assert alive[0] == ["B"] and "volume_breakout_confirmation_skipped" not in alive[1]
