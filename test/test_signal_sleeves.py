#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""动量 sleeve 与信号层风控的测试。"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.ingest.service import MarketDataService
from factor_engine.portfolio.optimizer import PortfolioConstraints, optimize_long_only
from factor_engine.signals import create_signal_recipe, list_signal_recipes


def _momentum_bars(gain=0.10, amount=8e8):
    dates = pd.bdate_range(end="2026-09-11", periods=40)
    close = pd.Series(np.linspace(10.0, 10.4, 39).tolist() + [10.4 * (1 + gain)], index=dates)
    return pd.DataFrame(
        {
            "stock_code": "603693.SH",
            "trade_date": dates,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": amount / close,
            "amount": amount,
        }
    )


def test_limit_momentum_recipe_is_registered() -> None:
    assert "limit_momentum" in list_signal_recipes()


def test_limit_momentum_recipe_flags_a_liquid_limit_up_day() -> None:
    result = create_signal_recipe("limit_momentum", min_gain=0.095, min_median_amount_20d=5e7).evaluate(
        _momentum_bars().set_index("trade_date")[["high", "low", "close", "volume", "amount"]]
    )

    assert result.signal_type == "limit_momentum"
    assert result.score >= 60
    assert result.features["gain_1d"] == pytest.approx(0.10, abs=1e-6)
    assert result.features["expected_holding_days"] == 5
    assert result.features["stop_price"] < result.features["latest_close"]


def test_limit_momentum_recipe_ignores_an_illiquid_limit_up() -> None:
    result = create_signal_recipe("limit_momentum", min_gain=0.095, min_median_amount_20d=5e7).evaluate(
        _momentum_bars(amount=2e6).set_index("trade_date")[["high", "low", "close", "volume", "amount"]]
    )

    assert result.signal_type == "neutral"
    assert result.features["liquidity_ok"] is False


def test_optimizer_honours_per_sleeve_floors_and_caps() -> None:
    candidates = pd.DataFrame(
        {
            "stock_code": ["A.SZ", "B.SZ", "C.SZ", "D.SZ"],
            "model_score": [99.0, 98.0, 20.0, 10.0],
            "volatility_20d": [0.3, 0.3, 0.3, 0.3],
            "median_turnover_amount_20d": [5e8] * 4,
            "tradable_flag": [True] * 4,
            "trade_date": ["2026-09-11"] * 4,
        }
    )
    constraints = PortfolioConstraints(gross_exposure=0.95, max_weight=0.60, max_holdings=4, weighting="inverse_volatility")
    frame, manifest = optimize_long_only(
        candidates, constraints=constraints, initial_capital=45_000.0,
        forced_codes=["C.SZ", "D.SZ"],
        forced_min_weight={"C.SZ": 0.08, "D.SZ": 0.05},
        forced_max_weight={"C.SZ": 0.20, "D.SZ": 0.10},
    )
    weights = frame.set_index("stock_code")["target_weight"]

    assert 0.08 - 1e-9 <= weights["C.SZ"] <= 0.20 + 1e-9
    assert 0.05 - 1e-9 <= weights["D.SZ"] <= 0.10 + 1e-9
    assert manifest["forced_max_weight"] == {"C.SZ": 0.20, "D.SZ": 0.10}


class _StubWarehouse:
    """Minimal warehouse stub so the sleeve logic can be tested without a data store."""

    def __init__(self, bars, info):
        self._bars = bars
        self._info = info

    def read_ohlcv(self, **kwargs):
        frame = self._bars
        codes = kwargs.get("stock_code")
        if codes is not None:
            wanted = {str(codes)} if isinstance(codes, str) else {str(code) for code in codes}
            frame = frame[frame["stock_code"].astype(str).isin(wanted)]
        columns = kwargs.get("columns")
        if columns:
            frame = frame[[column for column in columns if column in frame.columns]]
        return frame.copy()

    def read_stock_info(self, stock_codes=None, market=None):
        return self._info.copy()


def _service_with_stub():
    service = MarketDataService.__new__(MarketDataService)
    clean = _momentum_bars(gain=0.10, amount=8e8)
    clean["stock_code"] = "600111.SH"
    thin = _momentum_bars(gain=0.10, amount=2e6)
    thin["stock_code"] = "600222.SH"
    service.warehouse = _StubWarehouse(
        pd.concat([clean, thin], ignore_index=True),
        pd.DataFrame(
            {
                "stock_code": ["600111.SH", "600222.SH"],
                "name": ["正常股份", "ST薄量"],
                "market_cap": [9e9, 9e9],
            }
        ),
    )
    return service


def _sleeve_config():
    return {
        "enabled": True,
        "recipes": ["limit_momentum"],
        "allowed_setup_types": ["limit_momentum"],
        "min_score": 60.0,
        "min_model_score": 0.0,
        # Model shortlist = 1 name; the momentum scan must widen it back to 2.
        "scan_top_k": 1,
        "max_overrides": 0,
        "forced_min_weight": 0.08,
        "forced_max_weight": 0.20,
        "lookback_sessions": 120,
        "momentum_sleeve": {
            "enabled": True, "setup_types": ["limit_momentum"], "slots": 1,
            "min_weight": 0.05, "max_weight": 0.10, "min_model_score": 0.0,
        },
        "risk_filters": {"exclude_st": True, "min_market_cap": 0.0, "min_median_amount_20d": 5e7},
        # The recipe gate is deliberately loose here so the sleeve-level risk
        # filters (ST name, median turnover) are the ones under test.
        "recipe_params": {"limit_momentum": {"min_gain": 0.095, "min_median_amount_20d": 1e6}},
    }


def _bottom_momentum_bars(amount=8e8):
    dates = pd.bdate_range(end="2026-09-11", periods=150)
    close = pd.Series(
        np.r_[np.linspace(10.0, 5.0, 100), np.linspace(5.1, 7.0, 50)], index=dates
    )
    return pd.DataFrame(
        {
            "stock_code": "600333.SH", "trade_date": dates,
            "high": close * 1.01, "low": close * 0.99, "close": close,
            "volume": amount / close, "amount": amount,
        }
    )


def test_bottom_momentum_sleeve_has_its_own_weight_cap() -> None:
    service = MarketDataService.__new__(MarketDataService)
    bars = _bottom_momentum_bars()
    service.warehouse = _StubWarehouse(
        bars,
        pd.DataFrame({
            "stock_code": ["600333.SH"], "name": ["正常股份"],
            "market_cap": [9e9], "industry_l2": ["测试行业"],
        }),
    )
    ranked = pd.DataFrame({
        "trade_date": pd.to_datetime(["2026-09-11"]),
        "stock_code": ["600333.SH"], "model_score": [60.0],
    })
    config = _sleeve_config()
    config.update({
        "recipes": ["bottom_momentum"],
        "allowed_setup_types": ["bottom_momentum"],
        "min_score": 40.0,
        "risk_filters": {"exclude_st": True, "min_market_cap": 0.0, "min_median_amount_20d": 5e7},
        "recipe_params": {
            "bottom_momentum": {"min_score": 40.0, "min_relative20": -1.0}
        },
        "momentum_sleeve": {"enabled": False},
        "bottom_momentum_sleeve": {
            "enabled": True, "setup_types": ["bottom_momentum"], "slots": 1,
            "min_weight": 0.05, "max_weight": 0.10, "min_model_score": 0.0,
        },
    })
    _, summary = service._apply_price_setup_signals(ranked, ranked, signal_config=config)

    assert summary["forced_codes"] == ["600333.SH"]
    assert summary["forced_floors"]["600333.SH"] == 0.05
    assert summary["forced_caps"]["600333.SH"] == 0.10
    assert summary["sleeves"]["bottom_momentum"]["forced"] == ["600333.SH"]


def test_risk_filters_drop_st_and_illiquid_names_and_cap_the_sleeve() -> None:
    service = _service_with_stub()
    ranked = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2026-09-11"] * 2),
            "stock_code": ["600111.SH", "600222.SH"],
            "model_score": [95.0, 40.0],
        }
    )
    candidates = ranked.head(1).copy()

    _, summary = service._apply_price_setup_signals(candidates, ranked, signal_config=_sleeve_config())

    rejected = {item["stock_code"]: item["reasons"] for item in summary["risk_rejected"]}
    assert "600222.SH" in rejected
    assert any(reason.startswith("st:") for reason in rejected["600222.SH"])
    assert any(reason.startswith("median_amount_20=") for reason in rejected["600222.SH"])
    assert summary["forced_codes"] == ["600111.SH"]
    assert summary["forced_caps"]["600111.SH"] == 0.10
    assert summary["forced_floors"]["600111.SH"] == 0.05
    assert summary["sleeves"]["momentum"]["forced"] == ["600111.SH"]
    assert summary["momentum_scan_size"] == 2
    assert summary["momentum_scan_in_model_pool"] == 1
