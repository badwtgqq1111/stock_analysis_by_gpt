#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""signal / trade 层本地 smoke test。"""

import sys
import tempfile
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.ingest.service import MarketDataService


def test_signal_and_trade_store_roundtrip():
    signal_frame = pd.DataFrame(
        [
            {
                "date": "2025-01-10",
                "signal_type": "buy",
                "signal_strength": 8.5,
                "score": 72.0,
                "actionable": True,
                "strategy_name": "demo_strategy",
                "signal_source": "unit_test",
            }
        ]
    )
    trade_frame = pd.DataFrame(
        [
            {
                "date": "2025-01-11",
                "trade_type": "buy",
                "price": 10.5,
                "shares": 1000,
                "amount": 10500.0,
                "commission": 5.0,
                "strategy_name": "demo_strategy",
                "order_id": "ORDER-001",
                "trade_source": "unit_test",
            }
        ]
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        service = MarketDataService(base_dir=tmp_dir)
        try:
            signal_result = service.write_signal_frame(
                signal_frame,
                stock_code="00700",
                market="HK",
                signal_set="current_strategy",
                source="unit_test",
            )
            trade_result = service.write_trade_frame(
                trade_frame,
                stock_code="00700",
                market="HK",
                account_id="paper_account",
                strategy_name="demo_strategy",
                source="unit_test",
            )

            assert signal_result["rows"] == 1
            assert trade_result["rows"] == 1

            loaded_signals = service.get_signal_frame(
                stock_code="00700",
                market="HK",
                signal_set="current_strategy",
            )
            loaded_trades = service.get_trade_frame(
                stock_code="00700",
                market="HK",
                account_id="paper_account",
            )

            assert len(loaded_signals) == 1
            assert loaded_signals.iloc[0]["signal_type"] == "buy"
            assert bool(loaded_signals.iloc[0]["actionable"]) is True
            assert loaded_signals.iloc[0]["signal_set"] == "current_strategy"

            assert len(loaded_trades) == 1
            assert loaded_trades.iloc[0]["trade_type"] == "buy"
            assert loaded_trades.iloc[0]["order_id"] == "ORDER-001"
            assert loaded_trades.iloc[0]["account_id"] == "paper_account"
        finally:
            service.close()


def test_persist_backtest_result_writes_signals_and_trades():
    buy_signals = pd.DataFrame(
        [
            {
                "date": "2025-01-01",
                "signal_strength": 8.0,
                "entry_type": "demo_entry",
                "actionable": True,
                "expected_3m_score": 66.0,
            }
        ]
    )
    sell_signals = pd.DataFrame(
        [
            {
                "date": "2025-01-02",
                "reasons": ["demo_exit"],
            }
        ]
    )
    backtest_result = {
        "trades": [
            {
                "date": pd.Timestamp("2025-01-02"),
                "signal_date": pd.Timestamp("2025-01-01"),
                "type": "buy",
                "price": 11.0,
                "shares": 9090,
                "amount": 99990.0,
                "commission": 0.0,
                "signal_strength": 8.0,
                "entry_type": "demo_entry",
            },
            {
                "date": pd.Timestamp("2025-01-03"),
                "signal_date": pd.Timestamp("2025-01-02"),
                "type": "sell",
                "price": 12.0,
                "shares": 9090,
                "amount": 109080.0,
                "commission": 0.0,
                "signal_strength": 8.0,
                "exit_reason": "strategy_sell",
                "exit_category": "strategy_sell",
            },
        ]
    }

    with tempfile.TemporaryDirectory() as tmp_dir:
        service = MarketDataService(base_dir=tmp_dir)
        try:
            persist_result = service.persist_backtest_result(
                stock_code="00700",
                backtest_result=backtest_result,
                buy_signals=buy_signals,
                sell_signals=sell_signals,
                market="HK",
                signal_set="current_strategy",
                strategy_name="demo_strategy",
                account_id="paper_account",
                source="unit_test",
            )

            assert persist_result["signal_rows"] == 2
            assert persist_result["trade_rows"] == 2

            loaded_signals = service.get_signal_frame(
                stock_code="00700",
                market="HK",
                signal_set="current_strategy",
            )
            loaded_trades = service.get_trade_frame(
                stock_code="00700",
                market="HK",
                account_id="paper_account",
            )

            assert len(loaded_signals) == 2
            assert set(loaded_signals["signal_type"]) == {"buy", "sell"}
            assert len(loaded_trades) == 2
            assert list(loaded_trades["trade_type"]) == ["buy", "sell"]
        finally:
            service.close()


def test_persist_portfolio_result_writes_batch_signals():
    portfolio_result = {
        "ranking": [
            {"stock_code": "00001", "ranking_score": 91.0, "current_signal_score": 77.0},
            {"stock_code": "00002", "ranking_score": 85.0, "current_signal_score": 72.0},
        ],
        "selected": [
            {"stock_code": "00001", "ranking_score": 91.0, "current_signal_score": 77.0},
        ],
        "watchlist": [
            {"stock_code": "00003", "ranking_score": 70.0, "current_signal_score": 55.0},
        ],
    }

    with tempfile.TemporaryDirectory() as tmp_dir:
        service = MarketDataService(base_dir=tmp_dir)
        try:
            persist_result = service.persist_portfolio_result(
                portfolio_result=portfolio_result,
                market="HK",
                signal_set="all_hk_topn",
                strategy_name="demo_strategy",
                batch_id="batch_demo_001",
                source="unit_test",
            )

            assert persist_result["signal_rows"] == 4
            assert persist_result["batch_id"] == "batch_demo_001"

            loaded = service.get_signal_frame(
                market="HK",
                signal_set="all_hk_topn",
                batch_id="batch_demo_001",
            )

            assert len(loaded) == 4
            assert set(loaded["signal_type"]) == {"ranking", "selected", "watchlist"}
            assert set(loaded["batch_id"]) == {"batch_demo_001"}
            assert "rank_position" in loaded.columns
        finally:
            service.close()


def test_persist_portfolio_result_writes_signals_in_one_batch():
    portfolio_result = {
        "ranking": [
            {"stock_code": "00001", "ranking_score": 91.0, "current_signal_score": 77.0},
            {"stock_code": "00002", "ranking_score": 85.0, "current_signal_score": 72.0},
            {"stock_code": "00003", "ranking_score": 70.0, "current_signal_score": 55.0},
        ],
        "selected": [
            {"stock_code": "00001", "ranking_score": 91.0, "current_signal_score": 77.0},
        ],
        "watchlist": [
            {"stock_code": "00003", "ranking_score": 70.0, "current_signal_score": 55.0},
        ],
    }

    with tempfile.TemporaryDirectory() as tmp_dir:
        service = MarketDataService(base_dir=tmp_dir)
        calls = []
        original_upsert_signals = service.warehouse.upsert_signals

        def tracking_upsert_signals(frame):
            calls.append(len(frame))
            return original_upsert_signals(frame)

        service.warehouse.upsert_signals = tracking_upsert_signals
        try:
            persist_result = service.persist_portfolio_result(
                portfolio_result=portfolio_result,
                market="HK",
                signal_set="all_hk_topn",
                strategy_name="demo_strategy",
                batch_id="batch_demo_002",
                source="unit_test",
            )

            assert persist_result["signal_rows"] == 5
            assert calls == [5]
        finally:
            service.close()


if __name__ == "__main__":
    test_signal_and_trade_store_roundtrip()
    test_persist_backtest_result_writes_signals_and_trades()
    test_persist_portfolio_result_writes_batch_signals()
    test_persist_portfolio_result_writes_signals_in_one_batch()
    print("signal/trade store tests passed")
