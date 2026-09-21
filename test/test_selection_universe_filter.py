#!/usr/bin/env python3
"""Tests for the selection-layer universe filter and score-linked weighting."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.ingest.service import filter_selection_universe
from factor_engine.portfolio.optimizer import PortfolioConstraints, optimize_long_only


def test_st_and_liquidity_filters_report_what_they_drop():
    codes = ["000001.SZ", "000615.SZ", "000010.SZ", "600000.SH", "300001.SZ"]
    names = {"000001.SZ": "平安银行", "000615.SZ": "*ST美谷", "000010.SZ": "*ST美丽",
             "600000.SH": "浦发银行", "300001.SZ": "特锐德"}
    amounts = {code: 5e7 for code in codes}
    amounts["300001.SZ"] = 1e6

    dropped, audit = filter_selection_universe(
        codes, names=names, median_amount=amounts,
        settings={"enabled": True, "exclude_st": True, "min_median_amount_20d": 1e7},
    )
    assert set(dropped) == {"000615.SZ", "000010.SZ", "300001.SZ"}
    assert audit["st_dropped"] == ["000615.SZ", "000010.SZ"]
    assert audit["illiquid_dropped"] == ["300001.SZ"]
    assert audit["dropped"] == 3
    # names the filter cannot price are reported instead of silently passing
    dropped2, audit2 = filter_selection_universe(
        ["A", "B"], names={"A": "甲"}, median_amount={"A": 5e7},
        settings={"enabled": True, "exclude_st": True, "min_median_amount_20d": 1e7},
    )
    assert dropped2 == []
    assert audit2["missing_amount"] == ["B"]


def test_filter_is_inert_when_disabled():
    dropped, audit = filter_selection_universe(
        ["000615.SZ"], names={"000615.SZ": "*ST美谷"}, median_amount={}, settings={"enabled": False},
    )
    assert dropped == [] and audit["enabled"] is False


def _frame(scores, variances):
    return pd.DataFrame({
        "stock_code": [f"S{index:02d}" for index in range(len(scores))],
        "model_score": scores,
        "specific_variance": variances,
        "adv_20d": [1e9] * len(scores),
        "tradable_flag": [True] * len(scores),
        "industry_l2": ["IND0"] * len(scores),
        "trade_date": [pd.Timestamp("2026-09-18")] * len(scores),
    })


def test_score_inverse_vol_weighting_is_monotone_in_the_score():
    frame = _frame([99.0, 70.0, 50.0, 10.0], [0.04, 0.04, 0.04, 0.04])
    cfg = PortfolioConstraints(
        gross_exposure=0.5, max_weight=0.9, max_holdings=4, weighting="score_inverse_vol",
        alpha_weight_floor=0.25, max_industry_weight=1.0, max_turnover=1.0,
    )
    out, _ = optimize_long_only(frame, constraints=cfg, score_col="model_score", initial_capital=1_000_000.0)
    weights = out.set_index("stock_code")["target_weight"]
    assert weights["S00"] > weights["S01"] > weights["S02"] > weights["S03"]
    assert weights["S03"] > 0  # the floor keeps the weakest selected name alive
    inverse_vol = _frame([99.0, 70.0, 50.0, 10.0], [0.04, 0.04, 0.04, 0.04])
    cfg_iv = PortfolioConstraints(
        gross_exposure=0.5, max_weight=0.9, max_holdings=4, weighting="inverse_volatility",
        max_industry_weight=1.0, max_turnover=1.0,
    )
    flat, _ = optimize_long_only(inverse_vol, constraints=cfg_iv, score_col="model_score", initial_capital=1_000_000.0)
    assert np.allclose(flat["target_weight"].to_numpy(), flat["target_weight"].iloc[0])  # rank-blind


def test_forced_floor_is_scaled_by_the_model_score():
    frame = _frame([99.0, 60.0, 30.0, 5.0], [0.04] * 4)
    base = dict(
        gross_exposure=0.5, max_weight=0.9, max_holdings=4, weighting="score_inverse_vol",
        max_industry_weight=1.0, max_turnover=1.0,
    )
    generous, _ = optimize_long_only(
        frame, constraints=PortfolioConstraints(**base, forced_floor_score_scaling=False),
        score_col="model_score", initial_capital=1_000_000.0,
        forced_codes=["S03"], forced_min_weight=0.10,
    )
    earned, _ = optimize_long_only(
        frame, constraints=PortfolioConstraints(
            **base, forced_floor_score_scaling=True, forced_floor_max_weight=0.05,
        ),
        score_col="model_score", initial_capital=1_000_000.0,
        forced_codes=["S03"], forced_min_weight=0.10,
    )
    generous_weight = float(generous.set_index("stock_code").loc["S03", "target_weight"])
    earned_weight = float(earned.set_index("stock_code").loc["S03", "target_weight"])
    assert generous_weight >= 0.09
    assert earned_weight <= 0.05 + 1e-9
    assert earned_weight < generous_weight


def test_rank_power_weighting_puts_the_top_rank_first():
    frame = _frame([99.0, 98.0, 97.0, 96.0, 95.0], [0.04, 0.05, 0.06, 0.07, 0.08])
    cfg = PortfolioConstraints(
        gross_exposure=0.5, max_weight=0.9, max_holdings=5, weighting="rank_power",
        alpha_power=2.0, alpha_weight_floor=0.25, vol_exponent=0.25,
        max_industry_weight=1.0, max_turnover=1.0,
    )
    out, _ = optimize_long_only(frame, constraints=cfg, score_col="model_score", initial_capital=1_000_000.0)
    weights = out.set_index("stock_code")["target_weight"]
    assert weights["S00"] == weights.max()
    assert weights["S00"] > weights["S04"]
    from scipy.stats import spearmanr
    ranks = pd.Series({code: index + 1 for index, code in enumerate(weights.index)})
    assert spearmanr(ranks, weights).statistic < -0.5
