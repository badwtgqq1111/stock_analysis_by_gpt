#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""信号 recipe 层测试。"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from factor_engine.signals import (
    BoxPullbackRecipe,
    LowPriceSetupRecipe,
    RangeBreakoutRecipe,
    SignalRecipeRunner,
    create_signal_recipe,
    list_signal_recipes,
    summarize_low_price_setup,
)
from analyzer_core import StockAnalyzer


def _make_setup_frames():
    dates = pd.date_range("2024-01-02", periods=90, freq="B")

    breakout_close = pd.Series(
        np.concatenate(
            [
                np.linspace(1.05, 1.18, 55),
                np.linspace(1.19, 1.22, 20),
                np.linspace(1.225, 1.245, 15),
            ]
        ),
        index=dates,
    )
    breakout_frame = pd.DataFrame(
        {
            "Open": breakout_close * 0.995,
            "Close": breakout_close,
            "High": breakout_close * 1.01,
            "Low": breakout_close * 0.99,
            "Volume": np.concatenate([np.full(75, 800_000.0), np.linspace(900_000.0, 1_800_000.0, 15)]),
        },
        index=dates,
    )

    rebound_close = pd.Series(
        np.concatenate(
            [
                np.linspace(2.8, 1.05, 60),
                np.linspace(1.00, 1.18, 15),
                np.linspace(1.20, 1.34, 15),
            ]
        ),
        index=dates,
    )
    rebound_frame = pd.DataFrame(
        {
            "Open": rebound_close * 0.99,
            "Close": rebound_close,
            "High": rebound_close * 1.02,
            "Low": rebound_close * 0.97,
            "Volume": np.concatenate([np.full(60, 650_000.0), np.linspace(900_000.0, 2_100_000.0, 30)]),
        },
        index=dates,
    )
    return breakout_frame, rebound_frame


def test_low_price_setup_recipe_identifies_breakout_and_bottom_rebound():
    breakout_frame, rebound_frame = _make_setup_frames()

    breakout_snapshot = LowPriceSetupRecipe().evaluate(breakout_frame).to_dict()
    rebound_snapshot = LowPriceSetupRecipe().evaluate(rebound_frame).to_dict()

    assert breakout_snapshot["setup_type"] == "pre_breakout"
    assert breakout_snapshot["setup_score"] > breakout_snapshot["sideways_penalty"]
    assert breakout_snapshot["recipe_scores"]["pre_breakout"] >= 55.0
    assert rebound_snapshot["setup_type"] == "bottom_rebound"
    assert rebound_snapshot["setup_score"] > rebound_snapshot["sideways_penalty"]
    assert rebound_snapshot["recipe_scores"]["bottom_rebound"] >= 52.0


def test_summarize_low_price_setup_keeps_legacy_snapshot_contract():
    breakout_frame, _ = _make_setup_frames()

    snapshot = summarize_low_price_setup(breakout_frame)

    expected_keys = {
        "setup_type",
        "setup_score",
        "sideways_penalty",
        "low_price_candidate",
        "liquidity_ok",
        "latest_turnover",
        "median_turnover_20",
        "distance_to_20d_high",
        "distance_from_60d_low",
        "volume_ratio_20",
        "compression_ratio",
        "return_5d",
        "return_20d",
        "return_60d",
        "recipe_scores",
    }
    assert expected_keys.issubset(snapshot.keys())
    assert snapshot["setup_type"] == "pre_breakout"
    assert snapshot["low_price_candidate"] is True


def test_signal_recipe_registry_and_runner_use_names():
    breakout_frame, _ = _make_setup_frames()

    assert "low_price_setup" in list_signal_recipes()
    recipe = create_signal_recipe("low_price_setup")
    runner = SignalRecipeRunner(["low_price_setup"])

    direct_snapshot = recipe.evaluate(breakout_frame).to_dict()
    runner_snapshot = runner.evaluate(breakout_frame)

    assert direct_snapshot["setup_type"] == "pre_breakout"
    assert runner_snapshot["setup_type"] == "pre_breakout"
    assert runner_snapshot["signal_recipe_names"] == ["low_price_setup"]


def test_range_breakout_recipe_identifies_volume_confirmed_breakout():
    dates = pd.date_range("2024-01-02", periods=90, freq="B")
    base = np.concatenate(
        [
            np.linspace(1.00, 1.12, 55),
            np.linspace(1.115, 1.135, 20),
            np.linspace(1.125, 1.138, 14),
            [1.185],
        ]
    )
    close = pd.Series(base, index=dates)
    frame = pd.DataFrame(
        {
            "Open": close * 0.992,
            "Close": close,
            "High": close * 1.008,
            "Low": close * 0.992,
            "Volume": np.concatenate([np.full(75, 1_000_000.0), np.full(14, 1_050_000.0), [2_200_000.0]]),
        },
        index=dates,
    )

    snapshot = RangeBreakoutRecipe().evaluate(frame).to_dict()

    assert snapshot["setup_type"] == "range_breakout"
    assert snapshot["setup_score"] >= 60.0
    assert snapshot["range_breakout_score"] == snapshot["setup_score"]
    assert snapshot["breakout_above_20d_high"] is True
    assert snapshot["volume_confirmed"] is True


def test_signal_recipe_runner_keeps_recipe_outputs_and_picks_stronger_setup():
    breakout_frame, _ = _make_setup_frames()

    snapshot = SignalRecipeRunner(["low_price_setup", "range_breakout"]).evaluate(breakout_frame)

    assert snapshot["signal_recipe_names"] == ["low_price_setup", "range_breakout"]
    assert set(snapshot["signal_recipe_outputs"]) == {"low_price_setup", "range_breakout"}
    assert snapshot["setup_type"] in {"pre_breakout", "range_breakout"}
    assert snapshot["setup_score"] == max(
        snapshot["signal_recipe_outputs"]["low_price_setup"]["setup_score"],
        snapshot["signal_recipe_outputs"]["range_breakout"]["setup_score"],
    )


def test_box_pullback_recipe_identifies_low_volume_retreat_to_box_top():
    dates = pd.date_range("2024-01-02", periods=90, freq="B")
    close_values = np.concatenate(
        [
            np.linspace(1.00, 1.10, 50),
            np.linspace(1.095, 1.12, 20),
            np.linspace(1.17, 1.23, 8),
            np.linspace(1.21, 1.125, 12),
        ]
    )
    close = pd.Series(close_values, index=dates)
    volume = np.concatenate(
        [
            np.full(70, 1_100_000.0),
            np.full(8, 1_900_000.0),
            np.full(12, 720_000.0),
        ]
    )
    frame = pd.DataFrame(
        {
            "Open": close * 1.002,
            "Close": close,
            "High": close * 1.008,
            "Low": close * 0.992,
            "Volume": volume,
        },
        index=dates,
    )

    snapshot = BoxPullbackRecipe().evaluate(frame).to_dict()

    assert snapshot["setup_type"] == "box_pullback"
    assert snapshot["setup_score"] >= 60.0
    assert snapshot["box_breakout_confirmed"] is True
    assert snapshot["pullback_holding_box_top"] is True
    assert snapshot["pullback_volume_dryup"] is True


def test_stock_analyzer_builds_signal_recipe_report():
    dates = pd.date_range("2024-01-02", periods=95, freq="B")
    close_values = np.concatenate(
        [
            np.linspace(1.00, 1.12, 55),
            np.linspace(1.115, 1.135, 20),
            np.linspace(1.125, 1.138, 14),
            [1.185],
            np.linspace(1.205, 1.28, 5),
        ]
    )
    close = pd.Series(close_values, index=dates)
    frame = pd.DataFrame(
        {
            "Open": close * 0.992,
            "Close": close,
            "High": close * 1.008,
            "Low": close * 0.992,
            "Volume": np.concatenate(
                [
                    np.full(75, 1_000_000.0),
                    np.full(14, 1_050_000.0),
                    [2_200_000.0],
                    np.full(5, 1_500_000.0),
                ]
            ),
        },
        index=dates,
    )

    original_load_stock_data = StockAnalyzer.load_stock_data
    original_get_all_stocks = StockAnalyzer.get_all_stocks

    def stub_load_stock_data(self, stock_code, days=365):
        return frame

    def stub_get_all_stocks(self):
        return ["00001"]

    StockAnalyzer.load_stock_data = stub_load_stock_data
    StockAnalyzer.get_all_stocks = stub_get_all_stocks
    try:
        analyzer = StockAnalyzer(db_dir="/tmp/signal_report_test")
        report = analyzer.build_signal_recipe_report(
            stock_codes=["00001"],
            days=90,
            signal_recipes=("range_breakout",),
            horizons=(1, 5),
            signal_cooldown_days=5,
        )
        analyzer.close()
    finally:
        StockAnalyzer.load_stock_data = original_load_stock_data
        StockAnalyzer.get_all_stocks = original_get_all_stocks

    summary = report["summary"]
    events = report["events"]
    events_raw = report["events_raw"]
    assert not summary.empty
    assert not events.empty
    assert len(events_raw) >= len(events)
    assert set(summary["recipe_name"]) == {"range_breakout"}
    assert "range_breakout" in set(events["setup_type"])
    assert "signal_zone_id" in events.columns
    assert "merged_signal_count" in events.columns
    assert summary.iloc[0]["event_count"] >= 1
    assert summary.iloc[0]["win_rate_5"] >= 0.0


def test_signal_recipe_report_merges_duplicate_signal_zones():
    dates = pd.date_range("2024-01-02", periods=8, freq="B")
    events = pd.DataFrame(
        [
            {"stock_code": "00001", "date": dates[0], "recipe_name": "demo", "setup_type": "demo_setup", "setup_score": 70.0, "forward_return_5": 0.01},
            {"stock_code": "00001", "date": dates[1], "recipe_name": "demo", "setup_type": "demo_setup", "setup_score": 80.0, "forward_return_5": 0.02},
            {"stock_code": "00001", "date": dates[2], "recipe_name": "demo", "setup_type": "demo_setup", "setup_score": 75.0, "forward_return_5": 0.03},
            {"stock_code": "00001", "date": dates[7], "recipe_name": "demo", "setup_type": "demo_setup", "setup_score": 72.0, "forward_return_5": -0.01},
        ]
    )

    merged = StockAnalyzer._merge_signal_recipe_events(events, cooldown_days=3, event_policy="best_score")

    assert len(merged) == 2
    assert list(merged["merged_signal_count"]) == [3, 1]
    assert merged.iloc[0]["setup_score"] == 80.0
    assert merged.iloc[0]["zone_start_date"] == dates[0]
    assert merged.iloc[0]["zone_end_date"] == dates[2]
    assert merged.iloc[0]["signal_zone_id"] == "00001:demo:demo_setup:1"


def test_signal_recipe_summary_includes_distribution_and_concentration_metrics():
    events = pd.DataFrame(
        [
            {"stock_code": "00001", "recipe_name": "demo", "setup_type": "demo_setup", "setup_score": 70.0, "forward_return_5": 0.10, "forward_max_drawdown_5": -0.03},
            {"stock_code": "00002", "recipe_name": "demo", "setup_type": "demo_setup", "setup_score": 80.0, "forward_return_5": -0.05, "forward_max_drawdown_5": -0.08},
            {"stock_code": "00002", "recipe_name": "demo", "setup_type": "demo_setup", "setup_score": 90.0, "forward_return_5": 0.20, "forward_max_drawdown_5": -0.02},
            {"stock_code": "00003", "recipe_name": "demo", "setup_type": "demo_setup", "setup_score": 60.0, "forward_return_5": -0.02, "forward_max_drawdown_5": -0.04},
        ]
    )

    summary = StockAnalyzer._summarize_signal_recipe_events(events, horizons=(5,))
    row = summary.iloc[0]

    assert row["unique_stock_count"] == 3
    assert row["top5_stock_event_share"] == 1.0
    assert row["median_forward_return_5"] == 0.04
    assert abs(row["avg_win_5"] - 0.15) < 1e-12
    assert abs(row["avg_loss_5"] - -0.035) < 1e-12
    assert row["return_drawdown_ratio_5"] > 0
