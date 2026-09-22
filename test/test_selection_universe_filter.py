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


def test_volume_breakout_filter_drops_only_up_moves_on_high_volume():
    codes = ["A", "B", "C", "D", "E"]
    breakout = {  # code -> (5-day change, 20-day volume ratio)
        "A": (0.06, 2.4),   # up on 2.4x volume -> dropped
        "B": (0.06, 0.7),   # up on shrinking volume -> kept (the "quiet markup")
        "C": (-0.05, 2.4),  # down on high volume -> kept by this rule
        "D": (0.01, 1.2),   # mild up, normal volume -> kept
        "E": (None, 3.0),   # missing input -> kept (reported, not silently dropped)
    }
    dropped, audit = filter_selection_universe(
        codes, names={}, median_amount={}, volume_breakout=breakout,
        settings={"enabled": True, "exclude_st": False, "exclude_volume_breakout": True,
                  "volume_breakout_ratio": 1.5},
    )
    assert dropped == ["A"]
    assert audit["volume_breakout_dropped"] == ["A"]
    assert audit["volume_breakout_ratio"] == 1.5


def test_volume_breakout_filter_is_off_by_default():
    dropped, audit = filter_selection_universe(
        ["A"], names={}, median_amount={}, volume_breakout={"A": (0.06, 3.0)},
        settings={"enabled": True, "exclude_st": False},
    )
    assert dropped == [] and audit["volume_breakout_dropped"] == []


def test_volume_breakout_requires_flow_confirmation():
    codes = ["A", "B", "C", "D"]
    breakout = {"A": (0.06, 2.4), "B": (0.06, 2.4), "C": (0.06, 2.4), "D": (0.06, 0.7)}
    flow = {"A": 1.5, "B": 0.2, "C": None, "D": 1.0}
    dropped, audit = filter_selection_universe(
        codes, names={}, median_amount={}, volume_breakout=breakout, flow_z=flow,
        settings={"enabled": True, "exclude_st": False, "exclude_volume_breakout": True,
                  "volume_breakout_ratio": 1.5, "volume_breakout_flow_z_min": 1.0,
                  "volume_breakout_missing_flow": "drop"},
    )
    assert dropped == ["B", "C"]           # unconfirmed spike + missing flow
    assert audit["volume_breakout_unconfirmed"] == ["B", "C"]
    kept, _ = filter_selection_universe(
        codes, names={}, median_amount={}, volume_breakout=breakout, flow_z=flow,
        settings={"enabled": True, "exclude_st": False, "exclude_volume_breakout": True,
                  "volume_breakout_ratio": 1.5, "volume_breakout_flow_z_min": 1.0,
                  "volume_breakout_missing_flow": "keep"},
    )
    assert kept == ["B"]                    # only the missing-flow one comes back


def test_second_tier_admits_quiet_pullbacks_with_flow():
    from factor_engine.ml.strategy_labels import apply_startup_gate

    labels = pd.DataFrame({
        "stock_code": ["A", "B", "C", "D"],
        "startup_price_eligible": [True, False, False, False],
        "dist_from_120d_low": [0.10, 0.40, 0.40, 0.80],
        "volume_ratio": [1.0, 0.70, 1.30, 0.70],
        "flow_z": [0.0, 0.80, -0.50, 2.00],
    })
    gate = apply_startup_gate(labels, {"mode": "eligibility", "second_tier_enabled": True}).set_index("stock_code")
    assert gate.loc["A", "selection_tier"] == "core" and gate.loc["A", "startup_eligible"]
    assert gate.loc["B", "selection_tier"] == "second" and gate.loc["B", "startup_eligible"]
    assert not gate.loc["C", "startup_eligible"]   # volume expanded -> not a quiet pullback
    assert not gate.loc["D", "startup_eligible"]   # too far above the low
    with pytest.raises(ValueError, match="second tier needs columns"):
        apply_startup_gate(labels.drop(columns=["dist_from_120d_low"]),
                           {"mode": "eligibility", "second_tier_enabled": True})


def test_weight_caps_limit_a_second_tier_name():
    frame = _frame([99.0, 80.0, 60.0], [0.04, 0.05, 0.06])
    cfg = PortfolioConstraints(
        gross_exposure=0.6, max_weight=0.9, max_holdings=3, weighting="rank_power",
        alpha_power=2.0, alpha_weight_floor=0.25, vol_exponent=0.25,
        max_industry_weight=1.0, max_turnover=1.0,
    )
    uncapped, _ = optimize_long_only(frame, constraints=cfg, score_col="model_score", initial_capital=1_000_000.0)
    out, manifest = optimize_long_only(
        frame, constraints=cfg, score_col="model_score", initial_capital=1_000_000.0,
        weight_caps={"S02": 0.02},
    )
    weights = out.set_index("stock_code")["target_weight"]
    plain = uncapped.set_index("stock_code")["target_weight"]
    assert weights["S02"] <= 0.02 + 1e-9
    assert weights["S02"] < plain["S02"]
    # the freed budget goes to the uncapped names
    assert weights[["S00", "S01"]].sum() > plain[["S00", "S01"]].sum()
    assert manifest["weight_caps"]["capped_codes"] == ["S02"]


def test_flow_tilt_shifts_weight_toward_inflow():
    frame = _frame([99.0, 80.0], [0.04, 0.04])
    frame["moneyflow_net_z_5d_clean"] = [2.0, -2.0]
    base = dict(gross_exposure=0.5, max_weight=1.0, max_holdings=2, weighting="rank_power",
                alpha_power=1.0, alpha_weight_floor=0.5, vol_exponent=0.0,
                max_industry_weight=1.0, max_turnover=1.0)
    plain, _ = optimize_long_only(frame, constraints=PortfolioConstraints(**base),
                                  score_col="model_score", initial_capital=1_000_000.0)
    tilted, manifest = optimize_long_only(
        frame, constraints=PortfolioConstraints(**base, flow_tilt_strength=0.5),
        score_col="model_score", initial_capital=1_000_000.0,
    )
    a = float(plain.set_index("stock_code").loc["S00", "target_weight"])
    b = float(tilted.set_index("stock_code").loc["S00", "target_weight"])
    assert b > a
    assert manifest["flow_tilt"]["column"] == "moneyflow_net_z_5d_clean"


def test_atr_stop_is_clipped_and_triggers_by_volatility():
    from factor_engine.portfolio.exits import ExitRules, evaluate_exit_plan

    holdings = pd.DataFrame({"stock_code": ["LOWV", "HIGHV", "MID"], "shares": [100, 100, 100],
                             "cost_price": [10.0, 10.0, 10.0]})
    state = pd.DataFrame({
        "stock_code": ["LOWV", "HIGHV", "MID"], "close": [9.0, 9.4, 9.3],
        "atr_pct_14": [0.01, 0.08, 0.03], "name": ["甲", "乙", "丙"],
    })
    rules = ExitRules(stop_loss_atr_multiple=2.5, stop_loss_min_pct=0.04, stop_loss_max_pct=0.10, max_weight=0.9)
    plan = evaluate_exit_plan(holdings, state, rules=rules, cash=1000.0).set_index("stock_code")
    # ATR ×2.5 clipped into [4%, 10%]: 1% -> 4%, 8% -> 10%, 3% -> 7.5%
    assert plan.loc["LOWV", "effective_stop"] == pytest.approx(0.04)
    assert plan.loc["HIGHV", "effective_stop"] == pytest.approx(0.10)
    assert plan.loc["MID", "effective_stop"] == pytest.approx(0.075)
    # -10% loss: outside LOWV's 4% stop, inside HIGHV's 10% stop
    assert plan.loc["LOWV", "action"] == "EXIT"
    assert "触发止损（ATR×2.5 = 4.0%）" in plan.loc["LOWV", "reasons"]
    assert plan.loc["HIGHV", "action"] != "EXIT"


def test_atr_stop_is_off_when_the_multiple_is_zero():
    from factor_engine.portfolio.exits import ExitRules, evaluate_exit_plan

    holdings = pd.DataFrame({"stock_code": ["A"], "shares": [100], "cost_price": [10.0]})
    state = pd.DataFrame({"stock_code": ["A"], "close": [8.0], "atr_pct_14": [0.02], "name": ["甲"]})
    plan = evaluate_exit_plan(holdings, state, rules=ExitRules(max_weight=0.9), cash=1000.0)
    assert plan.iloc[0]["effective_stop"] == 0.0


def test_production_config_carries_the_validated_selection_rules():
    import tomllib

    config = tomllib.loads((ROOT / "config" / "cn_pipeline.toml").read_text(encoding="utf-8"))
    selection = config["selection"]
    universe = selection["universe_filter"]
    gate = selection["startup_gate"]
    constraints = selection["portfolio_constraints"]
    exits = config["exits"]["rules"]
    risk = selection["risk_control"]
    assert universe["exclude_st"] is True
    assert universe["exclude_volume_breakout"] is True
    assert universe["volume_breakout_flow_z_min"] == 1.0
    assert gate["enabled"] is True and gate["second_tier_enabled"] is True
    assert gate["second_tier_max_dist_from_120d_low"] == 0.55
    assert gate["second_tier_max_weight"] == 0.05
    assert constraints["weighting"] == "rank_power"
    assert constraints["flow_tilt_strength"] == 0.25
    assert risk["vol_target_mode"] == "cap"
    assert exits["stop_loss_atr_multiple"] == 2.5
    assert exits["stop_loss_min_pct"] == 0.04 and exits["stop_loss_max_pct"] == 0.10
    # the flat percentage stop stays off: it was rejected in favour of the ATR rule
    assert exits["stop_loss_pct"] == 0.0
