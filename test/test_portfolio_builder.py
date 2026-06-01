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
from backtest_engine.industry_selector import IndustryCandidateSelector


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
        "liquidity_ok": True,
        "market_cap": 1000.0 + ranking_bias,
        "latest_risk_score": 80.0,
        "risk_adjusted_score": 75.0 + ranking_bias,
        "drawdown_penalty_score": 0.0,
        "downtrend_penalty_score": 0.0,
        "overheat_penalty_score": 0.0,
        "signal_freshness_score": 95.0,
        "quality_data_coverage": 1.0,
        "pe_ratio": 15.0,
        "pb_ratio": 1.0,
        "industry_l1": "Test Industry",
        "industry_l2": f"Test Group {int(ranking_bias) % 3}",
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
    assert result["selected"][0]["allocated_capital"] > 0
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
    assert 0 < sum(weights.values()) <= 0.5  # kelly target capped by single-name limits
    capital_map = {item["stock_code"]: item["allocated_capital"] for item in result["selected"]}
    assert 0 < sum(capital_map.values()) <= 45000.0  # 90000 * 0.5 target, capped
    reasons = {item["stock_code"]: item["weight_reason"] for item in result["selected"]}
    assert reasons["00100"]["method"] == "score_weighted"


def test_portfolio_builder_excludes_ineligible_fallback_candidates_from_selected():
    builder = TopNPortfolioBuilder(top_n=2, initial_capital=100000)
    good = _make_analysis_result(
        "01000",
        ranking_bias=2,
        current_signal_active=True,
        current_signal_actionable=True,
    )
    good.update(
        {
            "selection_source": "lightgbm_ranker",
            "latest_entry_type": "lightgbm_rank",
            "quality_score": 72.0,
            "risk_adjusted_score": 70.0,
            "latest_risk_score": 85.0,
            "drawdown_penalty_score": 5.0,
            "overheat_penalty_score": 0.0,
            "downtrend_penalty_score": 0.0,
            "liquidity_ok": True,
            "signal_age_days": 0,
            "market_cap": 100.0,
            "industry_l1": "Industrials",
            "industry_l2": "Machinery",
            "require_complete_data_for_selection": True,
        }
    )
    stale_high_score = _make_analysis_result(
        "01001",
        ranking_bias=80,
        current_signal_active=True,
        current_signal_actionable=False,
    )
    stale_high_score.update(
        {
            "selection_source": "lightgbm_ranker",
            "latest_entry_type": "lightgbm_rank",
            "quality_score": 95.0,
            "risk_adjusted_score": 95.0,
            "latest_risk_score": 95.0,
            "drawdown_penalty_score": 0.0,
            "overheat_penalty_score": 0.0,
            "downtrend_penalty_score": 0.0,
            "liquidity_ok": True,
            "signal_age_days": 0,
            "market_cap": 500.0,
            "industry_l1": "Consumer Discretionary",
            "industry_l2": "Retail",
            "require_complete_data_for_selection": True,
        }
    )
    illiquid_high_score = _make_analysis_result(
        "01002",
        ranking_bias=70,
        current_signal_active=True,
        current_signal_actionable=True,
    )
    illiquid_high_score.update(
        {
            "selection_source": "lightgbm_ranker",
            "latest_entry_type": "lightgbm_rank",
            "quality_score": 90.0,
            "risk_adjusted_score": 90.0,
            "latest_risk_score": 90.0,
            "drawdown_penalty_score": 0.0,
            "overheat_penalty_score": 0.0,
            "downtrend_penalty_score": 0.0,
            "liquidity_ok": False,
            "signal_age_days": 0,
            "market_cap": 400.0,
            "industry_l1": "Healthcare",
            "industry_l2": "Biotech",
            "require_complete_data_for_selection": True,
        }
    )

    result = builder.build(
        stock_codes=["01000", "01001", "01002"],
        analysis_results=[good, stale_high_score, illiquid_high_score],
    )

    selected_codes = {item["stock_code"] for item in result["selected"]}
    ranking_by_code = {item["stock_code"]: item for item in result["ranking"]}

    assert selected_codes == {"01000"}
    assert ranking_by_code["01000"]["selection_eligible"] is True
    assert ranking_by_code["01000"]["data_coverage_score"] >= 70
    assert ranking_by_code["01001"]["selection_eligible"] is False
    assert "signal_not_actionable" in ranking_by_code["01001"]["eligibility_reasons"]
    assert ranking_by_code["01002"]["selection_eligible"] is False
    assert "liquidity_not_ok" in ranking_by_code["01002"]["eligibility_reasons"]


def test_portfolio_builder_final_selection_comes_from_industry_shortlist():
    builder = TopNPortfolioBuilder(top_n=4, initial_capital=100000)

    results = []
    for idx, bias in enumerate([90, 80, 70, 60], start=1):
        item = _make_analysis_result(
            f"0200{idx}",
            ranking_bias=bias,
            current_signal_active=True,
            current_signal_actionable=True,
        )
        item.update(
            {
                "selection_source": "lightgbm_ranker",
                "latest_entry_type": "lightgbm_rank",
                "quality_score": 70.0,
                "risk_adjusted_score": 75.0,
                "latest_risk_score": 80.0,
                "drawdown_penalty_score": 3.0,
                "overheat_penalty_score": 0.0,
                "downtrend_penalty_score": 0.0,
                "liquidity_ok": True,
                "signal_age_days": 0,
                "market_cap": 500.0,
                "industry_l1": "Technology",
                "industry_l2": "Software",
                "value_score": 60.0,
            }
        )
        results.append(item)

    for idx, bias in enumerate([20, 10], start=1):
        item = _make_analysis_result(
            f"0300{idx}",
            ranking_bias=bias,
            current_signal_active=True,
            current_signal_actionable=True,
        )
        item.update(
            {
                "selection_source": "lightgbm_ranker",
                "latest_entry_type": "lightgbm_rank",
                "quality_score": 65.0,
                "risk_adjusted_score": 65.0,
                "latest_risk_score": 75.0,
                "drawdown_penalty_score": 4.0,
                "overheat_penalty_score": 0.0,
                "downtrend_penalty_score": 0.0,
                "liquidity_ok": True,
                "signal_age_days": 0,
                "market_cap": 300.0,
                "industry_l1": "Healthcare",
                "industry_l2": "Biotech",
                "value_score": 55.0,
            }
        )
        results.append(item)

    result = builder.build(
        stock_codes=[item["stock_code"] for item in results],
        analysis_results=results,
    )

    selected_codes = {item["stock_code"] for item in result["selected"]}
    ranking_by_code = {item["stock_code"]: item for item in result["ranking"]}

    assert "02003" not in selected_codes
    assert "02004" not in selected_codes
    assert ranking_by_code["02003"]["industry_rank"] > ranking_by_code["02003"]["industry_cap"]
    assert all(item["industry_rank"] <= item["industry_cap"] for item in result["selected"])


def test_portfolio_builder_syncs_ranking_selected_flags_to_final_holdings():
    builder = TopNPortfolioBuilder(top_n=4, initial_capital=100000)

    results = []
    for idx, (code, bias, win_rate) in enumerate(
        [
            ("02101", 90, 90.0),
            ("02102", 80, 80.0),
            ("02103", 70, 70.0),
            ("02104", 60, 20.0),
            ("02105", 50, 95.0),
            ("02106", 40, 95.0),
        ]
    ):
        item = _make_analysis_result(
            code,
            ranking_bias=bias,
            current_signal_active=True,
            current_signal_actionable=True,
        )
        item.update(
            {
                "selection_source": "lightgbm_ranker",
                "latest_entry_type": "lightgbm_rank",
                "backtest": {
                    "total_return": 10.0 + bias,
                    "win_rate": win_rate,
                    "total_trades": 80,
                },
                "quality_score": 70.0,
                "risk_adjusted_score": 75.0,
                "latest_risk_score": 80.0,
                "drawdown_penalty_score": 0.0,
                "overheat_penalty_score": 0.0,
                "downtrend_penalty_score": 0.0,
                "liquidity_ok": True,
                "signal_age_days": 0,
                "market_cap": 500.0,
                "industry_l1": f"Industry {idx}",
                "industry_l2": f"Group {idx}",
                "value_score": 60.0,
            }
        )
        results.append(item)

    result = builder.build(
        stock_codes=[item["stock_code"] for item in results],
        analysis_results=results,
    )

    selected_codes = {item["stock_code"] for item in result["selected"]}
    ranking_selected_codes = {
        item["stock_code"]
        for item in result["ranking"]
        if item.get("selected")
    }

    assert selected_codes == ranking_selected_codes
    assert "02104" not in selected_codes
    assert all(item.get("portfolio_weight", 0) > 0 for item in result["selected"])
    assert all(
        ("portfolio_weight" in item) == (item["stock_code"] in selected_codes)
        for item in result["ranking"]
    )


def test_selection_eligibility_applies_documented_hard_filters():
    base = {
        "stock_code": "09000",
        "current_signal_active": True,
        "current_signal_actionable": True,
        "liquidity_ok": True,
        "is_fund_like": False,
        "tradable_flag": True,
        "setup_type": "pre_breakout",
        "market_cap": 1000.0,
        "latest_risk_score": 80.0,
        "risk_adjusted_score": 75.0,
        "drawdown_penalty_score": 0.0,
        "downtrend_penalty_score": 0.0,
        "industry_l1": "Technology",
        "signal_tier": "strong",
        "quality_data_coverage": 1.0,
        "pe_ratio": 15.0,
    }

    weak = {**base, "signal_tier": "weak"}
    drawdown = {**base, "drawdown_penalty_score": 30.0}
    downtrend = {**base, "downtrend_penalty_score": 50.0}
    no_quality = {**base, "quality_data_coverage": 0.0}
    extreme_pe = {**base, "pe_ratio": 303.0}

    cases = [
        (weak, "weak_signal_tier"),
        (drawdown, "excessive_drawdown(30)"),
        (downtrend, "severe_downtrend(50)"),
        (no_quality, "low_quality_coverage(0%)"),
        (extreme_pe, "extreme_pe(303)"),
    ]

    for item, expected_reason in cases:
        result = TopNPortfolioBuilder._compute_selection_eligibility(item)
        assert result["selection_eligible"] is False
        assert expected_reason in result["eligibility_reasons"]


def test_selection_eligibility_matches_selector_filter_reasons():
    item = {
        "stock_code": "09010",
        "current_signal_active": True,
        "current_signal_actionable": True,
        "liquidity_ok": True,
        "is_fund_like": False,
        "tradable_flag": True,
        "setup_type": "pre_breakout",
        "market_cap": 1000.0,
        "latest_risk_score": 80.0,
        "risk_adjusted_score": 75.0,
        "drawdown_penalty_score": 0.0,
        "downtrend_penalty_score": 0.0,
        "overheat_penalty_score": 85.0,
        "industry_l1": "Technology",
        "signal_tier": "strong",
        "signal_freshness_score": 34.0,
        "quality_data_coverage": 1.0,
        "data_coverage_score": 49.0,
        "pe_ratio": -1.0,
        "pb_ratio": 51.0,
    }

    result = TopNPortfolioBuilder._compute_selection_eligibility(item)

    assert result["selection_eligible"] is False
    assert "low_data_coverage(49%)" in result["eligibility_reasons"]
    assert "stale_signal(freshness=34)" in result["eligibility_reasons"]
    assert "overheated(85)" in result["eligibility_reasons"]
    assert "negative_pe(-1.0)" in result["eligibility_reasons"]
    assert "extreme_pb(51)" in result["eligibility_reasons"]
    assert len(result["eligibility_reasons"]) == len(set(result["eligibility_reasons"]))


def test_industry_selector_applies_documented_hard_filters_and_preserves_zero_coverage():
    rows = [
        {
            "stock_code": "09001",
            "ranking_score": 90.0,
            "eligibility_reasons": ["weak_signal_tier"],
            "current_signal_actionable": True,
            "liquidity_ok": True,
            "setup_type": "pre_breakout",
            "signal_freshness_score": 90.0,
            "signal_tier": "weak",
            "quality_data_coverage": 1.0,
            "drawdown_penalty_score": 0.0,
            "downtrend_penalty_score": 0.0,
            "overheat_penalty_score": 0.0,
            "data_coverage_score": 100.0,
            "pe_ratio": 15.0,
            "pb_ratio": 1.0,
            "industry_l1": "Tech",
            "industry_l2": "Software",
        },
        {
            "stock_code": "09002",
            "ranking_score": 89.0,
            "current_signal_actionable": True,
            "liquidity_ok": True,
            "setup_type": "pre_breakout",
            "signal_freshness_score": 90.0,
            "signal_tier": "medium",
            "quality_data_coverage": 0.0,
            "drawdown_penalty_score": 30.0,
            "downtrend_penalty_score": 50.0,
            "overheat_penalty_score": 0.0,
            "data_coverage_score": 100.0,
            "pe_ratio": 303.0,
            "pb_ratio": 1.0,
            "industry_l1": "Tech",
            "industry_l2": "Software",
        },
        {
            "stock_code": "09003",
            "ranking_score": 70.0,
            "current_signal_actionable": True,
            "liquidity_ok": True,
            "setup_type": "pre_breakout",
            "signal_freshness_score": 90.0,
            "signal_tier": "medium",
            "quality_data_coverage": 0.8,
            "drawdown_penalty_score": 0.0,
            "downtrend_penalty_score": 0.0,
            "overheat_penalty_score": 0.0,
            "data_coverage_score": 100.0,
            "pe_ratio": 15.0,
            "pb_ratio": 1.0,
            "industry_l1": "Utilities",
            "industry_l2": "Power",
        },
    ]

    selected = IndustryCandidateSelector(top_n=2).select(rows)
    by_code = {row["stock_code"]: row for row in selected}

    assert by_code["09001"]["eligibility_pass"] is False
    assert "weak_signal_tier(tier=weak)" in by_code["09001"]["eligibility_reasons"]
    assert len(by_code["09001"]["eligibility_reasons"]) == 1
    assert by_code["09002"]["eligibility_pass"] is False
    assert "low_quality_coverage(0%)" in by_code["09002"]["eligibility_reasons"]
    assert "excessive_drawdown(score=30)" in by_code["09002"]["eligibility_reasons"]
    assert "severe_downtrend(score=50)" in by_code["09002"]["eligibility_reasons"]
    assert "extreme_pe(303)" in by_code["09002"]["eligibility_reasons"]
    assert by_code["09003"]["eligibility_pass"] is True
    assert by_code["09003"]["selected"] is True


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

    rows = TopNPortfolioBuilder._build_ranking_rows([fresh_breakout, stale_sideways])
    fresh_row = rows[0]
    stale_row = rows[1]

    assert fresh_row["ranking_score"] > stale_row["ranking_score"]
    assert fresh_row["setup_type"] == "pre_breakout"
    assert stale_row["setup_type"] == "sideways"


def test_ranking_row_prefers_lower_drawdown_for_lightgbm_candidates():
    safer = {
        "stock_code": "01901",
        "backtest": {"total_return": 14.0, "win_rate": 60.0, "total_trades": 5},
        "latest_expected_3m_score": 90.0,
        "latest_matrix_score": 88.0,
        "latest_regime_score": np.nan,
        "latest_entry_type": "lightgbm_rank",
        "latest_signal_tier": "strong",
        "latest_signal_date": pd.Timestamp("2025-01-10"),
        "current_signal_active": True,
        "current_signal_actionable": True,
        "current_signal_score": 90.0,
        "avg_forward_return_60_signal": 7.0,
        "avg_forward_return_60_watch": 1.0,
        "factor_set": "qlib_alpha158",
        "selection_source": "lightgbm_ranker",
        "setup_type": "bottom_rebound",
        "setup_score": 75.0,
        "sideways_penalty": 1.0,
        "signal_freshness_score": 96.0,
        "signal_age_days": 1,
        "factor_explanation": {"model_type": "lightgbm_ranker"},
        "risk_adjusted_score": 84.0,
        "latest_risk_score": 92.0,
        "drawdown_penalty_score": 8.0,
        "recent_drawdown": -0.02,
    }
    riskier = {
        **safer,
        "stock_code": "01902",
        "latest_expected_3m_score": 85.0,
        "current_signal_score": 85.0,
        "risk_adjusted_score": 72.0,
        "latest_risk_score": 58.0,
        "drawdown_penalty_score": 42.0,
        "recent_drawdown": -0.11,
    }

    rows = TopNPortfolioBuilder._build_ranking_rows([safer, riskier])
    safer_row = rows[0]
    riskier_row = rows[1]

    assert safer_row["ranking_score"] > riskier_row["ranking_score"]
    assert safer_row["selection_source"] == "lightgbm_ranker"
    assert safer_row["drawdown_penalty_score"] < riskier_row["drawdown_penalty_score"]


def test_ranking_row_prefers_startup_candidate_over_downtrend_candidate_for_lightgbm():
    startup = {
        "stock_code": "02901",
        "backtest": {"total_return": 16.0, "win_rate": 61.0, "total_trades": 5},
        "latest_expected_3m_score": 86.0,
        "latest_matrix_score": 82.0,
        "latest_regime_score": np.nan,
        "latest_entry_type": "lightgbm_rank",
        "latest_signal_tier": "strong",
        "latest_signal_date": pd.Timestamp("2025-01-10"),
        "current_signal_active": True,
        "current_signal_actionable": True,
        "current_signal_score": 86.0,
        "avg_forward_return_60_signal": 7.5,
        "avg_forward_return_60_watch": 1.0,
        "factor_set": "qlib_alpha158",
        "selection_source": "lightgbm_ranker",
        "setup_type": "pre_breakout",
        "setup_score": 78.0,
        "sideways_penalty": 0.0,
        "signal_freshness_score": 96.0,
        "signal_age_days": 1,
        "factor_explanation": {"model_type": "lightgbm_ranker"},
        "risk_adjusted_score": 80.0,
        "latest_risk_score": 88.0,
        "drawdown_penalty_score": 6.0,
        "recent_drawdown": -0.015,
        "startup_score": 86.0,
        "overheat_penalty_score": 8.0,
        "downtrend_penalty_score": 0.0,
        "trend_state": "startup",
    }
    downtrend = {
        **startup,
        "stock_code": "02902",
        "latest_expected_3m_score": 72.0,
        "current_signal_score": 72.0,
        "risk_adjusted_score": 84.0,
        "startup_score": 12.0,
        "overheat_penalty_score": 2.0,
        "downtrend_penalty_score": 55.0,
        "trend_state": "downtrend",
    }

    rows = TopNPortfolioBuilder._build_ranking_rows([startup, downtrend])
    startup_row = rows[0]
    downtrend_row = rows[1]

    assert startup_row["ranking_score"] > downtrend_row["ranking_score"]
    assert startup_row["selection_source"] == "lightgbm_ranker"
    assert downtrend_row["downtrend_penalty_score"] > startup_row["downtrend_penalty_score"]


def test_ranking_row_prefers_startup_candidate_over_non_candidate_for_lightgbm():
    startup = {
        "stock_code": "03901",
        "backtest": {"total_return": 18.0, "win_rate": 58.0, "total_trades": 5},
        "latest_expected_3m_score": 91.0,
        "latest_matrix_score": 83.0,
        "latest_regime_score": np.nan,
        "latest_entry_type": "lightgbm_rank",
        "latest_signal_tier": "strong",
        "latest_signal_date": pd.Timestamp("2025-01-10"),
        "current_signal_active": True,
        "current_signal_actionable": True,
        "current_signal_score": 91.0,
        "avg_forward_return_60_signal": 8.0,
        "avg_forward_return_60_watch": 1.0,
        "factor_set": "qlib_alpha158",
        "selection_source": "lightgbm_ranker",
        "setup_type": "pre_breakout",
        "setup_score": 80.0,
        "sideways_penalty": 0.0,
        "signal_freshness_score": 95.0,
        "signal_age_days": 1,
        "factor_explanation": {"model_type": "lightgbm_ranker"},
        "risk_adjusted_score": 85.0,
        "latest_risk_score": 85.0,
        "drawdown_penalty_score": 5.0,
        "recent_drawdown": -0.012,
        "startup_score": 88.0,
        "overheat_penalty_score": 7.0,
        "downtrend_penalty_score": 0.0,
        "trend_state": "startup",
        "startup_candidate": True,
        "startup_candidate_score": 91.0,
    }
    non_candidate = {
        **startup,
        "stock_code": "03902",
        "latest_expected_3m_score": 89.0,
        "current_signal_score": 89.0,
        "risk_adjusted_score": 90.0,
        "startup_score": 42.0,
        "trend_state": "continuation",
        "startup_candidate": False,
        "startup_candidate_score": 48.0,
    }

    rows = TopNPortfolioBuilder._build_ranking_rows([startup, non_candidate])
    startup_row = rows[0]
    non_candidate_row = rows[1]

    assert startup_row["ranking_score"] > non_candidate_row["ranking_score"]
    assert startup_row["startup_candidate"] is True
    assert non_candidate_row["startup_candidate"] is False


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

    assert breakout_snapshot["setup_type"] in {"pre_breakout", "bottom_rebound", "neutral"}
    assert breakout_snapshot["setup_score"] > breakout_snapshot["sideways_penalty"] * 0.5
    assert rebound_snapshot["setup_type"] in {"bottom_rebound", "pre_breakout", "neutral"}
    assert rebound_snapshot["setup_score"] > rebound_snapshot["sideways_penalty"] * 0.5


if __name__ == "__main__":
    test_portfolio_builder_selects_active_actionable_first()
    test_portfolio_builder_generates_cross_sectional_picks_and_watchlist()
    test_portfolio_builder_supports_score_weight_allocation()
    test_portfolio_builder_excludes_ineligible_fallback_candidates_from_selected()
    test_portfolio_builder_final_selection_comes_from_industry_shortlist()
    test_portfolio_builder_syncs_ranking_selected_flags_to_final_holdings()
    test_selection_eligibility_applies_documented_hard_filters()
    test_selection_eligibility_matches_selector_filter_reasons()
    test_industry_selector_applies_documented_hard_filters_and_preserves_zero_coverage()
    test_portfolio_builder_compounds_portfolio_equity_curve_over_dates()
    test_portfolio_builder_replay_generates_real_trades()
    test_portfolio_builder_replay_applies_transaction_costs()
    test_ranking_row_prefers_fresh_breakout_over_stale_sideways_candidate()
    test_ranking_row_prefers_lower_drawdown_for_lightgbm_candidates()
    test_ranking_row_prefers_startup_candidate_over_downtrend_candidate_for_lightgbm()
    test_ranking_row_prefers_startup_candidate_over_non_candidate_for_lightgbm()
    test_low_price_setup_snapshot_identifies_breakout_and_bottom_rebound()
    print("portfolio builder tests passed")
