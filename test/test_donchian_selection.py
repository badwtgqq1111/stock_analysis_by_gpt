#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Donchian 通道突破接入选股的测试。"""

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from factor_engine.portfolio.optimizer import PortfolioConstraints, optimize_long_only
from factor_engine.signals import DonchianChannelRecipe, create_signal_recipe, list_signal_recipes


SPEC = importlib.util.spec_from_file_location("run_cn_pipeline_donchian", PROJECT_ROOT / "scripts" / "run_cn_pipeline.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def _box_frame(bars=60, last_rows=None):
    """Flat 20-day channel followed by explicit breakout/pullback rows."""
    dates = pd.date_range("2026-04-01", periods=bars, freq="B")
    frame = pd.DataFrame(
        {
            "open": 10.0,
            "high": 10.05,
            "low": 9.95,
            "close": 10.0,
            "volume": 2_000_000.0,
            "amount": 20_000_000.0,
        },
        index=dates,
        dtype=float,
    )
    for offset, row in enumerate(last_rows or []):
        index = len(frame) - len(last_rows) + offset
        for key, value in row.items():
            frame.iloc[index, frame.columns.get_loc(key)] = value
    return frame


def _breakout_frame():
    return _box_frame(
        last_rows=[{"close": 10.60, "high": 10.70, "low": 10.20, "volume": 6_000_000.0, "amount": 63_600_000.0}]
    )


def _pullback_frame():
    return _box_frame(
        last_rows=[
            {"close": 10.40, "high": 10.45, "low": 10.00, "volume": 3_000_000.0, "amount": 31_200_000.0},
            {"close": 10.32, "high": 10.38, "low": 10.30, "volume": 1_000_000.0, "amount": 10_320_000.0},
        ]
    )


def test_donchian_recipe_is_registered() -> None:
    assert "donchian_pullback" in list_signal_recipes()
    assert isinstance(create_signal_recipe("donchian_pullback"), DonchianChannelRecipe)


def test_donchian_recipe_flags_volume_confirmed_breakout_day() -> None:
    result = create_signal_recipe("donchian_pullback").evaluate(_breakout_frame())

    assert result.signal_type == "donchian_breakout"
    assert result.score >= 60.0
    assert result.features["breakout_count_20"] >= 1
    assert result.features["sessions_since_breakout"] == 0
    assert result.features["close_break_today"] is True
    assert result.features["donchian_pos"] >= 0.60
    assert result.features["channel_width"] <= 0.35


def test_donchian_recipe_flags_low_volume_pullback_holding_the_channel_top() -> None:
    result = create_signal_recipe("donchian_pullback").evaluate(_pullback_frame())

    assert result.signal_type == "donchian_pullback"
    assert result.score >= 60.0
    assert result.features["pullback_holding"] is True
    assert result.features["volume_dryup"] is True
    assert result.features["sessions_since_breakout"] == 1
    assert result.features["pullback_depth"] < 0.0


def test_donchian_recipe_is_not_a_breakout_without_a_channel_break() -> None:
    result = create_signal_recipe("donchian_pullback").evaluate(_box_frame())

    assert result.signal_type not in {"donchian_breakout", "donchian_pullback"}
    assert result.features["breakout_count_20"] == 0


def test_donchian_recipe_matches_local_cn_breakout_when_bars_are_available() -> None:
    parquet = PROJECT_ROOT / "assets" / "data" / "clean" / "ohlcv" / "market=CN"
    if not parquet.exists():
        pytest.skip("local CN parquet store is not available")

    from data.store.layout import DataLayout
    from data.store.warehouse import MarketDataWarehouse

    warehouse = MarketDataWarehouse(DataLayout(str(PROJECT_ROOT / "assets" / "data")), read_only=True)
    bars = warehouse.read_ohlcv(market="CN", frequency="daily", adjust="qfq", stock_code="603938.SH")
    if bars.empty:
        pytest.skip("603938.SH is not present in the local store")

    # Pin the evaluation to a fixed as-of date so the assertions stay valid as
    # the local store advances to newer bars.
    bars = bars.copy()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    as_of = pd.Timestamp("2026-09-09")
    frame = (
        bars[bars["trade_date"] <= as_of]
        .sort_values("trade_date").tail(120)
        .set_index("trade_date")[["high", "low", "close", "volume", "amount"]]
    )
    result = create_signal_recipe("donchian_pullback").evaluate(frame)

    assert result.signal_type == "donchian_breakout"
    assert result.score >= 60.0
    assert result.features["breakout_count_20"] >= 1
    assert result.features["intraday_break_today"] is True
    assert result.features["donchian_upper"] == pytest.approx(48.05, abs=0.02)
    assert result.features["donchian_lower"] == pytest.approx(39.10, abs=0.02)
    assert result.features["donchian_pos"] == pytest.approx(0.895, abs=0.01)


def test_optimizer_forced_codes_keep_a_slot_and_minimum_weight() -> None:
    candidates = pd.DataFrame(
        {
            "stock_code": ["000001.SZ", "000002.SZ", "000003.SZ", "603938.SH"],
            "model_score": [99.0, 98.0, 97.0, 10.0],
            "volatility_20d": [0.20, 0.20, 0.20, 1.20],
            "median_turnover_amount_20d": [5e8, 5e8, 5e8, 5e8],
            "tradable_flag": [True, True, True, True],
            "trade_date": ["2026-09-09"] * 4,
        }
    )
    constraints = PortfolioConstraints(
        gross_exposure=0.95, max_weight=0.60, max_holdings=3, weighting="inverse_volatility",
    )
    frame, manifest = optimize_long_only(
        candidates, constraints=constraints, initial_capital=45_000.0,
        forced_codes=["603938.SH"], forced_min_weight=0.20,
    )
    row = frame.set_index("stock_code").loc["603938.SH"]

    assert row["target_weight"] >= 0.20 - 1e-9
    assert row["target_weight"] <= 0.60 + 1e-9
    assert float(frame["target_weight"].sum()) == pytest.approx(0.95, abs=1e-6)
    assert manifest["forced_codes"] == ["603938.SH"]
    assert manifest["forced_min_weight"] == 0.20


def test_pipeline_config_declares_donchian_signal_layer() -> None:
    config = MODULE.read_config(PROJECT_ROOT / "config" / "cn_pipeline.toml")
    signals = config["selection"]["signals"]

    assert signals["enabled"] is True
    assert "donchian_pullback" in signals["recipes"]
    assert "donchian_breakout" in signals["allowed_setup_types"]
    assert signals["recipe_params"]["donchian_pullback"]["window"] == 20
    assert config["selection"]["portfolio_constraints"]["max_holdings"] >= 3


def test_selection_stage_passes_the_signal_layer_config() -> None:
    config = MODULE.read_config(PROJECT_ROOT / "config" / "cn_pipeline.toml")

    class FakeService:
        def select_persisted_model_scores(self, **kwargs):
            self.kwargs = kwargs
            return {"status": "completed", "signals": kwargs.get("signal_config")}

    service = FakeService()
    MODULE.run_stage("selection", config, service)

    assert service.kwargs["signal_config"]["enabled"] is True
    assert "donchian_pullback" in service.kwargs["signal_config"]["recipes"]
