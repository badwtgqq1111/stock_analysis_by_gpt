#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Sanity checks for ``capital_flow_locks.v1`` feature semantics."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factor_engine.expressions.capital_flow_locks import (  # noqa: E402
    FEATURE_VERSION,
    assemble_capital_flow_locks_panel,
    build_event_features,
    feature_group,
    sessions_since_flag,
)


def _toy_inputs(days: int = 40, code: str = "000001.SZ"):
    dates = pd.bdate_range("2026-01-05", periods=days)
    close = pd.Series(np.linspace(10.0, 15.0, days))
    volume = pd.Series(np.full(days, 1_000_000.0))
    bars = pd.DataFrame(
        {
            "stock_code": code,
            "trade_date": dates,
            "open": close.to_numpy(),
            "high": (close * 1.01).to_numpy(),
            "low": (close * 0.99).to_numpy(),
            "close": close.to_numpy(),
            "volume": volume.to_numpy(),
            "amount": (close * volume).to_numpy(),
        }
    )
    flow = pd.DataFrame(
        {
            "stock_code": code,
            "trade_date": dates,
            "buy_sm_amount": 100.0,
            "sell_sm_amount": 100.0,
            "buy_md_amount": 100.0,
            "sell_md_amount": 100.0,
            "buy_lg_amount": np.linspace(200.0, 400.0, days),
            "sell_lg_amount": 150.0,
            "buy_elg_amount": np.linspace(50.0, 300.0, days),
            "sell_elg_amount": 40.0,
            "net_mf_amount": np.linspace(10.0, 90.0, days),
        }
    )
    basic = pd.DataFrame(
        {
            "stock_code": code,
            "trade_date": dates,
            "circ_mv": 5_000_000.0,
            "total_mv": 6_000_000.0,
            "turnover_rate": 1.5,
            "turnover_rate_f": 1.8,
            "volume_ratio": 1.1,
        }
    )
    return bars, flow, basic


def test_cyc_matches_manual_rolling_ratio():
    bars, flow, basic = _toy_inputs()
    panel, manifest = assemble_capital_flow_locks_panel(bars, flow, daily_basic=basic)
    assert manifest["feature_version"] == FEATURE_VERSION
    index = 20
    window = bars.iloc[index - 4 : index + 1]
    expected = window["amount"].sum() / window["volume"].sum()
    assert abs(panel.loc[index, "cyc_5"] - expected) < 1e-9
    # 前复权口径的 CYC 与 close/volume 完全同源
    expected_adj = (window["close"] * window["volume"]).sum() / window["volume"].sum()
    assert abs(panel.loc[index, "cyc_adj_5"] - expected_adj) < 1e-9


def test_no_lookahead_when_future_rows_are_removed():
    bars, flow, basic = _toy_inputs(days=60)
    full, _ = assemble_capital_flow_locks_panel(bars, flow, daily_basic=basic)
    cut = 40
    truncated, _ = assemble_capital_flow_locks_panel(
        bars.iloc[:cut].copy(), flow.iloc[:cut].copy(), daily_basic=basic.iloc[:cut].copy()
    )
    columns = [
        column
        for column in truncated.columns
        if column not in {"stock_code", "trade_date"} and truncated[column].notna().any()
    ]
    left = full.iloc[:cut][columns].reset_index(drop=True)
    right = truncated[columns].reset_index(drop=True)
    for column in columns:
        pd.testing.assert_series_equal(
            left[column], right[column], check_names=False, check_dtype=False, rtol=1e-9, atol=1e-12
        )


def test_missing_moneyflow_keeps_nan_and_mask_without_filling_zero():
    bars, flow, basic = _toy_inputs(days=30)
    flow = flow.copy()
    flow.loc[10, ["buy_lg_amount", "buy_elg_amount", "net_mf_amount"]] = np.nan
    panel, _ = assemble_capital_flow_locks_panel(bars, flow, daily_basic=basic)
    assert np.isnan(panel.loc[10, "main_net_1d"])
    assert panel.loc[10, "main_net_3d_is_missing"] == 1.0
    # 缺失在 3 日窗口内继续传播：11 日的窗口仍包含 10 日
    assert panel.loc[11, "main_net_3d_is_missing"] == 1.0
    assert panel.loc[13, "main_net_3d_is_missing"] == 0.0
    # 缺失没有被填成 0：窗口内仍保留该日期的缺失传播
    assert panel.loc[10, "mf_net_1d_is_missing"] == 1.0


def test_event_zero_differs_from_uncovered_source():
    calendar = pd.DataFrame(
        {"stock_code": "000001.SZ", "trade_date": pd.bdate_range("2026-01-05", periods=10)}
    )
    top_list = pd.DataFrame(
        {
            "trade_date": [pd.Timestamp("2026-01-07")],
            "ts_code": ["000001.SZ"],
            "net_rate": [3.5],
            "amount_rate": [12.0],
            "net_amount": [1_000_000.0],
            "l_buy": [2_000_000.0],
            "l_sell": [1_000_000.0],
        }
    )
    events = build_event_features(
        top_list=top_list, calendar=calendar,
        coverage=("2026-01-06", "2026-01-08"),
    )
    for group_name, group in events.groupby("trade_date"):
        flag = float(group["top_list_flag"].iloc[0])
        if pd.Timestamp("2026-01-06") <= group_name <= pd.Timestamp("2026-01-08"):
            assert flag == (1.0 if group_name == pd.Timestamp("2026-01-07") else 0.0)
        else:
            assert np.isnan(flag), "uncovered trading days must stay missing, not zero"
    covered = events[events["trade_date"] == pd.Timestamp("2026-01-07")]
    assert covered["top_list_days_since"].iloc[0] == 0.0


def test_sessions_since_flag_is_per_stock():
    frame = pd.DataFrame(
        {
            "stock_code": ["000001.SZ"] * 5 + ["000002.SZ"] * 3,
            "trade_date": list(pd.bdate_range("2026-01-05", periods=5)) + list(pd.bdate_range("2026-01-05", periods=3)),
        }
    )
    flag = pd.Series([0, 1, 0, 0, 0, 0, 0, 1], index=frame.index)
    elapsed = sessions_since_flag(frame, flag).tolist()
    assert np.isnan(elapsed[0])
    assert elapsed[1:5] == [0.0, 1.0, 2.0, 3.0]
    assert np.isnan(elapsed[5]) and np.isnan(elapsed[6])
    assert elapsed[7] == 0.0


def test_feature_groups_cover_the_declared_families():
    assert feature_group("mf_net_5d") == "mf"
    assert feature_group("cyc5_slope_3d") == "cyc"
    assert feature_group("cyq_winner_rate") == "cyc"
    assert feature_group("main_strength_3d") == "main"
    assert feature_group("daredevil_score") == "daredevil"
    assert feature_group("hm_net_rate") == "daredevil"
    assert feature_group("three_lock_entry") == "locks"
    assert feature_group("three_lock_entry_is_missing") == "locks"
    assert feature_group("consensus_sign_agreement") == "consensus"
    assert feature_group("dc_net_5d") == "consensus"
    assert feature_group("ret_1d") == "pvx"
    assert feature_group("totally_unknown_feature") is None


def test_three_lock_states_on_a_clean_uptrend():
    bars, flow, basic = _toy_inputs(days=60)
    panel, _ = assemble_capital_flow_locks_panel(bars, flow, daily_basic=basic)
    tail = panel.tail(10)
    assert (tail["trend_lock"] == 1.0).all()
    assert set(tail["three_lock_score"].dropna().unique()) <= {1.0, 2 / 3, 1 / 3, 0.0}
    entry = panel[panel["three_lock_entry"] == 1.0]
    if not entry.empty:
        assert (entry["three_lock_score"] == 1.0).all()
