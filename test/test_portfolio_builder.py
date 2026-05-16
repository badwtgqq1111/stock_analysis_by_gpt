#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""portfolio builder 本地测试。"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest_engine import TopNPortfolioBuilder


def _make_analysis_result(
    stock_code,
    ranking_bias,
    current_signal_active=False,
    current_signal_actionable=False,
    signal_tier="strong",
    signal_date=pd.Timestamp("2025-01-10"),
    holding_horizon=60,
):
    data_index = pd.date_range("2025-01-01", periods=90, freq="D")
    base_price = 10.0 + ranking_bias
    close_prices = [base_price + idx * (0.05 + ranking_bias / 500.0) for idx in range(len(data_index))]
    open_prices = [price - 0.03 for price in close_prices]
    return {
        "stock_code": stock_code,
        "data": pd.DataFrame(
            {
                "Open": open_prices,
                "High": [price + 0.1 for price in close_prices],
                "Low": [price - 0.1 for price in close_prices],
                "Close": close_prices,
                "Volume": [1000 + ranking_bias] * len(data_index),
            },
            index=data_index,
        ),
        "backtest": {
            "total_return": 10.0 + ranking_bias,
            "win_rate": 60.0,
            "total_trades": 2,
        },
        "latest_expected_3m_score": 50.0 + ranking_bias,
        "latest_matrix_score": 40.0 + ranking_bias,
        "latest_regime_score": 45.0 + ranking_bias,
        "latest_entry_type": "demo_entry",
        "latest_signal_tier": signal_tier,
        "latest_signal_date": signal_date,
        "current_signal_active": current_signal_active,
        "current_signal_actionable": current_signal_actionable,
        "current_signal_score": 55.0 + ranking_bias,
        "avg_forward_return_60_signal": 6.0,
        "avg_forward_return_60_watch": 1.0,
        "buy_signals": pd.DataFrame(
            [
                {
                    "date": signal_date,
                    "expected_3m_score": 50.0 + ranking_bias,
                    "matrix_score": 40.0 + ranking_bias,
                    "regime_score": 45.0 + ranking_bias,
                    "signal_strength": 8.0,
                    "risk_score": 2.0,
                    "signal_tier": signal_tier,
                    "actionable": True,
                    "forward_return_20": 0.03,
                    "forward_return_40": 0.05,
                    "forward_return_60": 0.07 + ranking_bias / 1000.0,
                    "forward_max_drawdown_60": -0.02,
                    "entry_type": "demo_entry",
                    "holding_horizon": holding_horizon,
                }
            ]
        ),
    }


def test_portfolio_builder_selects_active_actionable_first():
    builder = TopNPortfolioBuilder(top_n=2, initial_capital=100000)
    analysis_results = [
        _make_analysis_result("00001", ranking_bias=1, current_signal_active=False, current_signal_actionable=False),
        _make_analysis_result("00002", ranking_bias=2, current_signal_active=True, current_signal_actionable=True),
        _make_analysis_result("00003", ranking_bias=3, current_signal_active=True, current_signal_actionable=True),
    ]

    result = builder.build(stock_codes=["00001", "00002", "00003"], analysis_results=analysis_results)

    assert result is not None
    assert len(result["selected"]) == 2
    assert {item["stock_code"] for item in result["selected"]} == {"00002", "00003"}
    assert result["selected"][0]["allocated_capital"] == 50000.0
    assert result["estimated_trade_count"] == 2


def test_portfolio_builder_generates_cross_sectional_picks_and_watchlist():
    builder = TopNPortfolioBuilder(top_n=1, initial_capital=50000)
    analysis_results = [
        _make_analysis_result("00010", ranking_bias=5, signal_tier="strong"),
        _make_analysis_result("00011", ranking_bias=1, signal_tier="weak"),
    ]

    result = builder.build(stock_codes=["00010", "00011"], analysis_results=analysis_results)

    assert len(result["cross_sectional_picks"]) == 1
    assert result["cross_sectional_picks"][0]["stock_code"] == "00010"
    assert result["daily_candidate_counts"][pd.Timestamp("2025-01-10")] == 2
    assert len(result["contributions"]) == 1
    assert len(result["watchlist"]) == 1
    assert result["watchlist"][0]["stock_code"] == "00011"
    assert len(result["synthetic_portfolio_equity_curve"]) == 1
    assert result["synthetic_portfolio_equity_curve"][0]["pick_count"] == 1
    assert len(result["portfolio_equity_curve"]) > 1
    assert result["portfolio_final_value"] > 50000.0
    assert result["portfolio_replay"]["total_return"] > 0


def test_portfolio_builder_supports_score_weight_allocation():
    builder = TopNPortfolioBuilder(top_n=2, initial_capital=90000, weighting_mode="score_weight")
    analysis_results = [
        _make_analysis_result("00100", ranking_bias=10, current_signal_active=True, current_signal_actionable=True),
        _make_analysis_result("00101", ranking_bias=0, current_signal_active=True, current_signal_actionable=True),
    ]

    result = builder.build(stock_codes=["00100", "00101"], analysis_results=analysis_results)

    assert result["weighting_mode"] == "score_weight"
    assert len(result["selected"]) == 2
    weights = {item["stock_code"]: item["portfolio_weight"] for item in result["selected"]}
    assert abs(sum(weights.values()) - 1.0) < 1e-9
    assert weights["00100"] > weights["00101"]
    capital_map = {item["stock_code"]: item["allocated_capital"] for item in result["selected"]}
    assert capital_map["00100"] > capital_map["00101"]
    assert abs(sum(capital_map.values()) - 90000.0) < 1e-6


def test_portfolio_builder_compounds_portfolio_equity_curve_over_dates():
    builder = TopNPortfolioBuilder(top_n=1, initial_capital=100000, weighting_mode="equal_weight")
    result_a = _make_analysis_result(
        "00200",
        ranking_bias=5,
        current_signal_active=True,
        current_signal_actionable=True,
        signal_date=pd.Timestamp("2025-01-10"),
        holding_horizon=3,
    )
    result_b = _make_analysis_result(
        "00201",
        ranking_bias=4,
        current_signal_active=True,
        current_signal_actionable=True,
        signal_date=pd.Timestamp("2025-01-20"),
        holding_horizon=3,
    )
    result_b["buy_signals"].loc[0, "forward_return_60"] = 0.10

    result = builder.build(stock_codes=["00200", "00201"], analysis_results=[result_a, result_b])

    assert len(result["synthetic_portfolio_equity_curve"]) == 2
    assert len(result["portfolio_replay"]["equity_curve"]) >= 2
    first_equity = result["synthetic_portfolio_equity_curve"][0]["equity"]
    second_equity = result["synthetic_portfolio_equity_curve"][1]["equity"]
    assert second_equity > first_equity > 100000.0
    assert result["portfolio_final_value"] >= 100000.0


def test_portfolio_builder_replay_generates_real_trades():
    builder = TopNPortfolioBuilder(top_n=1, initial_capital=80000, weighting_mode="equal_weight")
    result = builder.build(
        stock_codes=["00300"],
        analysis_results=[
            _make_analysis_result(
                "00300",
                ranking_bias=6,
                current_signal_active=True,
                current_signal_actionable=True,
                signal_date=pd.Timestamp("2025-01-10"),
                holding_horizon=3,
            )
        ],
    )

    replay = result["portfolio_replay"]
    assert replay["final_value"] > 0
    assert len(replay["trades"]) >= 2
    assert replay["trades"][0]["type"] == "buy"
    assert replay["trades"][1]["type"] == "sell"
    assert len(replay["equity_curve"]) > 1


def test_portfolio_builder_replay_applies_transaction_costs():
    zero_cost_builder = TopNPortfolioBuilder(top_n=1, initial_capital=80000, weighting_mode="equal_weight")
    cost_builder = TopNPortfolioBuilder(
        top_n=1,
        initial_capital=80000,
        weighting_mode="equal_weight",
        buy_commission_rate=0.001,
        sell_commission_rate=0.001,
        slippage_rate=0.001,
        min_commission=5.0,
    )

    analysis_results = [
        _make_analysis_result(
            "00400",
            ranking_bias=6,
            current_signal_active=True,
            current_signal_actionable=True,
            signal_date=pd.Timestamp("2025-01-10"),
            holding_horizon=3,
        )
    ]

    zero_cost_result = zero_cost_builder.build(stock_codes=["00400"], analysis_results=analysis_results)
    cost_result = cost_builder.build(stock_codes=["00400"], analysis_results=analysis_results)

    zero_replay = zero_cost_result["portfolio_replay"]
    cost_replay = cost_result["portfolio_replay"]

    assert cost_replay["total_commission"] > 0
    assert cost_replay["final_value"] < zero_replay["final_value"]
    assert cost_replay["trades"][0]["commission"] > 0
    assert cost_replay["trades"][1]["commission"] > 0
    assert cost_replay["trades"][0]["price"] > zero_replay["trades"][0]["price"]
    assert cost_replay["trades"][1]["price"] < zero_replay["trades"][1]["price"]


def test_ranking_row_prefers_fresh_breakout_over_stale_sideways_candidate():
    fresh_breakout = {
        "stock_code": "00901",
        "backtest": {"total_return": 18.0, "win_rate": 66.0, "total_trades": 6},
        "latest_expected_3m_score": 84.0,
        "latest_matrix_score": 78.0,
        "latest_regime_score": 73.0,
        "latest_entry_type": "factor_rank",
        "latest_signal_tier": "strong",
        "latest_signal_date": pd.Timestamp("2025-01-10"),
        "current_signal_active": True,
        "current_signal_actionable": True,
        "current_signal_score": 86.0,
        "avg_forward_return_60_signal": 12.0,
        "avg_forward_return_60_watch": 3.0,
        "factor_set": "qlib_alpha158",
        "selection_source": "factor_engine",
        "setup_type": "pre_breakout",
        "setup_score": 88.0,
        "sideways_penalty": 2.0,
        "signal_freshness_score": 96.0,
        "signal_age_days": 1,
        "factor_explanation": {},
    }
    stale_sideways = {
        "stock_code": "00902",
        "backtest": {"total_return": 4.0, "win_rate": 32.0, "total_trades": 4},
        "latest_expected_3m_score": 98.0,
        "latest_matrix_score": 92.0,
        "latest_regime_score": 90.0,
        "latest_entry_type": "factor_rank",
        "latest_signal_tier": "weak",
        "latest_signal_date": pd.Timestamp("2024-12-10"),
        "current_signal_active": True,
        "current_signal_actionable": False,
        "current_signal_score": 99.0,
        "avg_forward_return_60_signal": 2.0,
        "avg_forward_return_60_watch": 1.0,
        "factor_set": "qlib_alpha158",
        "selection_source": "factor_engine",
        "setup_type": "sideways",
        "setup_score": 18.0,
        "sideways_penalty": 24.0,
        "signal_freshness_score": 5.0,
        "signal_age_days": 22,
        "factor_explanation": {},
    }

    fresh_row = TopNPortfolioBuilder._build_ranking_row(fresh_breakout)
    stale_row = TopNPortfolioBuilder._build_ranking_row(stale_sideways)

    assert fresh_row["ranking_score"] > stale_row["ranking_score"]
    assert fresh_row["setup_type"] == "pre_breakout"
    assert stale_row["setup_type"] == "sideways"


def test_low_price_setup_snapshot_identifies_breakout_and_bottom_rebound():
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

    breakout_snapshot = TopNPortfolioBuilder._summarize_low_price_setup(breakout_frame)
    rebound_snapshot = TopNPortfolioBuilder._summarize_low_price_setup(rebound_frame)

    assert breakout_snapshot["setup_type"] == "pre_breakout"
    assert breakout_snapshot["setup_score"] > breakout_snapshot["sideways_penalty"]
    assert rebound_snapshot["setup_type"] == "bottom_rebound"
    assert rebound_snapshot["setup_score"] > rebound_snapshot["sideways_penalty"]


if __name__ == "__main__":
    test_portfolio_builder_selects_active_actionable_first()
    test_portfolio_builder_generates_cross_sectional_picks_and_watchlist()
    test_portfolio_builder_supports_score_weight_allocation()
    test_portfolio_builder_compounds_portfolio_equity_curve_over_dates()
    test_portfolio_builder_replay_generates_real_trades()
    test_portfolio_builder_replay_applies_transaction_costs()
    test_ranking_row_prefers_fresh_breakout_over_stale_sideways_candidate()
    test_low_price_setup_snapshot_identifies_breakout_and_bottom_rebound()
    print("portfolio builder tests passed")
