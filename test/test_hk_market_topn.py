#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""港股全市场 TopN 入口测试。"""

import sys
import tempfile
from pathlib import Path
import types
import threading
import time

import pandas as pd
import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if "indicators" not in sys.modules:
    indicators_stub = types.ModuleType("indicators")
    indicators_stub.calculate_technical_indicators = lambda data: data
    sys.modules["indicators"] = indicators_stub

if "reporting" not in sys.modules:
    reporting_stub = types.ModuleType("reporting")
    reporting_stub.generate_strategy_comparison_report = lambda *args, **kwargs: {}
    reporting_stub.generate_trading_strategy = lambda *args, **kwargs: {}
    sys.modules["reporting"] = reporting_stub

if "strategy" not in sys.modules:
    strategy_stub = types.ModuleType("strategy")

    class _DummyBase:
        def identify_buy_signals(self, data, stock_code=None):
            return None

        def identify_sell_signals(self, data):
            return None

    class BuyStrategy(_DummyBase):
        pass

    class SellStrategy(_DummyBase):
        pass

    class CurrentStrategy(BuyStrategy, SellStrategy):
        pass

    strategy_stub.BuyStrategy = BuyStrategy
    strategy_stub.SellStrategy = SellStrategy
    strategy_stub.CurrentStrategy = CurrentStrategy
    strategy_stub.STRATEGY_SUITE = []
    sys.modules["strategy"] = strategy_stub

from analyzer_core import StockAnalyzer
from data.model import normalize_ohlcv_frame
from data.store import DataLayout, MarketDataWarehouse


def test_backtest_portfolio_uses_all_hk_stocks_when_stock_codes_missing():
    original_get_all_stocks = StockAnalyzer.get_all_stocks
    original_analyze_stock = StockAnalyzer.analyze_stock

    def stub_get_all_stocks(self):
        return ["00001", "00002", "00003"]

    def stub_analyze_stock(self, stock_code, days=365):
        return {
            "stock_code": stock_code,
            "data": None,
            "buy_signals": None,
            "sell_signals": None,
            "backtest": {"total_return": float(int(stock_code[-1])), "win_rate": 50.0, "total_trades": 1},
            "latest_price": 10.0,
            "price_change_30d": 1.0,
            "latest_expected_3m_score": 60.0 + float(int(stock_code[-1])),
            "latest_matrix_score": 50.0,
            "latest_regime_score": 50.0,
            "latest_entry_type": "demo_entry",
            "latest_signal_tier": "strong",
            "latest_signal_date": None,
            "current_signal_active": True,
            "current_signal_actionable": True,
            "current_signal_score": 60.0 + float(int(stock_code[-1])),
            "avg_forward_return_60_signal": 5.0,
            "avg_forward_return_60_watch": 0.0,
        }

    StockAnalyzer.get_all_stocks = stub_get_all_stocks
    StockAnalyzer.analyze_stock = stub_analyze_stock
    try:
        analyzer = StockAnalyzer()
        result = analyzer.backtest_portfolio(stock_codes=None, days=120, top_n=2)
    finally:
        StockAnalyzer.get_all_stocks = original_get_all_stocks
        StockAnalyzer.analyze_stock = original_analyze_stock

    assert result is not None
    assert result["stock_pool"] == ["00001", "00002", "00003"]
    assert result["top_n"] == 2
    assert len(result["selected"]) == 2


def test_backtest_hk_market_delegates_to_portfolio_builder():
    original_backtest_portfolio = StockAnalyzer.backtest_portfolio

    def stub_backtest_portfolio(self, stock_codes, days=365, top_n=3, initial_capital=100000, **kwargs):
        return {
            "stock_pool": ["00001", "00002"],
            "top_n": top_n,
            "selected": [{"stock_code": "00001"}],
            "analysis_results": [],
        }

    StockAnalyzer.backtest_portfolio = stub_backtest_portfolio
    try:
        analyzer = StockAnalyzer()
        result = analyzer.backtest_hk_market(days=90, top_n=1)
    finally:
        StockAnalyzer.backtest_portfolio = original_backtest_portfolio

    assert result is not None
    assert result["top_n"] == 1
    assert result["selected"][0]["stock_code"] == "00001"


def test_backtest_portfolio_supports_parallel_analysis():
    original_analyze_stock = StockAnalyzer.analyze_stock

    thread_names = []
    lock = threading.Lock()

    def stub_analyze_stock(self, stock_code, days=365):
        with lock:
            thread_names.append(threading.current_thread().name)
        time.sleep(0.01)
        return {
            "stock_code": stock_code,
            "data": None,
            "buy_signals": None,
            "sell_signals": None,
            "backtest": {"total_return": float(int(stock_code[-1])), "win_rate": 50.0, "total_trades": 1},
            "latest_price": 10.0,
            "price_change_30d": 1.0,
            "latest_expected_3m_score": 60.0 + float(int(stock_code[-1])),
            "latest_matrix_score": 50.0,
            "latest_regime_score": 50.0,
            "latest_entry_type": "demo_entry",
            "latest_signal_tier": "strong",
            "latest_signal_date": None,
            "current_signal_active": True,
            "current_signal_actionable": True,
            "current_signal_score": 60.0 + float(int(stock_code[-1])),
            "avg_forward_return_60_signal": 5.0,
            "avg_forward_return_60_watch": 0.0,
        }

    StockAnalyzer.analyze_stock = stub_analyze_stock
    try:
        analyzer = StockAnalyzer()
        result = analyzer.backtest_portfolio(
            stock_codes=["00001", "00002", "00003", "00004"],
            days=120,
            top_n=2,
            max_workers=4,
        )
    finally:
        StockAnalyzer.analyze_stock = original_analyze_stock

    assert result is not None
    assert len(result["analysis_results"]) == 4
    assert len(set(thread_names)) > 1


def test_stock_analyzer_reads_from_new_data_architecture_without_legacy_db():
    original_database_manager = getattr(sys.modules["analyzer_core"], "DatabaseManager", None)

    class ExplodingDatabaseManager:
        def __init__(self, *args, **kwargs):
            raise AssertionError("legacy DatabaseManager should not be initialized")

    sys.modules["analyzer_core"].DatabaseManager = ExplodingDatabaseManager

    raw_frame = pd.DataFrame(
        {
            "date": ["2025-01-02", "2025-01-03", "2025-01-06"],
            "Open": [100.0, 101.0, 102.0],
            "High": [101.0, 102.0, 103.0],
            "Low": [99.0, 100.0, 101.0],
            "Close": [100.5, 101.5, 102.5],
            "Volume": [1000.0, 1100.0, 1200.0],
        }
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        layout = DataLayout(base_dir=str(Path(tmp_dir) / "data"))
        warehouse = MarketDataWarehouse(layout)
        try:
            warehouse.upsert_ohlcv(
                normalize_ohlcv_frame(raw_frame, stock_code="00700", source="unit_test")
            )
        finally:
            warehouse.close()

        try:
            analyzer = StockAnalyzer(db_dir=tmp_dir)
            stocks = analyzer.get_all_stocks()
            data = analyzer.load_stock_data("00700", days=800)
            analyzer.close()
        finally:
            if original_database_manager is not None:
                sys.modules["analyzer_core"].DatabaseManager = original_database_manager

    assert stocks == ["00700"]
    assert data is not None
    assert list(data.columns) == ["Open", "Close", "High", "Low", "Volume"]
    assert len(data) == 3
    assert str(data.index[-1].date()) == "2025-01-06"


def test_backtest_hk_market_factor_mode_does_not_require_strategy_signals():
    original_analyze_stock = StockAnalyzer.analyze_stock
    original_analyze_stock_factors = getattr(StockAnalyzer, "analyze_stock_factors", None)

    def exploding_analyze_stock(self, stock_code, days=365):
        raise AssertionError("strategy-based analyze_stock should not be used in factor mode")

    def stub_analyze_stock_factors(self, stock_code, days=365, factor_set="qlib_alpha158", factor_score_config=None, persist_features=False):
        score_map = {"00001": 90.0, "00002": 80.0, "00003": 70.0}
        return {
            "stock_code": stock_code,
            "data": None,
            "feature_frame": pd.DataFrame(),
            "factor_scores": {"composite_score": score_map[stock_code]},
            "factor_explanation": {
                "factor_set": factor_set,
                "component_weights": {"trend_score": 0.46, "quality_score": 0.34, "risk_score": 0.20},
                "component_scores": {
                    "trend_score": score_map[stock_code] - 10.0,
                    "quality_score": score_map[stock_code] - 20.0,
                    "risk_score": score_map[stock_code] - 30.0,
                    "composite_score": score_map[stock_code],
                },
                "top_positive_factors": [
                    {"factor": "MA20", "display_name": "MA20", "weight": 0.16, "direction": "lower_is_better", "raw_value": 0.92, "score": 82.0, "weighted_contribution": 13.12},
                    {"factor": "RSV20", "display_name": "RSV20", "weight": 0.10, "direction": "higher_is_better", "raw_value": 0.71, "score": 76.0, "weighted_contribution": 7.6},
                ],
                "top_negative_factors": [
                    {"factor": "STD20", "display_name": "STD20", "weight": 0.38, "direction": "lower_is_better", "raw_value": 0.33, "score": 48.0, "weighted_contribution": 18.24},
                ],
            },
            "backtest": {"total_return": score_map[stock_code] / 10.0, "win_rate": 50.0, "total_trades": 1},
            "latest_price": 10.0,
            "price_change_30d": 1.0,
            "latest_expected_3m_score": score_map[stock_code],
            "latest_matrix_score": score_map[stock_code] - 10.0,
            "latest_regime_score": score_map[stock_code] - 20.0,
            "latest_entry_type": "factor_rank",
            "latest_signal_tier": "strong",
            "latest_signal_date": None,
            "current_signal_active": True,
            "current_signal_actionable": True,
            "current_signal_score": score_map[stock_code],
            "avg_forward_return_60_signal": 5.0,
            "avg_forward_return_60_watch": 0.0,
            "factor_set": factor_set,
            "selection_source": "factor_engine",
        }

    StockAnalyzer.analyze_stock = exploding_analyze_stock
    StockAnalyzer.analyze_stock_factors = stub_analyze_stock_factors
    try:
        analyzer = StockAnalyzer()
        result = analyzer.backtest_hk_market(
            days=120,
            top_n=2,
            analysis_mode="factor",
            stock_codes=["00001", "00002", "00003"],
        )
        analyzer.close()
    finally:
        StockAnalyzer.analyze_stock = original_analyze_stock
        if original_analyze_stock_factors is not None:
            StockAnalyzer.analyze_stock_factors = original_analyze_stock_factors

    assert result is not None
    assert [item["stock_code"] for item in result["selected"]] == ["00001", "00002"]
    assert result["selected"][0]["entry_type"] == "factor_rank"
    assert result["selected"][0]["current_signal_score"] == 90.0
    assert result["selected"][0]["factor_explanation"]["component_scores"]["composite_score"] == 90.0
    assert result["selected"][0]["factor_explanation"]["top_positive_factors"][0]["factor"] == "MA20"


def test_backtest_portfolio_factor_mode_supports_parallel_analysis():
    original_analyze_stock = StockAnalyzer.analyze_stock
    original_analyze_stock_factors = getattr(StockAnalyzer, "analyze_stock_factors", None)

    thread_names = []
    lock = threading.Lock()

    def exploding_analyze_stock(self, stock_code, days=365):
        raise AssertionError("strategy-based analyze_stock should not be used in factor mode")

    def stub_analyze_stock_factors(self, stock_code, days=365, factor_set="qlib_alpha158", factor_score_config=None, persist_features=False):
        with lock:
            thread_names.append(threading.current_thread().name)
        time.sleep(0.01)
        score = 60.0 + float(int(stock_code[-1]))
        return {
            "stock_code": stock_code,
            "data": None,
            "feature_frame": pd.DataFrame(),
            "factor_scores": {"composite_score": score},
            "factor_explanation": {
                "factor_set": factor_set,
                "component_weights": {"trend_score": 0.46, "quality_score": 0.34, "risk_score": 0.20},
                "component_scores": {
                    "trend_score": score - 10.0,
                    "quality_score": score - 20.0,
                    "risk_score": score - 30.0,
                    "composite_score": score,
                },
                "top_positive_factors": [],
                "top_negative_factors": [],
            },
            "backtest": {"total_return": score / 10.0, "win_rate": 50.0, "total_trades": 1},
            "latest_price": 10.0,
            "price_change_30d": 1.0,
            "latest_expected_3m_score": score,
            "latest_matrix_score": score - 10.0,
            "latest_regime_score": score - 20.0,
            "latest_entry_type": "factor_rank",
            "latest_signal_tier": "strong",
            "latest_signal_date": None,
            "current_signal_active": True,
            "current_signal_actionable": True,
            "current_signal_score": score,
            "avg_forward_return_60_signal": 5.0,
            "avg_forward_return_60_watch": 0.0,
            "factor_set": factor_set,
            "selection_source": "factor_engine",
        }

    StockAnalyzer.analyze_stock = exploding_analyze_stock
    StockAnalyzer.analyze_stock_factors = stub_analyze_stock_factors
    try:
        analyzer = StockAnalyzer()
        result = analyzer.backtest_portfolio(
            stock_codes=["00001", "00002", "00003", "00004"],
            days=120,
            top_n=2,
            max_workers=4,
            analysis_mode="factor",
        )
        analyzer.close()
    finally:
        StockAnalyzer.analyze_stock = original_analyze_stock
        if original_analyze_stock_factors is not None:
            StockAnalyzer.analyze_stock_factors = original_analyze_stock_factors

    assert result is not None
    assert len(result["analysis_results"]) == 4
    assert len(set(thread_names)) > 1


def test_build_factor_validation_report_uses_cross_sectional_panel():
    original_load_stock_data = StockAnalyzer.load_stock_data

    def stub_load_stock_data(self, stock_code, days=365):
        dates = pd.date_range("2024-01-02", periods=90, freq="B")
        rank = int(stock_code[-1])
        close = np.linspace(100 + rank, 120 + rank * 2, len(dates))
        return pd.DataFrame(
            {
                "Open": close * 0.99,
                "Close": close,
                "High": close * 1.01,
                "Low": close * 0.98,
                "Volume": np.linspace(1_000_000 + rank * 1_000, 1_200_000 + rank * 1_000, len(dates)),
            },
            index=dates,
        ).rename_axis("date")

    StockAnalyzer.load_stock_data = stub_load_stock_data
    try:
        analyzer = StockAnalyzer()
        result = analyzer.build_factor_validation_report(
            stock_codes=["00001", "00002", "00003", "00004", "00005", "00006"],
            days=60,
            factor_set="qlib_alpha158",
            horizons=(5,),
            quantiles=3,
            min_observations=3,
            max_workers=2,
        )
        analyzer.close()
    finally:
        StockAnalyzer.load_stock_data = original_load_stock_data

    assert result is not None
    assert result["metadata"]["validation_mode"] == "cross_sectional_panel"
    assert not result["validation_frame"].empty
    assert not result["ic_summary"].empty
    assert not result["long_short_summary"].empty
    assert len(result["factor_coverage"]) == 6
    assert result["stock_summary"]["feature_rows"].min() > 0


if __name__ == "__main__":
    test_backtest_portfolio_uses_all_hk_stocks_when_stock_codes_missing()
    test_backtest_hk_market_delegates_to_portfolio_builder()
    test_backtest_portfolio_supports_parallel_analysis()
    test_stock_analyzer_reads_from_new_data_architecture_without_legacy_db()
    test_backtest_hk_market_factor_mode_does_not_require_strategy_signals()
    test_backtest_portfolio_factor_mode_supports_parallel_analysis()
    test_build_factor_validation_report_uses_cross_sectional_panel()
    print("hk market topn tests passed")
