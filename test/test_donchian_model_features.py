#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Donchian 面板特征进入模型训练链路的测试。"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from factor_engine.ml.panel_dataset import (
    PRICE_FEATURE_COLUMNS,
    _derive_price_features,
    build_feature_panel,
    compact_training_panel,
)


DONCHIAN_FEATURES = [
    "pv_donchian_pos_20", "pv_donchian_width_20", "pv_donchian_dist_upper_20",
    "pv_donchian_break_20", "pv_donchian_break_count_20", "pv_donchian_since_break_20",
    "pv_donchian_break_volume_20", "pv_donchian_pierce_20", "pv_donchian_pierce_volume_20",
]


def _bars(close_values, volume_values=None, bars=60):
    dates = pd.date_range("2026-04-01", periods=len(close_values), freq="B")
    close = pd.Series(close_values, index=dates, dtype=float)
    volume = pd.Series(volume_values if volume_values is not None else np.full(len(close), 2_000_000.0), index=dates, dtype=float)
    return pd.DataFrame(
        {
            "stock_code": "603938.SH",
            "trade_date": dates,
            "close": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "open": close,
            "volume": volume,
            "amount": close * volume,
            "turnover": 0.01,
        }
    )


def test_donchian_features_are_registered_as_price_features() -> None:
    for feature in DONCHIAN_FEATURES:
        assert feature in PRICE_FEATURE_COLUMNS


def test_derive_price_features_channel_state_matches_manual_window() -> None:
    close = np.concatenate([np.full(40, 10.0), np.linspace(10.1, 11.0, 20)])
    frame = _derive_price_features(_bars(close))
    high = pd.Series(close) * 1.01
    low = pd.Series(close) * 0.99
    upper = high.rolling(20, min_periods=20).max()
    lower = low.rolling(20, min_periods=20).min()
    last = len(close) - 1

    assert frame["pv_donchian_pos_20"].iloc[last] == pytest.approx(
        (close[last] - lower.iloc[last]) / (upper.iloc[last] - lower.iloc[last]), abs=1e-6
    )
    assert frame["pv_donchian_width_20"].iloc[last] == pytest.approx(
        (upper.iloc[last] - lower.iloc[last]) / close[last], abs=1e-6
    )
    assert frame["pv_donchian_dist_upper_20"].iloc[last] == pytest.approx(
        close[last] / upper.iloc[last] - 1.0, abs=1e-6
    )


def test_breakout_flag_uses_the_previous_channel_not_the_current_bar() -> None:
    # A single spike must be flagged on its own bar and never re-flagged later,
    # which proves the breakout comparison never reads the current channel top.
    close = np.concatenate([np.full(30, 10.0), [12.0], np.full(9, 11.0)])
    frame = _derive_price_features(_bars(close))
    flags = frame["pv_donchian_break_20"].to_numpy(dtype=float)

    assert flags[30] == 1.0
    assert np.nansum(flags[31:]) == 0.0
    assert frame["pv_donchian_since_break_20"].iloc[30] == 0.0
    assert frame["pv_donchian_since_break_20"].iloc[31] == 1.0
    assert frame["pv_donchian_break_count_20"].iloc[39] == 1.0


def test_pierce_flag_marks_intraday_break_closed_back_inside() -> None:
    close = np.concatenate([np.full(25, 10.0), np.full(4, 10.0), [10.02]])
    bars = _bars(close)
    bars.loc[bars.index[-1], "high"] = 10.50  # pierces the channel top, closes back inside
    bars.loc[bars.index[-1], "low"] = 9.98
    frame = _derive_price_features(bars)

    assert frame["pv_donchian_pierce_20"].iloc[-1] == 1.0
    assert frame["pv_donchian_break_20"].iloc[-1] == 0.0
    assert frame["pv_donchian_pierce_volume_20"].iloc[-1] > 0


def test_donchian_columns_become_model_features_in_the_clean_panel() -> None:
    close = np.concatenate([np.full(40, 10.0), np.linspace(10.1, 11.14, 19), [12.0]])
    bars = _bars(close)
    dates = bars["trade_date"]
    factors = pd.DataFrame(
        {
            "trade_date": list(dates) * 1,
            "stock_code": "603938.SH",
            "feature_name": "DEMO_FACTOR",
            "feature_value": np.linspace(0.0, 1.0, len(dates)),
            "ingest_time": pd.Timestamp("2026-09-10"),
        }
    )
    panel = build_feature_panel(
        factors, bars, market="CN", frequency="daily", adjust="qfq", factor_set="alpha_zoo_hk",
    )
    compact, model_columns, _ = compact_training_panel(
        panel, feature_columns=["DEMO_FACTOR"] + PRICE_FEATURE_COLUMNS, cleaning_version="p0.2.v1",
    )

    for feature in DONCHIAN_FEATURES:
        assert f"{feature}_clean" in model_columns
        assert f"{feature}_is_missing" in model_columns
    last = compact.iloc[-1]
    assert not pd.isna(last["pv_donchian_pos_20_clean"])
    assert last["pv_donchian_pos_20_clean"] > 0.5
    assert last["pv_donchian_break_20_clean"] == 1.0
    # Model feature discovery keys off the `_clean` suffix used by the trainer.
    assert all(column.endswith(("_clean", "_is_missing")) for column in model_columns)
