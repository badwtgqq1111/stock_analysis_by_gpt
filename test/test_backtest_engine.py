#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""backtest_engine 本地测试。"""

import sys
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest import backtest_strategy
from backtest_engine import BacktestConfig, BacktestEngine


def _make_price_frame():
    index = pd.date_range("2025-01-01", periods=5, freq="D")
    return pd.DataFrame(
        {
            "Open": [10.0, 11.0, 12.0, 13.0, 14.0],
            "High": [10.5, 11.5, 12.5, 13.5, 14.5],
            "Low": [9.5, 10.5, 11.5, 12.5, 13.5],
            "Close": [10.0, 11.0, 12.0, 13.0, 14.0],
            "Volume": [1000, 1000, 1000, 1000, 1000],
        },
        index=index,
    )


def test_engine_matches_legacy_backtest_entry_and_exit():
    data = _make_price_frame()
    buy_signals = pd.DataFrame(
        [
            {
                "date": data.index[0],
                "signal_strength": 8.0,
                "entry_type": "demo_entry",
                "actionable": True,
                "holding_horizon": 10,
            }
        ]
    )
    sell_signals = pd.DataFrame(
        [
            {
                "date": data.index[1],
                "reasons": ["demo_exit"],
            }
        ]
    )

    legacy_result = backtest_strategy(data, buy_signals, sell_signals, initial_capital=100000, default_holding_days=10)
    engine = BacktestEngine(config=BacktestConfig(initial_capital=100000, default_holding_days=10))
    engine_result = engine.run(data, buy_signals, sell_signals)

    assert engine_result == legacy_result
    assert engine_result["total_trades"] == 1
    assert engine_result["winning_trades"] == 1
    assert engine_result["trades"][0]["type"] == "buy"
    assert engine_result["trades"][1]["type"] == "sell"
    assert engine_result["round_trips"][0]["exit_reason"] == "strategy_sell"
    assert round(engine_result["final_value"], 6) == 109090.0


def test_engine_keeps_open_position_marked_to_market():
    data = _make_price_frame()
    buy_signals = pd.DataFrame(
        [
            {
                "date": data.index[0],
                "signal_strength": 7.0,
                "entry_type": "hold_entry",
                "actionable": True,
                "holding_horizon": 10,
            }
        ]
    )

    engine = BacktestEngine(config=BacktestConfig(initial_capital=100000, default_holding_days=10))
    result = engine.run(data, buy_signals, sell_signals=None)

    assert result["total_trades"] == 0
    assert result["open_position"] is not None
    assert result["open_position"]["entry_type"] == "hold_entry"
    assert len(result["equity_curve"]) == len(data) - 1
    assert round(result["final_value"], 6) == 127270.0


def test_engine_applies_commission_and_slippage_when_configured():
    data = _make_price_frame()
    buy_signals = pd.DataFrame(
        [
            {
                "date": data.index[0],
                "signal_strength": 8.0,
                "entry_type": "cost_entry",
                "actionable": True,
                "holding_horizon": 10,
            }
        ]
    )
    sell_signals = pd.DataFrame(
        [
            {
                "date": data.index[1],
                "reasons": ["cost_exit"],
            }
        ]
    )

    zero_cost_engine = BacktestEngine(config=BacktestConfig(initial_capital=100000, default_holding_days=10))
    zero_cost_result = zero_cost_engine.run(data, buy_signals, sell_signals)

    cost_engine = BacktestEngine(
        config=BacktestConfig(
            initial_capital=100000,
            default_holding_days=10,
            buy_commission_rate=0.001,
            sell_commission_rate=0.001,
            slippage_rate=0.001,
            min_commission=5.0,
        )
    )
    cost_result = cost_engine.run(data, buy_signals, sell_signals)

    assert cost_result["total_commission"] > 0
    assert cost_result["final_value"] < zero_cost_result["final_value"]
    assert cost_result["trades"][0]["commission"] > 0
    assert cost_result["trades"][1]["commission"] > 0
    assert cost_result["round_trips"][0]["entry_commission"] > 0
    assert cost_result["round_trips"][0]["exit_commission"] > 0
    assert cost_result["trades"][0]["price"] > zero_cost_result["trades"][0]["price"]
    assert cost_result["trades"][1]["price"] < zero_cost_result["trades"][1]["price"]


if __name__ == "__main__":
    test_engine_matches_legacy_backtest_entry_and_exit()
    test_engine_keeps_open_position_marked_to_market()
    test_engine_applies_commission_and_slippage_when_configured()
    print("backtest engine tests passed")
