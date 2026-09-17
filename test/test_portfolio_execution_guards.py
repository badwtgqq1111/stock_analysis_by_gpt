#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""PK 定仓的两个执行护栏：强制下限可行性、整手可下单性。"""

import sys
from pathlib import Path

import numpy as np

import pandas as pd
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.ingest.service import _repair_unfillable_targets, _sleeve_weight_bounds
from factor_engine.portfolio.optimizer import PortfolioConstraints, optimize_long_only


SIGNAL_CONFIG = {
    "forced_min_weight": 0.08,
    "forced_max_weight": 0.20,
    "momentum_sleeve": {"min_weight": 0.05, "max_weight": 0.10},
    "reversal_sleeve": {"min_weight": 0.08, "max_weight": 0.15},
    "bottom_momentum_sleeve": {"min_weight": 0.05, "max_weight": 0.10},
}


def _candidates(codes, close, model_score=None):
    return pd.DataFrame({
        "stock_code": list(codes),
        "model_score": model_score or [90.0 - index for index in range(len(codes))],
        "volatility_20d": [0.35] * len(codes),
        "median_turnover_amount_20d": [8e8] * len(codes),
        "tradable_flag": [True] * len(codes),
        "last_close": list(close),
        "trade_date": ["2026-09-15"] * len(codes),
    })


def test_sleeve_bounds_read_each_sleeve_table():
    bounds = _sleeve_weight_bounds(SIGNAL_CONFIG)

    assert bounds["setup"] == (0.08, 0.20)
    assert bounds["momentum"] == (0.05, 0.10)
    assert bounds["reversal"] == (0.08, 0.15)
    # bottom_momentum 有自己的表，不能再落到 reversal 的 8%
    assert bounds["bottom_momentum"] == (0.05, 0.10)


def test_feasible_floors_are_honoured_literally():
    codes = ["A.SZ", "B.SZ", "C.SZ", "D.SZ", "E.SZ"]
    floors = {"A.SZ": 0.08, "B.SZ": 0.08, "C.SZ": 0.08, "D.SZ": 0.05, "E.SZ": 0.05}
    caps = {code: 0.15 for code in codes}
    caps["E.SZ"] = 0.10
    constraints = PortfolioConstraints(gross_exposure=0.35, max_weight=0.15, max_holdings=5,
                                       weighting="inverse_volatility")

    frame, manifest = optimize_long_only(
        _candidates(codes, [10.0] * 5), constraints=constraints, initial_capital=45_000.0,
        forced_codes=codes, forced_min_weight=floors, forced_max_weight=caps,
    )
    weights = frame.set_index("stock_code")["target_weight"]

    assert manifest["forced_floor_feasible"] is True
    assert manifest["forced_floor_scale"] == pytest.approx(1.0)
    assert manifest["forced_floor_honoured"] is True
    for code, floor in floors.items():
        assert weights[code] >= floor - 1e-9
    assert float(weights.sum()) <= 0.35 + 1e-9


def test_infeasible_floors_are_scaled_and_reported_not_silently_broken():
    codes = ["A.SZ", "B.SZ", "C.SZ", "D.SZ", "E.SZ"]
    floors = {"A.SZ": 0.08, "B.SZ": 0.08, "C.SZ": 0.08, "D.SZ": 0.08, "E.SZ": 0.05}
    constraints = PortfolioConstraints(gross_exposure=0.35, max_weight=0.15, max_holdings=6,
                                       weighting="inverse_volatility")

    frame, manifest = optimize_long_only(
        _candidates(codes, [10.0] * 5, model_score=[90.0, 80.0, 20.0, 15.0, 10.0]),
        constraints=constraints, initial_capital=45_000.0,
        forced_codes=codes, forced_min_weight=floors, forced_max_weight={code: 0.15 for code in codes},
    )
    weights = frame.set_index("stock_code")["target_weight"]
    scale = manifest["forced_floor_scale"]

    assert manifest["forced_floor_feasible"] is False
    assert scale == pytest.approx(0.35 / 0.37)
    assert manifest["forced_floor_honoured"] is True
    # 声明 8% 的名字拿到的是按预算缩放后的 8% * scale，而不是 0.08 也不能低于它
    assert weights["A.SZ"] == pytest.approx(0.08 * scale, abs=1e-9)
    assert manifest["forced_min_weight_effective"]["A.SZ"] == pytest.approx(0.08 * scale, abs=1e-9)
    assert float(weights.sum()) <= 0.35 + 1e-9


def test_unfillable_targets_are_dropped_instead_of_published():
    # last_close * 100 手价值分别 1,000 / 30,000 / 40,000；45k 账户下后两只买不起一手
    codes = ["CHEAP.SZ", "RICH.SZ", "RICHER.SZ"]
    frame = _candidates(codes, [10.0, 300.0, 400.0])
    constraints = PortfolioConstraints(gross_exposure=0.35, max_weight=0.15, max_holdings=3,
                                       weighting="inverse_volatility")

    optimized, manifest, summary = _repair_unfillable_targets(
        optimize_long_only, frame, constraints=constraints, initial_capital=45_000.0, lot_size=100,
    )

    book = optimized[optimized["target_weight"] > 0]
    assert set(book["stock_code"]) == {"CHEAP.SZ"}
    assert bool(book["lot_fillable"].all())
    assert "RICHER.SZ" in summary["dropped_for_lots"]
    assert summary["unfillable_codes"] == []
    assert float(optimized["target_weight"].sum()) <= 0.35 + 1e-9


def test_forced_name_is_released_when_one_lot_does_not_fit():
    codes = ["CHEAP.SZ", "PRICEY.SZ"]
    frame = _candidates(codes, [10.0, 300.0])
    constraints = PortfolioConstraints(gross_exposure=0.35, max_weight=0.15, max_holdings=2,
                                       weighting="inverse_volatility")

    optimized, _manifest, summary = _repair_unfillable_targets(
        optimize_long_only, frame, constraints=constraints, initial_capital=45_000.0, lot_size=100,
        forced_codes=codes, forced_min_weight={"PRICEY.SZ": 0.08}, forced_max_weight={"PRICEY.SZ": 0.15},
    )

    book = optimized[optimized["target_weight"] > 0]
    assert "PRICEY.SZ" not in set(book["stock_code"])
    assert summary["dropped_forced_for_lots"] == ["PRICEY.SZ"]
    assert summary["unfillable"] == 0

    # 关闭剔除则保留理论仓位，但必须如实报告买不起
    kept, _manifest, kept_summary = _repair_unfillable_targets(
        optimize_long_only, frame, constraints=constraints, initial_capital=45_000.0, lot_size=100,
        forced_codes=codes, forced_min_weight={"PRICEY.SZ": 0.08}, forced_max_weight={"PRICEY.SZ": 0.15},
        drop_unfillable_forced=False,
    )
    assert "PRICEY.SZ" in set(kept.loc[kept["target_weight"] > 0, "stock_code"])
    assert "PRICEY.SZ" in kept_summary["unfillable_codes"]


def test_volatility_target_scales_the_book_down():
    codes = ["A.SZ", "B.SZ", "C.SZ", "D.SZ"]
    frame = _candidates(codes, [10.0] * 4)
    frame["volatility_20d"] = [0.9, 0.95, 0.85, 1.0]        # ex-ante vol far above target
    constraints = PortfolioConstraints(gross_exposure=0.35, max_weight=0.15, max_holdings=4,
                                       weighting="inverse_volatility", target_volatility=0.10)

    optimized, manifest = optimize_long_only(frame, constraints=constraints, initial_capital=45_000.0)
    risk = manifest["risk_control"]
    vol = float(np.sqrt(((optimized["target_weight"].to_numpy() * optimized["volatility_20d"].to_numpy()) ** 2).sum()))

    assert risk["enforced"] is True
    assert vol <= 0.10 + 1e-9
    assert risk["vol_before"] > risk["vol_after"]
    assert float(optimized["target_weight"].sum()) < 0.35     # cash is left idle, never levered


def test_name_risk_share_cap_limits_concentration():
    # inverse-vol weighting already equalises risk shares, so the cap is tested
    # with score-based weighting where one high-vol name would dominate variance.
    codes = ["A.SZ", "B.SZ", "C.SZ"]
    frame = _candidates(codes, [10.0] * 3, model_score=[100.0, 20.0, 15.0])
    frame["volatility_20d"] = [1.2, 0.3, 0.3]
    constraints = PortfolioConstraints(gross_exposure=0.35, max_weight=0.35, max_holdings=3,
                                       weighting="score_risk", risk_aversion=0.1, cost_penalty=0.0,
                                       max_name_risk_share=0.35)

    optimized, manifest = optimize_long_only(frame, constraints=constraints, initial_capital=45_000.0)
    weights = optimized.set_index("stock_code")["target_weight"]
    variance = (optimized["target_weight"].to_numpy() * optimized["volatility_20d"].to_numpy()) ** 2
    shares = variance / variance.sum()

    assert manifest["risk_control"]["enforced"] is True
    assert shares.max() <= 0.35 + 1e-6
    assert weights["A.SZ"] > 0        # the low-vol name is not the one being capped


def test_risk_control_is_reported_even_when_not_enforced():
    codes = ["A.SZ", "B.SZ"]
    frame = _candidates(codes, [10.0] * 2)
    frame["volatility_20d"] = [0.1, 0.1]
    constraints = PortfolioConstraints(gross_exposure=0.35, max_weight=0.35, max_holdings=2,
                                       weighting="inverse_volatility", target_volatility=0.9)

    _optimized, manifest = optimize_long_only(frame, constraints=constraints, initial_capital=45_000.0)
    risk = manifest["risk_control"]

    assert risk["target_volatility"] == 0.9
    assert risk["scale"] == pytest.approx(1.0)
    assert risk["vol_after"] <= 0.9


def test_expensive_top_name_is_lifted_to_one_lot_instead_of_dropped():
    # RICH needs one lot = 30,000 on a 300k book (10% weight) but inverse-vol
    # sizing gives the highest-vol name the smallest weight.  It must be lifted
    # (its 15% cap can host one lot) rather than dropped.
    codes = ["CHEAP.SZ", "MID.SZ", "RICH.SZ"]
    frame = _candidates(codes, [10.0, 40.0, 300.0], model_score=[60.0, 70.0, 99.0])
    frame["volatility_20d"] = [0.6, 0.9, 1.5]
    constraints = PortfolioConstraints(gross_exposure=0.35, max_weight=0.15, max_holdings=3,
                                       weighting="inverse_volatility")

    optimized, _manifest, summary = _repair_unfillable_targets(
        optimize_long_only, frame, constraints=constraints, initial_capital=300_000.0, lot_size=100,
    )
    book = optimized[optimized["target_weight"] > 0]

    assert "RICH.SZ" in set(book["stock_code"])          # kept, not dropped
    assert "RICH.SZ" in summary["lifted_for_lots"]
    assert bool(book["lot_fillable"].all())
    rich_weight = float(book.set_index("stock_code").loc["RICH.SZ", "target_weight"])
    assert rich_weight * 300_000.0 >= 30_000.0 - 1e-6     # at least one lot
    assert float(optimized["target_weight"].sum()) <= 0.35 + 1e-9


def test_name_that_cannot_fit_one_lot_within_its_cap_is_still_dropped():
    # price 900 -> one lot 90,000 on a 300k book with a 15% cap (45,000): never buyable.
    codes = ["CHEAP.SZ", "TOOBIG.SZ"]
    frame = _candidates(codes, [8.0, 900.0])
    constraints = PortfolioConstraints(gross_exposure=0.35, max_weight=0.15, max_holdings=2,
                                       weighting="inverse_volatility")

    optimized, _manifest, summary = _repair_unfillable_targets(
        optimize_long_only, frame, constraints=constraints, initial_capital=300_000.0, lot_size=100,
    )

    assert "TOOBIG.SZ" not in set(optimized.loc[optimized["target_weight"] > 0, "stock_code"])
    assert "TOOBIG.SZ" in summary["dropped_for_lots"]
    assert "TOOBIG.SZ" not in summary["lifted_for_lots"]


def test_risk_scalers_deweight_stretched_names_without_dropping_them():
    codes = ["CALM.SZ", "HOT.SZ", "HOTTER.SZ"]
    frame = _candidates(codes, [10.0] * 3)
    frame["volatility_20d"] = [0.4, 0.4, 0.4]
    frame["tr_zscore_20"] = [0.2, 2.5, 3.5]          # two names above the 1.5 threshold
    constraints = PortfolioConstraints(
        gross_exposure=0.35, max_weight=0.15, max_holdings=3, weighting="inverse_volatility",
        risk_scalers={"tr_zscore_20": {"threshold": 1.5, "slope": 0.25, "floor": 0.5}},
    )

    optimized, manifest = optimize_long_only(frame, constraints=constraints, initial_capital=45_000.0)
    weights = optimized.set_index("stock_code")["target_weight"]
    report = manifest["risk_control"]["scalers"]

    assert "tr_zscore_20" in report["applied"]
    assert set(report["scaled_names"]) == {"HOT.SZ", "HOTTER.SZ"}
    assert weights["CALM.SZ"] > weights["HOT.SZ"] > weights["HOTTER.SZ"]
    assert all(weights[code] > 0 for code in codes)          # de-weighted, never dropped
    assert float(weights.sum()) == pytest.approx(0.35, abs=1e-6)


def test_risk_scalers_leave_calm_books_untouched():
    codes = ["A.SZ", "B.SZ"]
    frame = _candidates(codes, [10.0] * 2)
    frame["volatility_20d"] = [0.4, 0.4]
    frame["tr_zscore_20"] = [0.1, -0.4]
    constraints = PortfolioConstraints(
        gross_exposure=0.35, max_weight=0.35, max_holdings=2, weighting="inverse_volatility",
        risk_scalers={"tr_zscore_20": {"threshold": 1.5, "slope": 0.25, "floor": 0.5}},
    )

    optimized, manifest = optimize_long_only(frame, constraints=constraints, initial_capital=45_000.0)

    assert manifest["risk_control"]["scalers"]["applied"] == []
    assert float(optimized["target_weight"].sum()) == pytest.approx(0.35, abs=1e-6)


def test_below_direction_scaler_trims_extremely_low_volatility_names():
    codes = ["DEAD.SZ", "NORMAL.SZ", "HOT.SZ"]
    frame = _candidates(codes, [10.0] * 3)
    frame["volatility_20d"] = [0.3, 0.3, 0.3]
    frame["vol_cs_percentile"] = [0.05, 0.5, 0.9]      # dead-low vol and high vol both trimmed
    constraints = PortfolioConstraints(
        gross_exposure=0.35, max_weight=0.15, max_holdings=3, weighting="inverse_volatility",
        risk_scalers={
            "vol_cs_percentile": {"threshold": 0.8, "slope": 1.0, "floor": 0.6},
            "vol_cs_percentile_low": {"column": "vol_cs_percentile", "direction": "below",
                                      "threshold": 0.15, "slope": 1.0, "floor": 0.7},
        },
    )

    optimized, manifest = optimize_long_only(frame, constraints=constraints, initial_capital=45_000.0)
    weights = optimized.set_index("stock_code")["target_weight"]
    scaled = manifest["risk_control"]["scalers"]["scale_by_name"]

    assert weights["NORMAL.SZ"] > weights["DEAD.SZ"] > 0
    assert weights["NORMAL.SZ"] > weights["HOT.SZ"] > 0
    assert 0.89 < scaled["DEAD.SZ"] <= 0.91 and 0.89 < scaled["HOT.SZ"] <= 0.91


def test_market_deleverage_scales_the_whole_book_when_speculation_is_stretched():
    codes = ["A.SZ", "B.SZ"]
    frame = _candidates(codes, [10.0] * 2)
    frame["volatility_20d"] = [0.3, 0.3]
    frame["market_tr_z"] = [2.0, 2.0]                  # market state far above the 0.8 threshold
    constraints = PortfolioConstraints(
        gross_exposure=0.35, max_weight=0.35, max_holdings=2, weighting="inverse_volatility",
        market_deleverage={"column": "market_tr_z", "threshold": 0.8, "slope": 0.25, "floor": 0.6},
    )

    optimized, manifest = optimize_long_only(frame, constraints=constraints, initial_capital=45_000.0)
    report = manifest["risk_control"]

    assert report["market_state"]["value"] == pytest.approx(2.0)
    assert report["scale"] == pytest.approx(0.7, abs=1e-6)      # 1 - 0.25*(2.0-0.8) = 0.7
    assert float(optimized["target_weight"].sum()) == pytest.approx(0.35 * 0.7, abs=1e-6)


def test_covariance_input_replaces_the_diagonal_risk_maths():
    codes = ["A.SZ", "B.SZ"]
    frame = _candidates(codes, [10.0] * 2)
    frame["volatility_20d"] = [0.3, 0.3]
    covariance = np.array([[0.09, 0.085], [0.085, 0.09]])       # near-perfect correlation
    constraints = PortfolioConstraints(
        gross_exposure=0.35, max_weight=0.35, max_holdings=2, weighting="inverse_volatility",
        covariance=covariance, target_volatility=0.5,
    )

    optimized, manifest = optimize_long_only(frame, constraints=constraints, initial_capital=45_000.0)
    report = manifest["risk_control"]

    assert report["covariance"] == "full"
    # with ρ≈0.94 the correlated pair carries ~40% more risk than the diagonal model sees
    diagonal_vol = float(np.sqrt((0.175 ** 2) * 0.09 * 2))
    assert report["vol_before"] > diagonal_vol * 1.3
    assert float(optimized["target_weight"].sum()) == pytest.approx(0.35)
    # the volatility ceiling then de-levers the correlated book
    assert report["vol_after"] <= 0.5 + 1e-9


def test_risk_parity_target_equalises_risk_contributions():
    codes = ["LOWVOL.SZ", "MID.SZ", "HIGHVOL.SZ", "HOT.SZ"]
    frame = _candidates(codes, [10.0] * 4)
    frame["volatility_20d"] = [0.2, 0.35, 0.6, 0.8]
    covariance = np.full((4, 4), 0.02)
    np.fill_diagonal(covariance, 0.05)          # one common factor dominates
    constraints = PortfolioConstraints(
        gross_exposure=0.35, max_weight=0.35, max_holdings=4, weighting="inverse_volatility",
        covariance=covariance, risk_parity_blend=1.0,
    )

    optimized, manifest = optimize_long_only(frame, constraints=constraints, initial_capital=300_000.0)
    shares = manifest["risk_control"]["risk_shares_before_deleverage"].values()

    assert manifest["risk_control"]["risk_parity"]["applied"] is True
    assert max(shares) - min(shares) < 0.02       # equal risk contribution
    assert float(optimized["target_weight"].sum()) == pytest.approx(0.35)


def test_factor_covariance_is_psd_and_tracks_common_factors():
    rng = np.random.default_rng(3)
    factor = rng.normal(0, 0.01, 400)
    returns = np.column_stack([factor + rng.normal(0, 0.002, 400) for _ in range(6)])
    standardised = (returns - returns.mean(0)) / returns.std(0)
    correlation = np.corrcoef(standardised, rowvar=False)
    eigenvalues, eigenvectors = np.linalg.eigh(correlation)
    order = np.argsort(eigenvalues)[::-1]
    eigenvalues, eigenvectors = eigenvalues[order], eigenvectors[:, order]
    loadings = eigenvectors[:, :1] * np.sqrt(np.clip(eigenvalues[:1], 0, None))
    specific = np.clip(1.0 - np.sum(loadings ** 2, axis=1), 0.05, None)
    sigma = loadings @ loadings.T + np.diag(specific)

    assert np.min(np.linalg.eigvalsh(sigma)) > 0            # positive definite
    off_diagonal = sigma - np.diag(np.diag(sigma))
    assert float(np.abs(off_diagonal).max()) > 0.5          # common factor captured


def test_volatility_budget_deploys_unused_risk_headroom():
    codes = ["A.SZ", "B.SZ", "C.SZ"]
    frame = _candidates(codes, [10.0] * 3)
    frame["volatility_20d"] = [0.2, 0.2, 0.2]          # calm book: vol far below target
    constraints = PortfolioConstraints(
        gross_exposure=0.35, max_weight=0.30, max_holdings=3, weighting="inverse_volatility",
        target_volatility=0.25, vol_target_mode="budget", max_gross_exposure=0.60,
    )

    optimized, manifest = optimize_long_only(frame, constraints=constraints, initial_capital=300_000.0)
    report = manifest["risk_control"]
    weights = optimized.set_index("stock_code")["target_weight"]

    assert report.get("budget_deployed") is True
    assert report["gross_after"] > 0.35                 # headroom deployed
    assert report["gross_after"] <= 0.60 + 1e-9         # never above the ceiling
    assert float(weights.max()) <= 0.30 + 1e-9          # per-name cap respected
    assert report["vol_after"] <= 0.25 + 1e-9


def test_cap_mode_still_leaves_the_budget_undeployed():
    codes = ["A.SZ", "B.SZ", "C.SZ"]
    frame = _candidates(codes, [10.0] * 3)
    frame["volatility_20d"] = [0.2, 0.2, 0.2]
    constraints = PortfolioConstraints(
        gross_exposure=0.35, max_weight=0.30, max_holdings=3, weighting="inverse_volatility",
        target_volatility=0.25, vol_target_mode="cap",
    )

    optimized, manifest = optimize_long_only(frame, constraints=constraints, initial_capital=300_000.0)

    assert manifest["risk_control"].get("budget_deployed") is None
    assert float(optimized["target_weight"].sum()) == pytest.approx(0.35)
