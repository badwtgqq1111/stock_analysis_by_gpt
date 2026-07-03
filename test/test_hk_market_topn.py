#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""港股全市场 TopN 入口测试。"""

import sys
import tempfile
import os
from pathlib import Path
import types
import threading
import time
from unittest.mock import patch

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

if "portfolio_strategy" not in sys.modules:
    strategy_stub = types.ModuleType("portfolio_strategy")

    strategy_stub.STRATEGY_SUITE = []
    sys.modules["portfolio_strategy"] = strategy_stub

from core import StockAnalyzer
from core.lightgbm_analysis import LightGBMAnalysisMixin
from data.model import normalize_financial_statement_metrics, normalize_ohlcv_frame
from data.store import DataLayout, MarketDataWarehouse


def test_lightgbm_stock_info_frame_to_map_handles_dataframe():
    info_frame = pd.DataFrame(
        [
            {"stock_code": "00700", "industry_l1": "资讯科技业", "industry_l2": "软件服务"},
            {"stock_code": "00005", "industry_l1": "金融业", "industry_l2": "银行"},
        ]
    )

    info_map = LightGBMAnalysisMixin._stock_info_frame_to_map(info_frame)

    assert info_map["00700"]["industry_l2"] == "软件服务"
    assert info_map["00005"]["industry_l1"] == "金融业"


def test_lightgbm_valuation_data_uses_persisted_stock_info_fields():
    info_map = LightGBMAnalysisMixin._stock_info_frame_to_map(
        pd.DataFrame(
            [
                {
                    "stock_code": "700",
                    "pe_ratio": "20.5",
                    "pb_ratio": "3.2",
                    "dividend_yield": "0.8",
                },
                {
                    "stock_code": "00005",
                    "pe_ratio": None,
                    "pb_ratio": "0.9",
                },
            ]
        )
    )

    pe_pb_data, valuation_payload = LightGBMAnalysisMixin._stock_info_map_to_valuation_data(
        ["00700", "00005"],
        info_map,
    )

    assert pe_pb_data["00700"] == (20.5, 3.2)
    assert np.isnan(pe_pb_data["00005"][0])
    assert pe_pb_data["00005"][1] == 0.9
    assert valuation_payload["00700"]["dividend_yield"] == 0.8
    assert np.isnan(valuation_payload["00005"]["pe_ratio"])


def test_lightgbm_local_financial_quality_uses_persisted_statement_metrics():
    with tempfile.TemporaryDirectory() as tmp_dir:
        analyzer = StockAnalyzer(db_dir=tmp_dir, market_read_only=False)
        try:
            stock_info = pd.DataFrame(
                [
                    {
                        "stock_code": "00700",
                        "market": "HK",
                        "exchange": "HKEX",
                        "asset_type": "equity",
                        "industry_l1": "资讯科技业",
                        "industry_l2": "软件服务",
                        "dividend_yield": 0.5,
                        "ingest_time": pd.Timestamp("2026-01-02"),
                    },
                    {
                        "stock_code": "00005",
                        "market": "HK",
                        "exchange": "HKEX",
                        "asset_type": "equity",
                        "industry_l1": "资讯科技业",
                        "industry_l2": "软件服务",
                        "dividend_yield": 0.1,
                        "ingest_time": pd.Timestamp("2026-01-02"),
                    },
                ]
            )
            analyzer.market_warehouse.upsert_stock_info_batch(stock_info.to_dict(orient="records"))
            financial = pd.DataFrame(
                [
                    normalize_financial_statement_metrics(
                        {
                            "report_date": "2025-12-31",
                            "available_at": "2026-03-31",
                            "roe": 0.20,
                            "roa": 0.10,
                            "gross_margin": 0.50,
                            "revenue_yoy": 0.20,
                            "debt_to_assets": 0.20,
                        },
                        stock_code="00700",
                        market="HK",
                        source="unit_test",
                    ),
                    normalize_financial_statement_metrics(
                        {
                            "report_date": "2025-12-31",
                            "available_at": "2026-03-31",
                            "roe": 0.05,
                            "roa": 0.02,
                            "gross_margin": 0.20,
                            "revenue_yoy": -0.05,
                            "debt_to_assets": 0.70,
                        },
                        stock_code="00005",
                        market="HK",
                        source="unit_test",
                    ),
                ]
            )
            analyzer.market_warehouse.upsert_financial_statement_metrics(financial)
            info_map = LightGBMAnalysisMixin._stock_info_frame_to_map(stock_info)

            quality_scores, quality_details = analyzer._load_local_financial_quality(
                ["00700", "00005"],
                info_map,
                asof_date="2026-04-01",
            )
        finally:
            analyzer.close()

    assert quality_scores["00700"] > quality_scores["00005"]
    assert quality_details["00700"]["quality_data_coverage"] > 0
    assert "roe_ind_pct" in quality_details["00700"]


def test_backtest_portfolio_uses_all_hk_stocks_when_stock_codes_missing():
    original_get_all_stocks = StockAnalyzer.get_all_stocks
    original_analyze_lightgbm = getattr(StockAnalyzer, "_analyze_lightgbm_market", None)

    def stub_get_all_stocks(self):
        return ["00001", "00002", "00003"]

    def stub_analyze_lightgbm(self, stock_codes, days=365, **kwargs):
        return [
            {
                "stock_code": sc,
                "data": None,
                "buy_signals": None,
                "sell_signals": None,
                "backtest": {"total_return": float(int(sc[-1])), "win_rate": 50.0, "total_trades": 1},
                "latest_price": 10.0,
                "price_change_30d": 1.0,
                "latest_expected_3m_score": 60.0 + float(int(sc[-1])),
                "latest_matrix_score": 50.0,
                "latest_regime_score": 50.0,
                "latest_entry_type": "lightgbm_rank",
                "latest_signal_tier": "strong",
                "latest_signal_date": None,
                "current_signal_active": True,
                "current_signal_actionable": True,
                "current_signal_score": 60.0 + float(int(sc[-1])),
                "avg_forward_return_60_signal": 5.0,
                "avg_forward_return_60_watch": 0.0,
            }
            for sc in stock_codes
        ]

    StockAnalyzer.get_all_stocks = stub_get_all_stocks
    StockAnalyzer._analyze_lightgbm_market = stub_analyze_lightgbm
    try:
        analyzer = StockAnalyzer()
        result = analyzer.backtest_portfolio(stock_codes=None, days=120, top_n=2)
    finally:
        StockAnalyzer.get_all_stocks = original_get_all_stocks
        if original_analyze_lightgbm is not None:
            StockAnalyzer._analyze_lightgbm_market = original_analyze_lightgbm

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
    original_analyze_stock_factors = getattr(StockAnalyzer, "analyze_stock_factors", None)

    thread_names = []
    lock = threading.Lock()

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
            "factor_explanation": {"factor_set": factor_set, "component_weights": {}, "component_scores": {}, "top_positive_factors": [], "top_negative_factors": []},
            "backtest": {"total_return": float(int(stock_code[-1])), "win_rate": 50.0, "total_trades": 1},
            "latest_price": 10.0,
            "price_change_30d": 1.0,
            "latest_expected_3m_score": score,
            "latest_matrix_score": 50.0,
            "latest_regime_score": 50.0,
            "latest_entry_type": "factor_rank",
            "latest_signal_tier": "strong",
            "latest_signal_date": None,
            "current_signal_active": True,
            "current_signal_actionable": True,
            "current_signal_score": score,
            "avg_forward_return_60_signal": 5.0,
            "avg_forward_return_60_watch": 0.0,
        }

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
    finally:
        if original_analyze_stock_factors is not None:
            StockAnalyzer.analyze_stock_factors = original_analyze_stock_factors

    assert result is not None
    assert len(result["analysis_results"]) == 4
    assert len(set(thread_names)) > 1


def test_backtest_portfolio_supports_lightgbm_analysis_mode():
    original_get_all_stocks = StockAnalyzer.get_all_stocks
    original_analyze_lightgbm_market = getattr(StockAnalyzer, "_analyze_lightgbm_market", None)

    def stub_get_all_stocks(self):
        return ["00001", "00002", "00003"]

    def stub_analyze_lightgbm_market(
        self,
        stock_codes,
        days=365,
        factor_set="qlib_alpha158",
        signal_recipes=None,
        persist_features=False,
        show_progress=False,
        **kwargs,
    ):
        return [
            {
                "stock_code": "00001",
                "data": None,
                "buy_signals": None,
                "sell_signals": None,
                "backtest": {"total_return": 12.0, "win_rate": 60.0, "total_trades": 4},
                "latest_price": 10.0,
                "price_change_30d": 1.0,
                "latest_expected_3m_score": 92.0,
                "latest_matrix_score": 92.0,
                "latest_regime_score": np.nan,
                "latest_entry_type": "lightgbm_rank",
                "latest_signal_tier": "strong",
                "latest_signal_date": None,
                "current_signal_active": True,
                "current_signal_actionable": True,
                "current_signal_score": 92.0,
                "avg_forward_return_60_signal": 8.0,
                "avg_forward_return_60_watch": 0.0,
                "factor_set": factor_set,
                "selection_source": "lightgbm_ranker",
                "factor_explanation": {"top_features": [{"feature_name": "MA5", "importance": 0.8}]},
                "setup_type": "neutral",
                "setup_score": 0.0,
                "sideways_penalty": 0.0,
                "low_price_candidate": True,
                "signal_freshness_score": 100.0,
                "signal_age_days": 0,
            },
            {
                "stock_code": "00002",
                "data": None,
                "buy_signals": None,
                "sell_signals": None,
                "backtest": {"total_return": 8.0, "win_rate": 55.0, "total_trades": 4},
                "latest_price": 12.0,
                "price_change_30d": 0.5,
                "latest_expected_3m_score": 80.0,
                "latest_matrix_score": 80.0,
                "latest_regime_score": np.nan,
                "latest_entry_type": "lightgbm_rank",
                "latest_signal_tier": "medium",
                "latest_signal_date": None,
                "current_signal_active": True,
                "current_signal_actionable": True,
                "current_signal_score": 80.0,
                "avg_forward_return_60_signal": 5.0,
                "avg_forward_return_60_watch": 0.0,
                "factor_set": factor_set,
                "selection_source": "lightgbm_ranker",
                "factor_explanation": {"top_features": [{"feature_name": "MA10", "importance": 0.5}]},
                "setup_type": "neutral",
                "setup_score": 0.0,
                "sideways_penalty": 0.0,
                "low_price_candidate": True,
                "signal_freshness_score": 100.0,
                "signal_age_days": 0,
            },
        ]

    StockAnalyzer.get_all_stocks = stub_get_all_stocks
    StockAnalyzer._analyze_lightgbm_market = stub_analyze_lightgbm_market
    try:
        analyzer = StockAnalyzer()
        result = analyzer.backtest_portfolio(
            stock_codes=None,
            days=120,
            top_n=1,
            analysis_mode="lightgbm",
        )
    finally:
        StockAnalyzer.get_all_stocks = original_get_all_stocks
        if original_analyze_lightgbm_market is not None:
            StockAnalyzer._analyze_lightgbm_market = original_analyze_lightgbm_market

    assert result is not None
    assert result["top_n"] == 1
    assert result["selected"][0]["stock_code"] == "00001"
    assert result["ranking"][0]["selection_source"] == "lightgbm_ranker"


def test_stock_analyzer_reads_from_new_data_architecture_without_legacy_db():
    original_database_manager = getattr(sys.modules["core"], "DatabaseManager", None)

    class ExplodingDatabaseManager:
        def __init__(self, *args, **kwargs):
            raise AssertionError("legacy DatabaseManager should not be initialized")

    sys.modules["core"].DatabaseManager = ExplodingDatabaseManager

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
                sys.modules["core"].DatabaseManager = original_database_manager

    assert stocks == ["00700"]
    assert data is not None
    assert list(data.columns) == ["Open", "Close", "High", "Low", "Volume"]
    assert len(data) == 3
    assert str(data.index[-1].date()) == "2025-01-06"


def test_load_stock_data_batch_returns_per_stock_frames():
    raw_frame = pd.DataFrame(
        {
            "date": ["2025-01-02", "2025-01-03", "2025-01-02", "2025-01-03"],
            "Open": [100.0, 101.0, 200.0, 201.0],
            "High": [101.0, 102.0, 201.0, 202.0],
            "Low": [99.0, 100.0, 199.0, 200.0],
            "Close": [100.5, 101.5, 200.5, 201.5],
            "Volume": [1000.0, 1100.0, 2000.0, 2100.0],
        }
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        layout = DataLayout(base_dir=str(Path(tmp_dir) / "data"))
        warehouse = MarketDataWarehouse(layout)
        try:
            warehouse.upsert_ohlcv(
                normalize_ohlcv_frame(raw_frame.iloc[:2], stock_code="00700", source="unit_test")
            )
            warehouse.upsert_ohlcv(
                normalize_ohlcv_frame(raw_frame.iloc[2:], stock_code="00005", source="unit_test")
            )
        finally:
            warehouse.close()

        analyzer = StockAnalyzer(db_dir=tmp_dir)
        try:
            batch_map = analyzer.load_stock_data_batch(["00700", "00005"], days=800)
        finally:
            analyzer.close()

    assert set(batch_map.keys()) == {"00700", "00005"}
    assert list(batch_map["00700"].columns) == ["Open", "Close", "High", "Low", "Volume"]
    assert len(batch_map["00700"]) == 2
    assert len(batch_map["00005"]) == 2


def test_backtest_hk_market_factor_mode_does_not_require_portfolio_strategy():
    original_analyze_stock_factors = getattr(StockAnalyzer, "analyze_stock_factors", None)

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
        if original_analyze_stock_factors is not None:
            StockAnalyzer.analyze_stock_factors = original_analyze_stock_factors

    assert result is not None
    assert [item["stock_code"] for item in result["selected"]] == ["00001", "00002"]
    assert result["selected"][0]["entry_type"] == "factor_rank"
    assert result["selected"][0]["current_signal_score"] == 90.0
    assert result["selected"][0]["factor_explanation"]["component_scores"]["composite_score"] == 90.0
    assert result["selected"][0]["factor_explanation"]["top_positive_factors"][0]["factor"] == "MA20"


def test_backtest_portfolio_factor_mode_supports_parallel_analysis():
    original_analyze_stock_factors = getattr(StockAnalyzer, "analyze_stock_factors", None)

    thread_names = []
    lock = threading.Lock()

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
    assert result["validation_frame"].empty
    assert not result["ic_summary"].empty
    assert not result["long_short_summary"].empty
    assert len(result["factor_coverage"]) == 6
    assert result["stock_summary"]["feature_rows"].min() > 0
    assert result["metadata"]["validation_batch_size"] >= 1


def test_backtest_portfolio_factor_mode_uses_batch_analysis():
    original_batch_analyzer = getattr(StockAnalyzer, "_analyze_factor_batch", None)

    batch_calls = []

    def stub_analyze_factor_batch(
        self,
        stock_codes,
        days=365,
        factor_set="qlib_alpha158",
        factor_score_config=None,
        persist_features=False,
        ridge_factors=None,
        progress_callback=None,
        batch_index=None,
        total_batches=None,
    ):
        batch_calls.append(list(stock_codes))
        results = []
        for index, stock_code in enumerate(stock_codes, start=1):
            score = 75.0 + index
            results.append(
                {
                    "stock_code": stock_code,
                    "data": pd.DataFrame(),
                    "feature_frame": pd.DataFrame(),
                    "factor_scores": {"composite_score": score},
                    "factor_explanation": {},
                    "backtest": {"total_return": score / 10.0, "win_rate": 60.0, "total_trades": 2},
                    "latest_price": 1.2,
                    "price_change_30d": 5.0,
                    "latest_expected_3m_score": score,
                    "latest_matrix_score": score - 8.0,
                    "latest_regime_score": score - 12.0,
                    "latest_entry_type": "factor_rank",
                    "latest_signal_tier": "strong",
                    "latest_signal_date": pd.Timestamp("2025-01-10"),
                    "current_signal_active": True,
                    "current_signal_actionable": True,
                    "current_signal_score": score,
                    "avg_forward_return_60_signal": 6.0,
                    "avg_forward_return_60_watch": 1.0,
                    "buy_signals": pd.DataFrame(),
                    "factor_set": factor_set,
                    "selection_source": "factor_engine",
                    "setup_type": "pre_breakout",
                    "setup_score": 80.0,
                    "sideways_penalty": 1.0,
                    "signal_freshness_score": 95.0,
                    "signal_age_days": 1,
                }
            )
        return results

    StockAnalyzer._analyze_factor_batch = stub_analyze_factor_batch
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            analyzer = StockAnalyzer(db_dir=tmp_dir)
            result = analyzer.backtest_portfolio(
                stock_codes=["00001", "00002", "00003", "00004", "00005"],
                days=120,
                top_n=2,
                max_workers=2,
                analysis_mode="factor",
            )
            analyzer.close()
    finally:
        if original_batch_analyzer is not None:
            StockAnalyzer._analyze_factor_batch = original_batch_analyzer
        else:
            delattr(StockAnalyzer, "_analyze_factor_batch")

    assert result is not None
    assert len(result["analysis_results"]) == 5
    assert sum(len(batch) for batch in batch_calls) == 5
    assert len(batch_calls) < 5
    assert any(len(batch) > 1 for batch in batch_calls)


def test_factor_analysis_batch_size_scales_down_on_low_memory():
    original_available_memory_bytes = StockAnalyzer._available_memory_bytes

    try:
        StockAnalyzer._available_memory_bytes = staticmethod(lambda: 1 * 1024 ** 3)
        low_memory_batch = StockAnalyzer._resolve_factor_analysis_batch_size(
            total_stocks=2766,
            max_workers=8,
            analysis_mode="factor",
        )

        StockAnalyzer._available_memory_bytes = staticmethod(lambda: 16 * 1024 ** 3)
        high_memory_batch = StockAnalyzer._resolve_factor_analysis_batch_size(
            total_stocks=2766,
            max_workers=8,
            analysis_mode="factor",
        )
    finally:
        StockAnalyzer._available_memory_bytes = original_available_memory_bytes

    assert low_memory_batch >= 1
    assert high_memory_batch >= 1
    assert low_memory_batch < high_memory_batch


def test_backtest_portfolio_factor_mode_reports_batch_progress_details():
    original_batch_analyzer = getattr(StockAnalyzer, "_analyze_factor_batch", None)
    original_stderr = sys.stderr

    class _Buffer:
        def __init__(self):
            self.parts = []

        def write(self, text):
            self.parts.append(str(text))
            return len(str(text))

        def flush(self):
            return None

        def getvalue(self):
            return "".join(self.parts)

    def stub_analyze_factor_batch(
        self,
        stock_codes,
        days=365,
        factor_set="qlib_alpha158",
        factor_score_config=None,
        persist_features=False,
        ridge_factors=None,
        progress_callback=None,
        batch_index=None,
        total_batches=None,
    ):
        results = []
        for stock_code in stock_codes:
            if progress_callback is not None:
                progress_callback(stock_code)
            results.append(
                {
                    "stock_code": stock_code,
                    "data": pd.DataFrame(),
                    "feature_frame": pd.DataFrame(),
                    "factor_scores": {"composite_score": 80.0},
                    "factor_explanation": {},
                    "backtest": {"total_return": 8.0, "win_rate": 60.0, "total_trades": 2},
                    "latest_price": 1.2,
                    "price_change_30d": 5.0,
                    "latest_expected_3m_score": 80.0,
                    "latest_matrix_score": 72.0,
                    "latest_regime_score": 68.0,
                    "latest_entry_type": "factor_rank",
                    "latest_signal_tier": "strong",
                    "latest_signal_date": pd.Timestamp("2025-01-10"),
                    "current_signal_active": True,
                    "current_signal_actionable": True,
                    "current_signal_score": 80.0,
                    "avg_forward_return_60_signal": 6.0,
                    "avg_forward_return_60_watch": 1.0,
                    "buy_signals": pd.DataFrame(),
                    "factor_set": factor_set,
                    "selection_source": "factor_engine",
                    "setup_type": "pre_breakout",
                    "setup_score": 80.0,
                    "sideways_penalty": 1.0,
                    "signal_freshness_score": 95.0,
                    "signal_age_days": 1,
                }
            )
        return results

    StockAnalyzer._analyze_factor_batch = stub_analyze_factor_batch
    sys.stderr = _Buffer()
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            analyzer = StockAnalyzer(db_dir=tmp_dir)
            result = analyzer.backtest_portfolio(
                stock_codes=["00001", "00002", "00003", "00004", "00005"],
                days=120,
                top_n=2,
                max_workers=2,
                analysis_mode="factor",
                show_progress=True,
            )
            output = sys.stderr.getvalue()
            analyzer.close()
    finally:
        sys.stderr = original_stderr
        if original_batch_analyzer is not None:
            StockAnalyzer._analyze_factor_batch = original_batch_analyzer
        else:
            delattr(StockAnalyzer, "_analyze_factor_batch")

    assert result is not None
    assert "phase=batch_factor" in output
    assert "batches_done=" in output
    assert "active_batches=" in output
    assert "stocks_done=" in output
    assert "eta=" in output


def test_backtest_portfolio_lightgbm_mode_reports_progress_details():
    analyzer_module = sys.modules["core"]
    original_load_stock_data_batch = StockAnalyzer.load_stock_data_batch
    original_create_factor_set = analyzer_module.create_factor_set
    original_ranker_cls = analyzer_module.LightGBMRankerPipeline
    original_stderr = sys.stderr

    class _Buffer:
        def __init__(self):
            self.parts = []

        def write(self, text):
            self.parts.append(str(text))
            return len(str(text))

        def flush(self):
            return None

        def getvalue(self):
            return "".join(self.parts)

    class DummyFactor:
        def transform(self, ohlcv_frame, context=None):
            trade_dates = pd.to_datetime(ohlcv_frame["trade_date"])
            return pd.DataFrame(
                {
                    "MA5": np.linspace(1.0, 2.0, len(trade_dates)),
                },
                index=trade_dates,
            )

    class DummyRanker:
        label_horizon = 20

        def fit_predict(self, panel_features, panel_targets):
            feature_index = panel_features.index
            result = panel_features[["stock_code"]].copy()
            result["model_score_raw"] = np.linspace(0.1, 0.9, len(result))
            result["model_score"] = np.linspace(60.0, 90.0, len(result))
            metadata = {
                "train_rows": len(result) // 2,
                "valid_rows": len(result) // 4,
                "feature_count": 1,
                "top_features": [{"feature_name": "MA5", "importance": 1.0, "importance_weight": 1.0}],
            }
            result.index = feature_index
            return result, metadata

    def stub_load_stock_data_batch(self, stock_codes, days=365, **kwargs):
        batch_map = {}
        for idx, stock_code in enumerate(stock_codes, start=1):
            dates = pd.date_range("2024-01-02", periods=80, freq="B")
            close = np.linspace(10 + idx, 14 + idx, len(dates))
            batch_map[stock_code] = pd.DataFrame(
                {
                    "Open": close * 0.99,
                    "Close": close,
                    "High": close * 1.01,
                    "Low": close * 0.98,
                    "Volume": np.linspace(1000, 2000, len(dates)),
                },
                index=dates,
            ).rename_axis("date")
        return batch_map

    StockAnalyzer.load_stock_data_batch = stub_load_stock_data_batch
    analyzer_module.create_factor_set = lambda *args, **kwargs: DummyFactor()
    analyzer_module.LightGBMRankerPipeline = DummyRanker
    sys.stderr = _Buffer()
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            analyzer = StockAnalyzer(db_dir=tmp_dir)
            result = analyzer.backtest_portfolio(
                stock_codes=["00001", "00002", "00003"],
                days=120,
                top_n=2,
                analysis_mode="lightgbm",
                show_progress=True,
            )
            output = sys.stderr.getvalue()
            analyzer.close()
    finally:
        sys.stderr = original_stderr
        StockAnalyzer.load_stock_data_batch = original_load_stock_data_batch
        analyzer_module.create_factor_set = original_create_factor_set
        analyzer_module.LightGBMRankerPipeline = original_ranker_cls

    assert result is not None
    assert "phase=lightgbm_prepare" in output
    assert "phase=lightgbm_finalize" in output
    assert "feature_ready=" in output
    assert "prepared=" in output
    assert "predicted=" in output
    assert "stocks_done=" in output
    assert "eta=" in output


def test_validation_frame_helpers_trim_columns():
    feature_frame = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2024-01-02"]),
            "stock_code": ["00001"],
            "market": ["HK"],
            "exchange": ["XHKG"],
            "asset_type": ["equity"],
            "frequency": ["daily"],
            "adjust": ["qfq"],
            "feature_set": ["alpha_demo"],
            "feature_name": ["MA20"],
            "feature_value": [1.23],
            "source": ["unit_test"],
            "ingest_time": [pd.Timestamp("2024-01-02")],
        }
    )
    ohlcv_frame = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2024-01-02"]),
            "stock_code": ["00001"],
            "market": ["HK"],
            "exchange": ["XHKG"],
            "asset_type": ["equity"],
            "frequency": ["daily"],
            "adjust": ["qfq"],
            "close": [12.3],
            "open": [12.0],
            "high": [12.5],
            "low": [11.8],
            "volume": [1000],
            "source": ["unit_test"],
        }
    )

    trimmed_feature = StockAnalyzer._trim_validation_feature_frame(feature_frame)
    trimmed_ohlcv = StockAnalyzer._trim_validation_ohlcv_frame(ohlcv_frame)

    assert "source" not in trimmed_feature.columns
    assert "ingest_time" not in trimmed_feature.columns
    assert set(trimmed_feature.columns) == {
        "trade_date", "stock_code", "market", "exchange", "asset_type",
        "frequency", "adjust", "feature_set", "feature_version",
        "feature_config_hash", "feature_name", "feature_value",
    }
    assert set(trimmed_ohlcv.columns) == {
        "trade_date", "stock_code", "market", "exchange", "asset_type",
        "frequency", "adjust", "close",
    }


def test_validation_batch_size_scales_down_without_dropping_stocks():
    original_available_memory_bytes = StockAnalyzer._available_memory_bytes

    StockAnalyzer._available_memory_bytes = staticmethod(lambda: 1 * 1024 ** 3)
    try:
        all_scope_batch = StockAnalyzer._resolve_validation_batch_size("all", requested_workers=8)
        scoring_scope_batch = StockAnalyzer._resolve_validation_batch_size("scoring_only", requested_workers=8)
    finally:
        StockAnalyzer._available_memory_bytes = original_available_memory_bytes

    assert all_scope_batch >= 1
    assert scoring_scope_batch >= 1
    assert all_scope_batch < scoring_scope_batch


def test_build_factor_validation_report_uses_feature_cache_when_fresh():
    original_load_stock_data = StockAnalyzer.load_stock_data

    call_count = {"value": 0}

    def stub_load_stock_data(self, stock_code, days=365):
        call_count["value"] += 1
        dates = pd.date_range("2024-01-02", periods=40, freq="B")
        rank = int(stock_code[-1])
        close = np.linspace(10 + rank, 20 + rank * 1.5, len(dates))
        return pd.DataFrame(
            {
                "Open": close * 0.99,
                "Close": close,
                "High": close * 1.01,
                "Low": close * 0.98,
                "Volume": np.linspace(1000, 2000, len(dates)),
            },
            index=dates,
        ).rename_axis("date")

    StockAnalyzer.load_stock_data = stub_load_stock_data
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            analyzer = StockAnalyzer(db_dir=tmp_dir)
            kwargs = dict(
                stock_codes=["00001", "00002", "00003"],
                days=30,
                factor_set="qlib_alpha158",
                horizons=(5,),
                quantiles=3,
                min_observations=3,
                max_workers=1,
            )
            first = analyzer.build_factor_validation_report(**kwargs)
            first_calls = call_count["value"]
            second = analyzer.build_factor_validation_report(**kwargs)
            analyzer.close()
    finally:
        StockAnalyzer.load_stock_data = original_load_stock_data

    assert first is not None
    assert second is not None
    assert first_calls == 3
    assert call_count["value"] == first_calls
    assert not second["ic_summary"].empty


def test_build_factor_validation_report_refreshes_stale_feature_cache():
    original_load_stock_data = StockAnalyzer.load_stock_data

    call_count = {"value": 0}

    def stub_load_stock_data(self, stock_code, days=365):
        call_count["value"] += 1
        dates = pd.date_range("2024-01-02", periods=40, freq="B")
        rank = int(stock_code[-1])
        close = np.linspace(10 + rank, 20 + rank, len(dates))
        return pd.DataFrame(
            {
                "Open": close * 0.99,
                "Close": close,
                "High": close * 1.01,
                "Low": close * 0.98,
                "Volume": np.linspace(1000, 2000, len(dates)),
            },
            index=dates,
        ).rename_axis("date")

    StockAnalyzer.load_stock_data = stub_load_stock_data
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            analyzer = StockAnalyzer(db_dir=tmp_dir)
            kwargs = dict(
                stock_codes=["00001", "00002"],
                days=30,
                factor_set="qlib_alpha158",
                horizons=(5,),
                quantiles=3,
                min_observations=3,
                max_workers=1,
            )
            first = analyzer.build_factor_validation_report(**kwargs)
            cache_dir = analyzer.get_validation_feature_cache_dir()
            cache_files = list(cache_dir.glob("*.pkl"))
            assert cache_files
            stale_time = time.time() - 8 * 24 * 60 * 60
            for cache_file in cache_files:
                os.utime(cache_file, (stale_time, stale_time))
            before_refresh = call_count["value"]
            second = analyzer.build_factor_validation_report(**kwargs)
            analyzer.close()
    finally:
        StockAnalyzer.load_stock_data = original_load_stock_data

    assert first is not None
    assert second is not None
    assert before_refresh == 2
    assert call_count["value"] == 4


def test_build_factor_validation_report_reuses_feature_store_after_pickle_cache_removed():
    import factor_engine as factor_engine_module

    def _make_frame(stock_code, periods=50):
        dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=periods)
        offset = int(stock_code[-1])
        close = np.linspace(10 + offset, 20 + offset, len(dates))
        raw = pd.DataFrame(
            {
                "date": dates,
                "Open": close * 0.99,
                "Close": close,
                "High": close * 1.01,
                "Low": close * 0.98,
                "Volume": np.linspace(1000, 2000, len(dates)),
            }
        )
        return normalize_ohlcv_frame(raw, stock_code=stock_code, market="HK", source="unit_test")

    transform_calls = {"value": 0}
    real_create_factor_set = factor_engine_module.create_factor_set

    def wrapped_create_factor_set(name, **kwargs):
        factor = real_create_factor_set(name, **kwargs)
        original_transform = factor.transform

        def counting_transform(*args, **inner_kwargs):
            transform_calls["value"] += 1
            return original_transform(*args, **inner_kwargs)

        factor.transform = counting_transform
        return factor

    with tempfile.TemporaryDirectory() as tmp_dir:
        analyzer = StockAnalyzer(db_dir=tmp_dir, market_read_only=False)
        try:
            analyzer.market_warehouse.upsert_ohlcv(
                pd.concat([_make_frame("00001"), _make_frame("00002")], ignore_index=True)
            )
            kwargs = dict(
                stock_codes=["00001", "00002"],
                days=30,
                factor_set="qlib_alpha158",
                horizons=(5,),
                quantiles=3,
                min_observations=3,
                max_workers=1,
            )

            with patch("factor_engine.create_factor_set", side_effect=wrapped_create_factor_set):
                first = analyzer.build_factor_validation_report(**kwargs)
                first_calls = transform_calls["value"]

                cache_dir = analyzer.get_validation_feature_cache_dir()
                for cache_file in cache_dir.glob("*.pkl"):
                    cache_file.unlink()

                second = analyzer.build_factor_validation_report(**kwargs)
        finally:
            analyzer.close()

    assert first is not None
    assert second is not None
    assert first_calls == 2
    assert transform_calls["value"] == first_calls


def test_build_factor_validation_report_records_feature_materialization_metadata():
    from factor_engine import build_feature_materialization_metadata, create_factor_set

    original_load_stock_data = StockAnalyzer.load_stock_data

    def stub_load_stock_data(self, stock_code, days=365):
        dates = pd.date_range("2024-01-02", periods=40, freq="B")
        close = np.linspace(10, 20, len(dates))
        return pd.DataFrame(
            {
                "Open": close * 0.99,
                "Close": close,
                "High": close * 1.01,
                "Low": close * 0.98,
                "Volume": np.linspace(1000, 2000, len(dates)),
            },
            index=dates,
        ).rename_axis("date")

    validated_feature_names = ["MA20", "STD20"]
    restricted_config = StockAnalyzer._parse_scoring_factors_to_alpha158_config(validated_feature_names)
    factor_template = create_factor_set("qlib_alpha158", config=restricted_config)
    expected_materialization = build_feature_materialization_metadata(
        factor_set="qlib_alpha158",
        metadata=factor_template.metadata().to_dict(),
        config=restricted_config,
    )

    StockAnalyzer.load_stock_data = stub_load_stock_data
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            analyzer = StockAnalyzer(db_dir=tmp_dir)
            result = analyzer.build_factor_validation_report(
                stock_codes=["00001", "00002"],
                days=30,
                factor_set="qlib_alpha158",
                horizons=(5,),
                quantiles=3,
                min_observations=3,
                max_workers=1,
                validation_factor_scope="scoring_only",
                validated_feature_names=validated_feature_names,
            )
            analyzer.close()
    finally:
        StockAnalyzer.load_stock_data = original_load_stock_data

    assert result is not None
    assert result["metadata"]["feature_materialization"] == expected_materialization


def test_analyze_factor_batch_handles_duplicate_panel_dates_without_series_truth_error(monkeypatch):
    analyzer_module = sys.modules["core"]
    original_load_stock_data = StockAnalyzer.load_stock_data
    original_create_factor_set = analyzer_module.create_factor_set
    original_compute_factor_scores = StockAnalyzer._compute_factor_scores

    class DummyFactor:
        def transform(self, ohlcv_frame, context=None):
            trade_dates = pd.to_datetime(ohlcv_frame["trade_date"])
            return pd.DataFrame(
                {
                    "MA5": np.linspace(1.0, 2.0, len(trade_dates)),
                },
                index=trade_dates,
            )

    def stub_load_stock_data(self, stock_code, days=365):
        dates = pd.date_range("2024-01-02", periods=80, freq="B")
        base = 10 + int(stock_code[-1])
        close = np.linspace(base, base + 4, len(dates))
        return pd.DataFrame(
            {
                "Open": close * 0.99,
                "Close": close,
                "High": close * 1.01,
                "Low": close * 0.98,
                "Volume": np.linspace(1000, 2000, len(dates)),
            },
            index=dates,
        ).rename_axis("date")

    def stub_compute_factor_scores(self, panel_features, factor_set=None, score_config=None, ridge_factors=None):
        size = len(panel_features)
        panel_scores = pd.DataFrame(
            {
                "trend_score": np.linspace(60.0, 80.0, size),
                "quality_score": np.linspace(55.0, 75.0, size),
                "risk_score": np.linspace(40.0, 50.0, size),
                "composite_score": np.linspace(65.0, 85.0, size),
            },
            index=panel_features.index,
        )
        factor_details = {
            "factor_set": factor_set,
            "component_weights": {"trend_score": 0.4, "quality_score": 0.3, "risk_score": 0.15},
            "factors": {
                "MA5": {
                    "component": "trend",
                    "weight": 0.14,
                    "direction": "lower_is_better",
                    "raw_series": panel_features["MA5"],
                    "score_series": pd.Series(np.linspace(50.0, 90.0, size), index=panel_features.index),
                }
            },
        }
        return panel_scores, factor_details

    monkeypatch.setattr(StockAnalyzer, "load_stock_data", stub_load_stock_data)
    monkeypatch.setattr(analyzer_module, "create_factor_set", lambda *args, **kwargs: DummyFactor())
    monkeypatch.setattr(StockAnalyzer, "_compute_factor_scores", stub_compute_factor_scores)

    with tempfile.TemporaryDirectory() as tmp_dir:
        analyzer = StockAnalyzer(db_dir=tmp_dir)
        results = analyzer._analyze_factor_batch(
            ["00001", "00002"],
            days=60,
            factor_set="qlib_alpha158",
        )
        analyzer.close()

    assert len(results) == 2
    assert results[0]["factor_explanation"]["top_positive_factors"]
    assert "signal_freshness_score" in results[0]
    assert "signal_age_days" in results[0]


def test_signal_freshness_score_uses_data_date_reference():
    score, age_days = StockAnalyzer._signal_freshness_score(
        pd.Timestamp("2025-01-08"),
        pd.Timestamp("2025-01-10"),
    )

    missing_score, missing_age_days = StockAnalyzer._signal_freshness_score(None, pd.Timestamp("2025-01-10"))

    assert age_days == 2
    assert score == 92.0
    assert missing_age_days == 999
    assert missing_score == 0.0


if __name__ == "__main__":
    test_backtest_portfolio_uses_all_hk_stocks_when_stock_codes_missing()
    test_backtest_hk_market_delegates_to_portfolio_builder()
    test_backtest_portfolio_supports_parallel_analysis()
    test_backtest_portfolio_supports_lightgbm_analysis_mode()
    test_stock_analyzer_reads_from_new_data_architecture_without_legacy_db()
    test_backtest_hk_market_factor_mode_does_not_require_portfolio_strategy()
    test_backtest_portfolio_factor_mode_supports_parallel_analysis()
    test_build_factor_validation_report_uses_cross_sectional_panel()
    test_backtest_portfolio_factor_mode_uses_batch_analysis()
    test_validation_frame_helpers_trim_columns()
    test_validation_batch_size_scales_down_without_dropping_stocks()
    test_backtest_portfolio_lightgbm_mode_reports_progress_details()
    print("hk market topn tests passed")
