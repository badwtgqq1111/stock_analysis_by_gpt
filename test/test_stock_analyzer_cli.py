#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""stock_analyzer CLI 测试。"""

import importlib
import io
import json
import sys
import tempfile
import types
from contextlib import redirect_stdout
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _install_stubs():
    for module_name in (
        "cli",
        "cli.all_hk",
        "cli.factor_report",
        "cli.formatters",
        "cli.generate_factors",
        "cli.helpers",
        "cli.main",
        "cli.review_batch",
        "cli.select_stocks",
        "cli.strategy_suite",
        "cli.validate_factors",
        "stock_analyzer",
    ):
        sys.modules.pop(module_name, None)

    analyzer_core_stub = types.ModuleType("analyzer_core")

    class StockAnalyzer:
        last_call = None
        last_validation_call = None
        init_count = 0

        def __init__(self, *args, **kwargs):
            type(self).init_count += 1

        @staticmethod
        def get_score_factor_names(score_config=None):
            return ["MA20"]

        @staticmethod
        def _select_top_ridge_factors(scorecard, top_k=30):
            working = scorecard.copy() if isinstance(scorecard, pd.DataFrame) else pd.DataFrame()
            return working.head(int(top_k or 30))

        def backtest_hk_market(
            self,
            days=365,
            top_n=3,
            initial_capital=100000,
            max_workers=1,
            analysis_mode="factor",
            factor_set="qlib_alpha158",
            factor_score_config=None,
            show_progress=False,
            enable_portfolio_replay=True,
            signal_recipes=None,
            max_features=0,
            ridge_factors=None,
        ):
            type(self).last_call = {
                "days": days,
                "top_n": top_n,
                "initial_capital": initial_capital,
                "max_workers": max_workers,
                "analysis_mode": analysis_mode,
                "factor_set": factor_set,
                "factor_score_config": factor_score_config,
                "show_progress": show_progress,
                "enable_portfolio_replay": enable_portfolio_replay,
                "signal_recipes": signal_recipes,
            }
            if int(max_features or 0):
                type(self).last_call["max_features"] = int(max_features)
            if ridge_factors is not None:
                type(self).last_call["ridge_factors_rows"] = len(ridge_factors)
            return {
                "stock_pool": ["00001", "00002", "00003"],
                "top_n": top_n,
                "selected": [{"stock_code": "00001"}, {"stock_code": "00002"}][:top_n],
                "ranking": [
                    {"stock_code": "00001", "ranking_score": 90.0},
                    {"stock_code": "00002", "ranking_score": 80.0},
                ],
                "watchlist": [{"stock_code": "00003", "ranking_score": 70.0}],
                "estimated_portfolio_return": 12.5,
                "estimated_portfolio_win_rate": 66.0,
                "estimated_trade_count": 8,
                "analysis_results": [{"stock_code": "00001"}, {"stock_code": "00002"}],
            }

        def get_all_stocks(self):
            return ["00001", "00002", "00003"]

        def build_factor_validation_report(
            self,
            stock_codes=None,
            days=365,
            factor_set="qlib_alpha158",
            factor_score_config=None,
            horizons=(1, 5, 10, 20),
            quantiles=5,
            min_observations=5,
            max_workers=1,
            show_progress=False,
            validation_factor_scope="all",
            validated_feature_names=None,
        ):
            type(self).last_validation_call = {
                "stock_codes": list(stock_codes or []),
                "days": days,
                "factor_set": factor_set,
                "factor_score_config": factor_score_config,
                "horizons": tuple(horizons),
                "quantiles": quantiles,
                "min_observations": min_observations,
                "max_workers": max_workers,
                "show_progress": show_progress,
                "validation_factor_scope": validation_factor_scope,
                "validated_feature_names": list(validated_feature_names or []),
            }
            type(self).last_call = {
                "stock_codes": list(stock_codes or []),
                "days": days,
                "factor_set": factor_set,
                "factor_score_config": factor_score_config,
                "horizons": tuple(horizons),
                "quantiles": quantiles,
                "min_observations": min_observations,
                "max_workers": max_workers,
                "show_progress": show_progress,
                "validation_factor_scope": validation_factor_scope,
                "validated_feature_names": list(validated_feature_names or []),
            }
            return {
                "metadata": {
                    "factor_set": factor_set,
                    "stock_count": len(stock_codes or []),
                    "success_count": len(stock_codes or []),
                    "validation_mode": "cross_sectional_panel",
                    "horizons": tuple(horizons),
                    "quantiles": quantiles,
                    "min_observations": min_observations,
                    "validation_factor_scope": validation_factor_scope,
                    "validated_feature_names": list(validated_feature_names or []),
                    "feature_materialization": {
                        "feature_set": factor_set,
                        "feature_version": "0.1.0",
                        "feature_config_hash": "stubcfg1234567890",
                    },
                    "factor_score_config": {
                        "trend": {"MA20": {"weight": 0.0, "higher_is_better": False}},
                        "weights": {"trend_score": 1.0, "quality_score": 0.0, "risk_score": 0.0},
                    },
                },
                "stock_summary": pd.DataFrame([
                    {"stock_code": "00001", "mean_ic": 0.1, "mean_rank_ic": 0.2, "mean_spread": 0.03, "mean_turnover": 0.4}
                ]),
                "factor_coverage": pd.DataFrame([
                    {"stock_code": "00001", "feature_rows": 10, "ic_rows": 3, "feature_names": 1}
                ]),
                "ic_summary": pd.DataFrame([
                    {"feature_name": "MA20", "horizon": 5, "mean_ic": 0.1, "mean_rank_ic": 0.2, "ic_positive_rate": 0.7, "rank_ic_positive_rate": 0.8, "ic_ir": 1.2, "rank_ic_ir": 1.5}
                ]),
                "quantile_summary": pd.DataFrame([
                    {"feature_name": "MA20", "horizon": 5, "quantile": 5, "mean_return": 0.03, "avg_observation_count": 8, "valid_dates": 12}
                ]),
                "long_short_summary": pd.DataFrame([
                    {"feature_name": "MA20", "horizon": 5, "mean_spread": 0.03, "spread_ir": 1.1, "positive_rate": 0.75, "valid_dates": 12}
                ]),
                "turnover_summary": pd.DataFrame([
                    {"feature_name": "MA20", "quantile": 5, "mean_turnover": 0.4, "max_turnover": 0.8, "valid_dates": 12}
                ]),
                "decay_summary": pd.DataFrame([
                    {"feature_name": "MA20", "horizon": 5, "ic_decay_ratio": 0.9, "rank_ic_decay_ratio": 0.95, "spread_decay_ratio": 0.92}
                ]),
            }

        def build_signal_recipe_report(
            self,
            stock_codes=None,
            days=365,
            signal_recipes=None,
            horizons=(20, 40, 60),
            max_workers=1,
            show_progress=False,
            min_history_days=60,
            signal_cooldown_days=20,
            signal_event_policy="first",
        ):
            type(self).last_call = {
                "stock_codes": list(stock_codes or []),
                "days": days,
                "signal_recipes": tuple(signal_recipes or ()),
                "horizons": tuple(horizons),
                "max_workers": max_workers,
                "show_progress": show_progress,
                "min_history_days": min_history_days,
                "signal_cooldown_days": signal_cooldown_days,
                "signal_event_policy": signal_event_policy,
            }
            return {
                "metadata": {
                    "stock_count": len(stock_codes or []),
                    "raw_event_count": 2,
                    "event_count": 1,
                    "days": days,
                    "signal_recipes": tuple(signal_recipes or ()),
                    "horizons": tuple(horizons),
                    "signal_cooldown_days": signal_cooldown_days,
                    "signal_event_policy": signal_event_policy,
                },
                "summary": pd.DataFrame(
                    [
                        {
                            "recipe_name": "range_breakout",
                            "setup_type": "range_breakout",
                            "event_count": 1,
                            "avg_setup_score": 80.0,
                            "avg_forward_return_5": 0.05,
                            "win_rate_5": 1.0,
                            "avg_forward_max_drawdown_5": -0.02,
                        }
                    ]
                ),
                "events": pd.DataFrame(
                    [
                        {
                            "stock_code": "00001",
                            "date": pd.Timestamp("2025-01-10"),
                            "recipe_name": "range_breakout",
                            "setup_type": "range_breakout",
                            "setup_score": 80.0,
                            "forward_return_5": 0.05,
                        }
                    ]
                ),
                "events_raw": pd.DataFrame(
                    [
                        {
                            "stock_code": "00001",
                            "date": pd.Timestamp("2025-01-10"),
                            "recipe_name": "range_breakout",
                            "setup_type": "range_breakout",
                            "setup_score": 80.0,
                            "forward_return_5": 0.05,
                        },
                        {
                            "stock_code": "00001",
                            "date": pd.Timestamp("2025-01-11"),
                            "recipe_name": "range_breakout",
                            "setup_type": "range_breakout",
                            "setup_score": 81.0,
                            "forward_return_5": 0.04,
                        },
                    ]
                ),
            }

    analyzer_core_stub.StockAnalyzer = StockAnalyzer
    sys.modules["analyzer_core"] = analyzer_core_stub

    data_ingest_service_stub = types.ModuleType("data.ingest.service")

    class MarketDataService:
        last_persist_call = None
        last_get_signal_call = None
        last_generate_call = None
        last_get_all_stock_codes_call = None

        def __init__(self, *args, **kwargs):
            pass

        def get_all_stock_codes(
            self,
            market="HK",
            asset_type="equity",
            frequency="daily",
            adjust="qfq",
        ):
            type(self).last_get_all_stock_codes_call = {
                "market": market,
                "asset_type": asset_type,
                "frequency": frequency,
                "adjust": adjust,
            }
            return ["00001", "00002", "00003"]

        def generate_factor_set(
            self,
            stock_codes=None,
            factor_set="qlib_alpha158",
            market="HK",
            frequency="daily",
            adjust="qfq",
            days=365,
            max_workers=1,
            show_progress=False,
        ):
            type(self).last_generate_call = {
                "stock_codes": list(stock_codes or []),
                "factor_set": factor_set,
                "market": market,
                "frequency": frequency,
                "adjust": adjust,
                "days": days,
                "max_workers": max_workers,
                "show_progress": show_progress,
            }
            return {
                "stock_count": len(stock_codes or []),
                "success_count": len(stock_codes or []),
                "skipped_count": 0,
                "error_count": 0,
                "rows_written": 579,
                "dataset_path": "./assets/data/feature/features",
                "factor_materialization": {
                    "feature_set": factor_set,
                    "feature_version": "0.1.0",
                    "feature_config_hash": "stubcfg1234567890",
                },
                "results": [
                    {
                        "stock_code": stock_code,
                        "status": "computed",
                        "rows": 193,
                    }
                    for stock_code in (stock_codes or [])
                ],
            }

        def persist_portfolio_result(
            self,
            portfolio_result,
            market="HK",
            signal_set="portfolio_scan",
            strategy_name=None,
            batch_id=None,
            source="portfolio_builder",
        ):
            type(self).last_persist_call = {
                "market": market,
                "signal_set": signal_set,
                "strategy_name": strategy_name,
                "batch_id": batch_id,
                "source": source,
                "selected_count": len(portfolio_result.get("selected", [])),
            }
            return {
                "market": market,
                "signal_rows": 4,
                "batch_id": batch_id or "stub_batch_001",
            }

        def get_signal_frame(
            self,
            stock_code=None,
            market=None,
            exchange=None,
            asset_type=None,
            frequency=None,
            adjust="qfq",
            signal_set=None,
            signal_type=None,
            batch_id=None,
            strategy_name=None,
            start_date=None,
            end_date=None,
        ):
            type(self).last_get_signal_call = {
                "market": market,
                "signal_set": signal_set,
                "batch_id": batch_id,
            }
            return pd.DataFrame(
                [
                    {"stock_code": "00001", "signal_type": "ranking", "batch_id": batch_id, "rank_position": 1, "score": 90.0},
                    {"stock_code": "00002", "signal_type": "ranking", "batch_id": batch_id, "rank_position": 2, "score": 80.0},
                    {"stock_code": "00001", "signal_type": "selected", "batch_id": batch_id, "rank_position": 1, "score": 90.0},
                    {"stock_code": "00003", "signal_type": "watchlist", "batch_id": batch_id, "rank_position": 1, "score": 70.0},
                ]
            )

        def close(self):
            return None

    data_ingest_service_stub.MarketDataService = MarketDataService
    sys.modules["data.ingest.service"] = data_ingest_service_stub

    reporting_stub = types.ModuleType("reporting")
    reporting_stub.analyze_buy_points = lambda *args, **kwargs: {}
    reporting_stub.analyze_target_date_alignment = lambda *args, **kwargs: []
    reporting_stub.build_strategy_comparison_tables = lambda *args, **kwargs: {}
    reporting_stub.create_visualization_charts = lambda *args, **kwargs: None
    reporting_stub.format_table_for_console = lambda table: str(table)
    sys.modules["reporting"] = reporting_stub

    strategy_stub = types.ModuleType("strategy_signals")

    class _DummyBase:
        pass

    strategy_stub.BuyStrategy = _DummyBase
    strategy_stub.SellStrategy = _DummyBase
    strategy_stub.CurrentStrategy = _DummyBase
    sys.modules["strategy_signals"] = strategy_stub

    return analyzer_core_stub.StockAnalyzer, data_ingest_service_stub.MarketDataService


def test_run_cli_supports_all_hk_mode():
    stock_analyzer_cls, _ = _install_stubs()
    if "stock_analyzer" in sys.modules:
        del sys.modules["stock_analyzer"]
    stock_analyzer = importlib.import_module("stock_analyzer")
    select_stocks_module = importlib.import_module("cli.select_stocks")

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        stock_analyzer.run_cli(["all_hk", "--top-n", "2", "--days", "120", "--initial-capital", "200000"])

    output = buffer.getvalue()
    assert stock_analyzer_cls.last_call == {
        "days": 120,
        "top_n": 2,
        "initial_capital": 200000.0,
        "max_workers": 0,
        "analysis_mode": "factor",
        "factor_set": "qlib_alpha158",
        "factor_score_config": None,
        "show_progress": False,
        "enable_portfolio_replay": True,
        "signal_recipes": None,
    }
    assert "全港股 Top 2" in output
    assert "成功分析 2 只股票" in output


def test_run_cli_all_hk_supports_max_workers():
    stock_analyzer_cls, _ = _install_stubs()
    if "stock_analyzer" in sys.modules:
        del sys.modules["stock_analyzer"]
    stock_analyzer = importlib.import_module("stock_analyzer")

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        stock_analyzer.run_cli(
            ["all_hk", "--top-n", "2", "--days", "120", "--max-workers", "8"]
        )

    assert stock_analyzer_cls.last_call == {
        "days": 120,
        "top_n": 2,
        "initial_capital": 100000,
        "max_workers": 8,
        "analysis_mode": "factor",
        "factor_set": "qlib_alpha158",
        "factor_score_config": None,
        "show_progress": False,
        "enable_portfolio_replay": True,
        "signal_recipes": None,
    }


def test_run_cli_all_hk_supports_strategy_mode_override():
    stock_analyzer_cls, _ = _install_stubs()
    if "stock_analyzer" in sys.modules:
        del sys.modules["stock_analyzer"]
    stock_analyzer = importlib.import_module("stock_analyzer")

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        stock_analyzer.run_cli(
            ["all_hk", "--top-n", "2", "--analysis-mode", "strategy"]
        )

    assert stock_analyzer_cls.last_call == {
        "days": 365,
        "top_n": 2,
        "initial_capital": 100000,
        "max_workers": 0,
        "analysis_mode": "strategy",
        "factor_set": "qlib_alpha158",
        "factor_score_config": None,
        "show_progress": False,
        "enable_portfolio_replay": True,
        "signal_recipes": None,
    }


def test_run_cli_all_hk_supports_progress_and_fast_mode():
    stock_analyzer_cls, _ = _install_stubs()
    if "stock_analyzer" in sys.modules:
        del sys.modules["stock_analyzer"]
    stock_analyzer = importlib.import_module("stock_analyzer")

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        stock_analyzer.run_cli(
            ["all_hk", "--top-n", "2", "--show-progress", "--fast-mode"]
        )

    assert stock_analyzer_cls.last_call == {
        "days": 365,
        "top_n": 2,
        "initial_capital": 100000,
        "max_workers": 0,
        "analysis_mode": "factor",
        "factor_set": "qlib_alpha158",
        "factor_score_config": None,
        "show_progress": True,
        "enable_portfolio_replay": False,
        "signal_recipes": None,
    }


def test_run_cli_select_stocks_supports_signal_recipes():
    stock_analyzer_cls, _ = _install_stubs()
    if "stock_analyzer" in sys.modules:
        del sys.modules["stock_analyzer"]
    stock_analyzer = importlib.import_module("stock_analyzer")
    select_stocks_module = importlib.import_module("cli.select_stocks")

    with tempfile.TemporaryDirectory() as tmp_dir:
        analyzer = stock_analyzer.StockAnalyzer()
        cache_dir = Path(tmp_dir) / "factor_weight_cache"
        validation_stock_codes = analyzer.get_all_stocks()
        cache_key, _ = stock_analyzer._build_validation_cache_key(
            factor_set="qlib_alpha158",
            validation_days=365,
            validation_horizons=(1, 5, 10, 20),
            validation_quantiles=5,
            validation_min_observations=5,
            validation_stock_codes=validation_stock_codes,
            validation_factor_scope="scoring_only",
            validated_feature_names=analyzer.get_score_factor_names(),
        )
        cache_payload = {
            "factor_score_config": {
                "trend": {"MA20": {"weight": 1.0, "higher_is_better": False}},
                "weights": {"trend_score": 1.0, "quality_score": 0.0, "risk_score": 0.0},
            },
            "factor_scorecard": [
                {
                    "feature_name": "MA20",
                    "component": "trend",
                    "validation_score": 1.0,
                    "recommended_factor_weight": 1.0,
                }
                ],
            }

        cache_path = Path(cache_dir) / f"{cache_key}.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(stock_analyzer.json.dumps(cache_payload), encoding="utf-8")

        original_cache_dir = select_stocks_module._get_validation_cache_dir
        select_stocks_module._get_validation_cache_dir = lambda _analyzer: cache_dir
        buffer = io.StringIO()
        try:
            with redirect_stdout(buffer):
                stock_analyzer.run_cli(
                    [
                        "select_stocks",
                        "--validation-factor-scope",
                        "scoring_only",
                        "--signal-recipes",
                        "low_price_setup,range_breakout",
                        "--export-csv",
                        str(Path(tmp_dir) / "selected"),
                    ]
                )
        finally:
            select_stocks_module._get_validation_cache_dir = original_cache_dir

    assert stock_analyzer_cls.last_call["signal_recipes"] == ("low_price_setup", "range_breakout")


def test_run_cli_select_stocks_supports_lightgbm_mode():
    stock_analyzer_cls, _ = _install_stubs()
    if "stock_analyzer" in sys.modules:
        del sys.modules["stock_analyzer"]
    stock_analyzer = importlib.import_module("stock_analyzer")

    with tempfile.TemporaryDirectory() as tmp_dir:
        analyzer = stock_analyzer.StockAnalyzer()
        cache_dir = Path(tmp_dir) / "factor_weight_cache"
        validation_stock_codes = analyzer.get_all_stocks()
        cache_key, _ = stock_analyzer._build_validation_cache_key(
            factor_set="qlib_alpha158",
            validation_days=365,
            validation_horizons=(1, 5, 10, 20),
            validation_quantiles=5,
            validation_min_observations=5,
            validation_stock_codes=validation_stock_codes,
            validation_factor_scope="scoring_only",
            validated_feature_names=analyzer.get_score_factor_names(),
        )
        cache_payload = {
            "factor_score_config": {
                "trend": {"MA20": {"weight": 1.0, "higher_is_better": False}},
                "weights": {"trend_score": 1.0, "quality_score": 0.0, "risk_score": 0.0},
            },
            "factor_scorecard": [
                {
                    "feature_name": "MA20",
                    "component": "trend",
                    "validation_score": 1.0,
                    "recommended_factor_weight": 1.0,
                }
            ],
        }

        cache_path = Path(cache_dir) / f"{cache_key}.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(stock_analyzer.json.dumps(cache_payload), encoding="utf-8")

        original_cache_dir = stock_analyzer._get_validation_cache_dir
        stock_analyzer._get_validation_cache_dir = lambda _analyzer: cache_dir
        try:
            stock_analyzer.run_cli(
                [
                    "select_stocks",
                    "--analysis-mode",
                    "lightgbm",
                ]
            )
        finally:
            stock_analyzer._get_validation_cache_dir = original_cache_dir

    assert stock_analyzer_cls.last_call["analysis_mode"] == "lightgbm"


def test_run_cli_supports_generate_factors_mode():
    stock_analyzer_cls, market_data_service_cls = _install_stubs()
    if "stock_analyzer" in sys.modules:
        del sys.modules["stock_analyzer"]
    stock_analyzer = importlib.import_module("stock_analyzer")

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        stock_analyzer.run_cli(
            [
                "generate_factors",
                "--days",
                "180",
                "--factor-set",
                "qlib_alpha158",
                "--stock-limit",
                "2",
                "--max-workers",
                "4",
                "--show-progress",
            ]
        )

    assert stock_analyzer_cls.last_call is None
    assert stock_analyzer_cls.init_count == 0
    assert market_data_service_cls.last_get_all_stock_codes_call == {
        "market": "HK",
        "asset_type": "equity",
        "frequency": "daily",
        "adjust": "qfq",
    }
    assert market_data_service_cls.last_generate_call == {
        "stock_codes": ["00001", "00002"],
        "factor_set": "qlib_alpha158",
        "market": "HK",
        "frequency": "daily",
        "adjust": "qfq",
        "days": 180,
        "max_workers": 4,
        "show_progress": True,
    }


def test_run_cli_generate_factors_writes_run_manifest():
    _, market_data_service_cls = _install_stubs()
    if "stock_analyzer" in sys.modules:
        del sys.modules["stock_analyzer"]
    stock_analyzer = importlib.import_module("stock_analyzer")

    with tempfile.TemporaryDirectory() as tmp_dir:
        export_base = Path(tmp_dir) / "generated_factors"
        result = stock_analyzer.run_cli(
            [
                "generate_factors",
                "--days",
                "180",
                "--factor-set",
                "qlib_alpha158",
                "--stock-limit",
                "2",
                "--max-workers",
                "4",
                "--export-csv",
                str(export_base),
            ]
        )

        manifest_path = Path(result["manifest_path"])
        assert manifest_path.exists()
        assert market_data_service_cls.last_generate_call["stock_codes"] == ["00001", "00002"]

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["run_type"] == "generate_factors"
        assert manifest["params"]["factor_set"] == "qlib_alpha158"
        assert manifest["params"]["stock_limit"] == 2
        assert manifest["params"]["days"] == 180
        assert manifest["factor_materialization"]["feature_config_hash"] == "stubcfg1234567890"
        assert manifest["artifacts"]["summary_csv_path"].endswith("_summary.csv")


def test_run_cli_all_hk_supports_export_csv():
    _install_stubs()
    if "stock_analyzer" in sys.modules:
        del sys.modules["stock_analyzer"]
    stock_analyzer = importlib.import_module("stock_analyzer")

    with tempfile.TemporaryDirectory() as tmp_dir:
        export_base = Path(tmp_dir) / "all_hk_topn"
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            stock_analyzer.run_cli(
                ["all_hk", "--top-n", "2", "--export-csv", str(export_base)]
            )

        ranking_path = export_base.with_name(f"{export_base.stem}_ranking.csv")
        selected_path = export_base.with_name(f"{export_base.stem}_selected.csv")
        watchlist_path = export_base.with_name(f"{export_base.stem}_watchlist.csv")

        assert ranking_path.exists()
        assert selected_path.exists()
        assert watchlist_path.exists()

        ranking_df = pd.read_csv(ranking_path, dtype={"stock_code": str})
        selected_df = pd.read_csv(selected_path, dtype={"stock_code": str})
        watchlist_df = pd.read_csv(watchlist_path, dtype={"stock_code": str})

        assert list(ranking_df["stock_code"]) == ["00001", "00002"]
        assert list(selected_df["stock_code"]) == ["00001", "00002"]
        assert list(watchlist_df["stock_code"]) == ["00003"]


def test_run_cli_all_hk_supports_persist_signals():
    _, market_data_service_cls = _install_stubs()
    if "stock_analyzer" in sys.modules:
        del sys.modules["stock_analyzer"]
    stock_analyzer = importlib.import_module("stock_analyzer")

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        stock_analyzer.run_cli(
            ["all_hk", "--top-n", "2", "--persist-signals", "--batch-id", "batch_cli_001"]
        )

    output = buffer.getvalue()
    assert market_data_service_cls.last_persist_call == {
        "market": "HK",
        "signal_set": "all_hk_topn",
        "strategy_name": "all_hk_topn",
        "batch_id": "batch_cli_001",
        "source": "stock_analyzer_cli",
        "selected_count": 2,
    }
    assert "batch_cli_001" in output


def test_run_cli_supports_review_batch_mode():
    _, market_data_service_cls = _install_stubs()
    if "stock_analyzer" in sys.modules:
        del sys.modules["stock_analyzer"]
    stock_analyzer = importlib.import_module("stock_analyzer")

    with tempfile.TemporaryDirectory() as tmp_dir:
        export_base = Path(tmp_dir) / "review_batch"
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            stock_analyzer.run_cli(
                [
                    "review_batch",
                    "batch_cli_001",
                    "--export-csv",
                    str(export_base),
                ]
            )

        output = buffer.getvalue()
        assert market_data_service_cls.last_get_signal_call == {
            "market": "HK",
            "signal_set": "all_hk_topn",
            "batch_id": "batch_cli_001",
        }
        assert "batch_cli_001" in output

        ranking_path = export_base.with_name(f"{export_base.stem}_ranking.csv")
        summary_path = export_base.with_name(f"{export_base.stem}_summary.csv")
        selected_path = export_base.with_name(f"{export_base.stem}_selected.csv")
        watchlist_path = export_base.with_name(f"{export_base.stem}_watchlist.csv")

        assert ranking_path.exists()
        assert summary_path.exists()
        assert selected_path.exists()
        assert watchlist_path.exists()

        summary_df = pd.read_csv(summary_path)
        assert summary_df.iloc[0]["batch_id"] == "batch_cli_001"
        assert int(summary_df.iloc[0]["ranking_count"]) == 2
        assert int(summary_df.iloc[0]["selected_count"]) == 1
        assert int(summary_df.iloc[0]["watchlist_count"]) == 1
        assert "平均评分" in output


def test_run_cli_supports_factor_report_mode():
    stock_analyzer_cls, _ = _install_stubs()
    if "stock_analyzer" in sys.modules:
        del sys.modules["stock_analyzer"]
    stock_analyzer = importlib.import_module("stock_analyzer")

    with tempfile.TemporaryDirectory() as tmp_dir:
        export_base = Path(tmp_dir) / "factor_report"
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            stock_analyzer.run_cli(
                [
                    "factor_report",
                    "--days",
                    "180",
                    "--factor-set",
                    "qlib_alpha158",
                    "--max-workers",
                    "4",
                    "--horizons",
                    "5,10",
                    "--quantiles",
                    "4",
                    "--min-observations",
                    "6",
                    "--stock-limit",
                    "2",
                    "--signal-cooldown-days",
                    "15",
                    "--signal-event-policy",
                    "best_score",
                    "--export-csv",
                    str(export_base),
                ]
            )

        output = buffer.getvalue()
        assert stock_analyzer_cls.last_call == {
            "stock_codes": ["00001", "00002"],
            "days": 180,
            "factor_set": "qlib_alpha158",
            "factor_score_config": None,
            "horizons": (5, 10),
            "quantiles": 4,
            "min_observations": 6,
            "max_workers": 4,
            "show_progress": False,
            "validation_factor_scope": "all",
            "validated_feature_names": [],
        }
        assert "因子验证报告" in output
        assert export_base.with_name(f"{export_base.stem}_factor_scorecard.csv").exists()
        assert export_base.with_name(f"{export_base.stem}_metadata.json").exists()


def test_run_cli_factor_report_writes_run_manifest():
    _install_stubs()
    if "stock_analyzer" in sys.modules:
        del sys.modules["stock_analyzer"]
    stock_analyzer = importlib.import_module("stock_analyzer")

    with tempfile.TemporaryDirectory() as tmp_dir:
        export_base = Path(tmp_dir) / "factor_report"
        result = stock_analyzer.run_cli(
            [
                "factor_report",
                "--days",
                "180",
                "--factor-set",
                "qlib_alpha158",
                "--stock-limit",
                "2",
                "--export-csv",
                str(export_base),
            ]
        )

        manifest_path = Path(result["manifest_path"])
        assert manifest_path.exists()

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["run_type"] == "factor_report"
        assert manifest["params"]["factor_set"] == "qlib_alpha158"
        assert manifest["params"]["stock_limit"] == 2
        assert manifest["factor_materialization"]["feature_config_hash"] == "stubcfg1234567890"
        assert manifest["artifacts"]["metadata_json_path"].endswith("_metadata.json")


def test_run_cli_validate_factors_writes_run_manifest():
    _install_stubs()
    if "stock_analyzer" in sys.modules:
        del sys.modules["stock_analyzer"]
    stock_analyzer = importlib.import_module("stock_analyzer")

    with tempfile.TemporaryDirectory() as tmp_dir:
        export_base = Path(tmp_dir) / "validation_scorecard"
        result = stock_analyzer.run_cli(
            [
                "validate_factors",
                "--factor-set",
                "qlib_alpha158",
                "--days",
                "180",
                "--export-csv",
                str(export_base),
            ]
        )

        manifest_path = Path(result["manifest_path"])
        assert manifest_path.exists()

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["run_type"] == "validate_factors"
        assert manifest["params"]["factor_set"] == "qlib_alpha158"
        assert manifest["params"]["days"] == 180
        assert manifest["artifacts"]["scorecard_csv_path"].endswith("_scorecard.csv")
        assert manifest["artifacts"]["validation_cache_key"] == result["cache_key"]


def test_run_cli_select_stocks_writes_run_manifest_with_validation_cache_reference():
    stock_analyzer_cls, _ = _install_stubs()
    if "stock_analyzer" in sys.modules:
        del sys.modules["stock_analyzer"]
    stock_analyzer = importlib.import_module("stock_analyzer")
    select_stocks_module = importlib.import_module("cli.select_stocks")

    with tempfile.TemporaryDirectory() as tmp_dir:
        analyzer = stock_analyzer_cls()
        cache_dir = Path(tmp_dir) / "factor_weight_cache"
        validation_stock_codes = analyzer.get_all_stocks()
        cache_key, _ = stock_analyzer._build_validation_cache_key(
            factor_set="qlib_alpha158",
            validation_days=365,
            validation_horizons=(1, 5, 10, 20),
            validation_quantiles=5,
            validation_min_observations=5,
            validation_stock_codes=validation_stock_codes,
            validation_factor_scope="scoring_only",
            validated_feature_names=analyzer.get_score_factor_names(),
        )
        cache_payload = {
            "factor_score_config": {
                "trend": {"MA20": {"weight": 1.0, "higher_is_better": False}},
                "weights": {"trend_score": 1.0, "quality_score": 0.0, "risk_score": 0.0},
            },
            "factor_scorecard": [
                {
                    "feature_name": "MA20",
                    "component": "trend",
                    "validation_score": 1.0,
                    "recommended_factor_weight": 1.0,
                }
            ],
            "feature_materialization": {
                "feature_set": "qlib_alpha158",
                "feature_version": "0.1.0",
                "feature_config_hash": "demo1234abcd5678",
            },
        }
        cache_path = Path(cache_dir) / f"{cache_key}.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(stock_analyzer.json.dumps(cache_payload), encoding="utf-8")

        original_cache_dir = select_stocks_module._get_validation_cache_dir
        select_stocks_module._get_validation_cache_dir = lambda _analyzer: cache_dir
        try:
            export_base = Path(tmp_dir) / "selected_topn"
            result = stock_analyzer.run_cli(
                [
                    "select_stocks",
                    "--top-n",
                    "2",
                    "--validation-factor-scope",
                    "scoring_only",
                    "--export-csv",
                    str(export_base),
                ]
            )
        finally:
            select_stocks_module._get_validation_cache_dir = original_cache_dir

        manifest_path = Path(result["manifest_path"])
        assert manifest_path.exists()

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["run_type"] == "select_stocks"
        assert manifest["params"]["top_n"] == 2
        assert manifest["upstream"]["validation_cache_key"] == cache_key
        assert manifest["factor_materialization"]["feature_config_hash"] == "demo1234abcd5678"


def test_run_cli_supports_signal_report_mode():
    stock_analyzer_cls, _ = _install_stubs()
    if "stock_analyzer" in sys.modules:
        del sys.modules["stock_analyzer"]
    stock_analyzer = importlib.import_module("stock_analyzer")

    with tempfile.TemporaryDirectory() as tmp_dir:
        export_base = Path(tmp_dir) / "signal_report"
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            stock_analyzer.run_cli(
                [
                    "signal_report",
                    "--days",
                    "180",
                    "--signal-recipes",
                    "range_breakout,box_pullback",
                    "--horizons",
                    "5,20",
                    "--max-workers",
                    "4",
                    "--show-progress",
                    "--stock-limit",
                    "2",
                    "--signal-cooldown-days",
                    "15",
                    "--signal-event-policy",
                    "best_score",
                    "--export-csv",
                    str(export_base),
                ]
            )

        output = buffer.getvalue()
        assert stock_analyzer_cls.last_call == {
            "stock_codes": ["00001", "00002"],
            "days": 180,
            "signal_recipes": ("range_breakout", "box_pullback"),
            "horizons": (5, 20),
            "max_workers": 4,
            "show_progress": True,
            "min_history_days": 60,
            "signal_cooldown_days": 15,
            "signal_event_policy": "best_score",
        }
        assert "信号配方验证报告" in output
        assert export_base.with_name(f"{export_base.stem}_signal_summary.csv").exists()
        assert export_base.with_name(f"{export_base.stem}_signal_events.csv").exists()
        assert export_base.with_name(f"{export_base.stem}_signal_events_raw.csv").exists()
        assert export_base.with_name(f"{export_base.stem}_metadata.json").exists()


def test_run_cli_all_hk_supports_recommended_factor_weights_mode():
    stock_analyzer_cls, _ = _install_stubs()
    if "stock_analyzer" in sys.modules:
        del sys.modules["stock_analyzer"]
    stock_analyzer = importlib.import_module("stock_analyzer")

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        stock_analyzer.run_cli(
            [
                "all_hk",
                "--top-n",
                "2",
                "--use-recommended-factor-weights",
                "--validation-factor-scope",
                "scoring_only",
                "--validation-days",
                "180",
                "--validation-horizons",
                "5,10",
                "--validation-quantiles",
                "4",
                "--validation-min-observations",
                "6",
                "--validation-stock-limit",
                "2",
                "--refresh-recommended-factor-weights",
            ]
        )

    output = buffer.getvalue()
    assert stock_analyzer_cls.last_call["days"] == 365
    assert stock_analyzer_cls.last_call["top_n"] == 2
    assert stock_analyzer_cls.last_call["analysis_mode"] == "factor"
    assert stock_analyzer_cls.last_call["factor_set"] == "qlib_alpha158"
    assert stock_analyzer_cls.last_call["factor_score_config"]["trend"]["MA20"]["weight"] == 1.0
    assert stock_analyzer_cls.last_validation_call["validation_factor_scope"] == "scoring_only"
    assert stock_analyzer_cls.last_validation_call["validated_feature_names"] == ["MA20"]
    assert "已启用验证驱动权重模式" in output


def test_run_cli_all_hk_writes_run_manifest_with_validation_reference():
    _install_stubs()
    if "stock_analyzer" in sys.modules:
        del sys.modules["stock_analyzer"]
    stock_analyzer = importlib.import_module("stock_analyzer")
    all_hk_module = importlib.import_module("cli.all_hk")

    with tempfile.TemporaryDirectory() as tmp_dir:
        cache_dir = Path(tmp_dir) / "factor_weight_cache"
        export_base = Path(tmp_dir) / "all_hk_topn"
        original_cache_dir = all_hk_module._get_validation_cache_dir
        all_hk_module._get_validation_cache_dir = lambda _analyzer: cache_dir
        try:
            result = stock_analyzer.run_cli(
                [
                    "all_hk",
                    "--top-n",
                    "2",
                    "--use-recommended-factor-weights",
                    "--refresh-recommended-factor-weights",
                    "--validation-days",
                    "180",
                    "--validation-stock-limit",
                    "2",
                    "--export-csv",
                    str(export_base),
                ]
            )
        finally:
            all_hk_module._get_validation_cache_dir = original_cache_dir

        manifest_path = Path(result["manifest_path"])
        assert manifest_path.exists()

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["run_type"] == "all_hk"
        assert manifest["params"]["top_n"] == 2
        assert manifest["params"]["use_recommended_factor_weights"] is True
        assert manifest["params"]["validation_factor_scope"] == "all"
        assert manifest["upstream"]["validation_cache_key"]
        assert manifest["factor_materialization"]["feature_config_hash"] == "stubcfg1234567890"
        assert manifest["artifacts"]["ranking_csv_path"].endswith("_ranking.csv")


def test_merge_recommended_factor_weights_supports_feature_name_rows():
    stock_analyzer_cls, _ = _install_stubs()
    if "stock_analyzer" in sys.modules:
        del sys.modules["stock_analyzer"]
    stock_analyzer = importlib.import_module("stock_analyzer")

    merged = stock_analyzer._merge_recommended_factor_weights(
        {
            "trend": {"MA20": {"weight": 0.0, "higher_is_better": False}},
            "weights": {"trend_score": 1.0, "quality_score": 0.0, "risk_score": 0.0},
        },
        pd.DataFrame(
            [
                {
                    "feature_name": "MA20",
                    "component": "trend",
                    "recommended_factor_weight": 1.0,
                }
            ]
        ),
    )
    assert merged["trend"]["MA20"]["weight"] == 1.0


def test_build_validation_cache_key_distinguishes_factor_scope():
    _install_stubs()
    if "stock_analyzer" in sys.modules:
        del sys.modules["stock_analyzer"]
    stock_analyzer = importlib.import_module("stock_analyzer")

    key_all, identity_all = stock_analyzer._build_validation_cache_key(
        factor_set="qlib_alpha158",
        validation_days=365,
        validation_horizons=(1, 5, 10, 20),
        validation_quantiles=5,
        validation_min_observations=5,
        validation_stock_codes=["00001", "00002"],
        validation_factor_scope="all",
        validated_feature_names=None,
    )
    key_scoring, identity_scoring = stock_analyzer._build_validation_cache_key(
        factor_set="qlib_alpha158",
        validation_days=365,
        validation_horizons=(1, 5, 10, 20),
        validation_quantiles=5,
        validation_min_observations=5,
        validation_stock_codes=["00001", "00002"],
        validation_factor_scope="scoring_only",
        validated_feature_names=["MA20"],
    )

    assert key_all != key_scoring
    assert identity_all["validation_factor_scope"] == "all"
    assert identity_all["validated_feature_names"] == []
    assert identity_scoring["validation_factor_scope"] == "scoring_only"
    assert identity_scoring["validated_feature_names"] == ["MA20"]


def test_validation_scorecard_cache_with_inf_is_rejected():
    _install_stubs()
    if "stock_analyzer" in sys.modules:
        del sys.modules["stock_analyzer"]
    stock_analyzer = importlib.import_module("stock_analyzer")

    dirty = pd.DataFrame(
        [
            {
                "feature_name": "MA20",
                "component": pd.NA,
                "validation_score": float("inf"),
                "recommended_factor_weight": pd.NA,
            }
        ]
    )
    assert stock_analyzer._is_usable_validation_scorecard(dirty) is False


def test_validation_scorecard_sanitizer_classifies_component_and_cleans_inf():
    _install_stubs()
    if "stock_analyzer" in sys.modules:
        del sys.modules["stock_analyzer"]
    stock_analyzer = importlib.import_module("stock_analyzer")

    dirty = pd.DataFrame(
        [
            {
                "feature_name": "MA5",
                "component": pd.NA,
                "validation_score": float("inf"),
                "recommended_factor_weight": pd.NA,
            }
        ]
    )
    cleaned = stock_analyzer._sanitize_validation_scorecard(dirty)
    assert cleaned.loc[0, "component"] == "trend"
    assert pd.isna(cleaned.loc[0, "validation_score"])


if __name__ == "__main__":
    test_run_cli_supports_all_hk_mode()
    test_run_cli_all_hk_supports_max_workers()
    test_run_cli_all_hk_supports_strategy_mode_override()
    test_run_cli_all_hk_supports_progress_and_fast_mode()
    test_run_cli_select_stocks_supports_lightgbm_mode()
    test_run_cli_all_hk_supports_export_csv()
    test_run_cli_all_hk_supports_persist_signals()
    test_run_cli_supports_review_batch_mode()
    test_run_cli_supports_factor_report_mode()
    test_run_cli_all_hk_supports_recommended_factor_weights_mode()
    test_merge_recommended_factor_weights_supports_feature_name_rows()
    test_build_validation_cache_key_distinguishes_factor_scope()
    print("stock analyzer cli tests passed")
