#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""PK 定仓的两个执行护栏：强制下限可行性、整手可下单性。"""

import sys
from pathlib import Path

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
