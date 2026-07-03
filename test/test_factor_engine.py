#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Qlib 风格因子引擎本地测试。"""

import inspect
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.ingest.service import MarketDataService, _factor_compute_worker
from data.model import (
    FEATURE_COLUMNS,
    normalize_financial_statement_metrics,
    normalize_ohlcv_frame,
    normalize_valuation_snapshot,
)
from data.store.clickhouse_store import ClickHouseStore
from data.store.layout import DataLayout
from data.store.warehouse import MarketDataWarehouse
from factor_engine import create_factor_set, export_factor_manifest, list_factor_sets, show_factor
from factor_engine.ml.lightgbm_ranker import LightGBMRankerPipeline, _load_lightgbm_regressor_class


def _make_ohlcv_frame(rows=90):
    index = pd.date_range("2024-01-02", periods=rows, freq="B")
    base = np.linspace(100.0, 140.0, rows)
    frame = pd.DataFrame(
        {
            "date": index,
            "Open": base * 0.995,
            "High": base * 1.015,
            "Low": base * 0.985,
            "Close": base,
            "Volume": np.linspace(1_000_000, 1_500_000, rows),
        }
    )
    return frame


def test_factor_registry_contains_qlib_sets():
    assert "qlib_alpha158" in list_factor_sets()
    assert "qlib_alpha360" in list_factor_sets()
    assert "gtja_alpha191" in list_factor_sets()
    assert "alpha101" in list_factor_sets()
    assert "academic_hk" in list_factor_sets()
    assert "valuation_hk" in list_factor_sets()
    assert "financial_quality_hk" in list_factor_sets()
    assert "alpha_zoo_hk" in list_factor_sets()


def test_alpha360_shape_and_basics():
    factor_set = create_factor_set("qlib_alpha360")
    normalized = normalize_ohlcv_frame(_make_ohlcv_frame(), stock_code="00700", market="HK")
    feature_frame = factor_set.transform(normalized)

    assert feature_frame.shape[1] == 360
    assert feature_frame.columns[0] == "CLOSE59"
    assert feature_frame.columns[-1] == "VOLUME0"
    latest_row = feature_frame.iloc[-1]
    assert abs(latest_row["CLOSE0"] - 1.0) < 1e-9
    assert abs(latest_row["VOLUME0"] - 1.0) < 1e-9


def test_alpha158_default_feature_count_and_values():
    factor_set = create_factor_set("qlib_alpha158")
    normalized = normalize_ohlcv_frame(_make_ohlcv_frame(), stock_code="00700", market="HK")
    feature_frame = factor_set.transform(normalized)

    assert feature_frame.shape[1] == 193
    latest_row = feature_frame.iloc[-1]
    close = normalized["close"].iloc[-1]
    open_price = normalized["open"].iloc[-1]
    ma5 = normalized["close"].rolling(5, min_periods=5).mean().iloc[-1] / close
    assert abs(latest_row["KMID"] - ((close - open_price) / open_price)) < 1e-9
    assert abs(latest_row["MA5"] - ma5) < 1e-9
    assert pd.notna(latest_row["RSQR60"])


def test_alpha158_hk_includes_gtja_alpha191_features():
    factor_set = create_factor_set("alpha158_hk")
    normalized = normalize_ohlcv_frame(_make_ohlcv_frame(rows=260), stock_code="00700", market="HK")
    feature_frame = factor_set.transform(normalized)
    metadata = factor_set.metadata().to_dict()

    assert feature_frame.shape[1] == metadata["extra"]["feature_count"]
    assert metadata["version"] == "3.0.0"
    assert metadata["extra"]["gtja_feature_count"] == 191
    assert metadata["extra"]["hk_custom_factor_count"] == 9
    assert {"KMID", "buying_pressure", "GTJA001", "GTJA191"}.issubset(feature_frame.columns)
    assert pd.notna(feature_frame.iloc[-1]["GTJA191"])


def test_gtja_alpha191_shape_metadata_and_representative_values():
    factor_set = create_factor_set("gtja_alpha191")
    normalized = normalize_ohlcv_frame(_make_ohlcv_frame(rows=260), stock_code="00700", market="HK")
    feature_frame = factor_set.transform(normalized)
    metadata = factor_set.metadata().to_dict()

    assert feature_frame.shape[1] == 191
    assert feature_frame.columns[0] == "GTJA001"
    assert feature_frame.columns[-1] == "GTJA191"
    assert metadata["extra"]["feature_count"] == 191
    assert metadata["extra"]["proxy_formula_count"] > 0

    latest = feature_frame.iloc[-1]
    close = normalized["close"]
    assert abs(latest["GTJA018"] - (close.iloc[-1] / close.shift(5).iloc[-1])) < 1e-9
    assert pd.notna(latest["GTJA041"])
    assert pd.notna(latest["GTJA191"])


def test_alpha101_academic_and_alpha_zoo_metadata():
    normalized = normalize_ohlcv_frame(_make_ohlcv_frame(rows=280), stock_code="00700", market="HK")

    alpha101 = create_factor_set("alpha101")
    alpha101_frame = alpha101.transform(normalized)
    assert alpha101_frame.shape[1] == 101
    assert {"ALPHA101_001", "ALPHA101_101"}.issubset(alpha101_frame.columns)

    academic = create_factor_set("academic_hk")
    academic_frame = academic.transform(normalized)
    assert academic_frame.shape[1] == 6
    assert "academic_carhart_mom" in academic_frame.columns

    manifest = export_factor_manifest("alpha101")
    assert manifest["factor_sets"][0]["feature_count"] == 101
    shown = show_factor("ALPHA101_001", factor_set="alpha101")
    assert shown["factor"]["factor_id"] == "ALPHA101_001"

    zoo_meta = create_factor_set("alpha_zoo_hk").metadata().to_dict()
    assert zoo_meta["extra"]["feature_count"] > 500
    assert any(item["factor_set"] == "alpha101" for item in zoo_meta["extra"]["components"])
    assert any(item["factor_set"] == "financial_cross_section_hk" for item in zoo_meta["extra"]["components"])
    assert "roe_ind_pct" in zoo_meta["extra"]["feature_names"]


def test_native_factor_zoo_has_no_external_reference_repo_runtime_dependency():
    import factor_engine.expressions.academic as academic
    import factor_engine.expressions.alpha101 as alpha101
    import factor_engine.expressions.alpha_zoo as alpha_zoo
    import factor_engine.expressions.financial_factors as financial_factors

    forbidden_markers = (
        "Vibe-Trading",
        "Vibe_Trading",
        "vibe_trading",
        "vide_trading",
        "HKUDS",
    )
    for module in (academic, alpha101, alpha_zoo, financial_factors):
        source = inspect.getsource(module)
        assert not any(marker in source for marker in forbidden_markers)


def test_financial_snapshot_normalizers_and_store_roundtrip():
    valuation = normalize_valuation_snapshot(
        {
            "trade_date": "2026-01-02",
            "market_cap": 100_000_000,
            "pe_ratio": 12.3,
            "pb_ratio": 1.4,
            "volume": 1_000_000,
            "amount": 5_000_000,
            "circulating_shares": 10_000_000,
        },
        stock_code="700",
        market="HK",
        source="unit_test",
    )
    financial = normalize_financial_statement_metrics(
        {
            "report_date": "2025-12-31",
            "announce_date": "2026-03-20",
            "period_type": "annual",
            "roe": 0.18,
            "roa": 0.08,
            "gross_margin": 0.45,
            "revenue_yoy": 0.12,
        },
        stock_code="700",
        market="HK",
        source="unit_test",
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        warehouse = MarketDataWarehouse(DataLayout(base_dir=tmp_dir))
        valuation_result = warehouse.upsert_valuation_snapshots(pd.DataFrame([valuation]))
        financial_result = warehouse.upsert_financial_statement_metrics(pd.DataFrame([financial]))
        assert valuation_result["rows"] == 1
        assert financial_result["rows"] == 1

        loaded_valuation = warehouse.read_valuation_snapshots(stock_codes=["00700"], market="HK")
        loaded_financial = warehouse.read_financial_statement_metrics(stock_codes=["00700"], market="HK")
        assert not loaded_valuation.empty
        assert not loaded_financial.empty
        assert loaded_valuation.iloc[0]["turnover_rate"] == 10.0
        assert loaded_financial.iloc[0]["roe"] == 0.18


def test_financial_cross_section_factor_uses_context_values():
    from factor_engine.context import FactorContext

    normalized = normalize_ohlcv_frame(_make_ohlcv_frame(rows=90), stock_code="00700", market="HK")
    factor_set = create_factor_set("financial_cross_section_hk")
    context = FactorContext(
        stock_code="00700",
        market="HK",
        extra={
            "pe_ind_pct": 80.0,
            "roe_ind_pct": 90.0,
            "quality_value_score": 85.0,
            "growth_quality_score": 75.0,
        },
    )
    feature_frame = factor_set.transform(normalized, context=context)

    assert feature_frame.shape[1] == 11
    assert feature_frame.iloc[-1]["pe_ind_pct"] == 80.0
    assert feature_frame.iloc[-1]["roe_ind_pct"] == 90.0
    assert feature_frame.iloc[-1]["quality_value_score"] == 85.0


def test_service_factor_context_extra_map_adds_financial_cross_section_scores():
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = MarketDataService(base_dir=tmp_dir)
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
                        "pe_ratio": 20.0,
                        "pb_ratio": 3.0,
                        "ps_ratio": 5.0,
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
                        "pe_ratio": 30.0,
                        "pb_ratio": 5.0,
                        "ps_ratio": 8.0,
                        "dividend_yield": 0.1,
                        "ingest_time": pd.Timestamp("2026-01-02"),
                    },
                ]
            )
            service.warehouse.upsert_stock_info_batch(stock_info.to_dict(orient="records"))
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
            service.warehouse.upsert_financial_statement_metrics(financial)

            context_map = service._build_factor_context_extra_map(
                ["00700", "00005"],
                market="HK",
                asof_date="2026-04-01",
            )
        finally:
            service.close()

    assert context_map["00700"]["roe_ind_pct"] > context_map["00005"]["roe_ind_pct"]
    assert context_map["00700"]["pe_ind_pct"] > context_map["00005"]["pe_ind_pct"]
    assert context_map["00700"]["quality_value_score"] > context_map["00005"]["quality_value_score"]


def test_factor_compute_worker_uses_trade_date_payload_with_range_index():
    normalized = normalize_ohlcv_frame(_make_ohlcv_frame(), stock_code="00700", market="HK")
    range_index_frame = normalized.reset_index(drop=True)
    payload = {
        "stock_code": "00700",
        "ohlcv_data": range_index_frame.to_dict("list"),
        "ohlcv_columns": list(range_index_frame.columns),
        "ohlcv_trade_dates": list(range_index_frame["trade_date"].dt.strftime("%Y-%m-%d")),
        "factor_set": "qlib_alpha158",
        "config": None,
        "market": "HK",
        "frequency": "daily",
        "adjust": "qfq",
        "exchange": None,
        "asset_type": "equity",
    }

    result = _factor_compute_worker(payload)

    assert result["stock_code"] == "00700"
    assert "feature_data" in result
    assert len(result["feature_index"]) == len(range_index_frame)
    assert result["feature_index"][0].startswith("2024-")
    assert "KMID" in result["feature_columns"]


def test_factor_compute_worker_supports_gtja_alpha191():
    normalized = normalize_ohlcv_frame(_make_ohlcv_frame(rows=260), stock_code="00700", market="HK")
    payload = {
        "stock_code": "00700",
        "ohlcv_data": normalized.to_dict("list"),
        "ohlcv_columns": list(normalized.columns),
        "ohlcv_trade_dates": list(normalized["trade_date"].dt.strftime("%Y-%m-%d")),
        "factor_set": "gtja_alpha191",
        "config": None,
        "market": "HK",
        "frequency": "daily",
        "adjust": "qfq",
        "exchange": None,
        "asset_type": "equity",
    }

    result = _factor_compute_worker(payload)

    assert result["stock_code"] == "00700"
    assert len(result["feature_columns"]) == 191
    assert "GTJA001" in result["feature_columns"]
    assert "GTJA191" in result["feature_columns"]



def test_service_can_compute_and_persist_factor_set():
    raw_frame = _make_ohlcv_frame()
    normalized = normalize_ohlcv_frame(raw_frame, stock_code="00700", market="HK", source="unit_test")

    with tempfile.TemporaryDirectory() as tmp_dir:
        service = MarketDataService(base_dir=tmp_dir)
        try:
            service.warehouse.upsert_ohlcv(normalized)
            result = service.sync_factor_set(
                stock_code="00700",
                market="HK",
                factor_set="qlib_alpha158",
                source="unit_test",
            )
            assert result["rows"] == len(normalized)
            assert result["write_result"] is not None
            assert result["metadata"]["extra"]["feature_count"] == 193

            loaded = service.get_feature_frame(
                stock_code="00700",
                market="HK",
                feature_set="qlib_alpha158",
            )
            assert not loaded.empty
            assert {"KMID", "MA5", "RSQR60"}.issubset(set(loaded["feature_name"].unique()))
        finally:
            service.close()


def test_service_can_persist_multiple_factor_materializations_for_same_factor_set():
    raw_frame = _make_ohlcv_frame()
    normalized = normalize_ohlcv_frame(raw_frame, stock_code="00700", market="HK", source="unit_test")

    with tempfile.TemporaryDirectory() as tmp_dir:
        service = MarketDataService(base_dir=tmp_dir)
        try:
            service.warehouse.upsert_ohlcv(normalized)

            no_ta = service.sync_factor_set(
                stock_code="00700",
                market="HK",
                factor_set="qlib_alpha158",
                source="unit_test",
                config={"ta": {"indicators": []}},
            )
            rsi_only = service.sync_factor_set(
                stock_code="00700",
                market="HK",
                factor_set="qlib_alpha158",
                source="unit_test",
                config={"ta": {"indicators": ["TA_RSI"]}},
            )

            no_ta_meta = no_ta["metadata"]
            rsi_only_meta = rsi_only["metadata"]

            assert no_ta_meta["version"] == rsi_only_meta["version"]
            assert no_ta_meta["extra"]["feature_config_hash"] != rsi_only_meta["extra"]["feature_config_hash"]

            loaded_no_ta = service.get_feature_frame(
                stock_code="00700",
                market="HK",
                feature_set="qlib_alpha158",
                feature_version=no_ta_meta["version"],
                feature_config_hash=no_ta_meta["extra"]["feature_config_hash"],
            )
            loaded_rsi_only = service.get_feature_frame(
                stock_code="00700",
                market="HK",
                feature_set="qlib_alpha158",
                feature_version=rsi_only_meta["version"],
                feature_config_hash=rsi_only_meta["extra"]["feature_config_hash"],
            )

            assert not loaded_no_ta.empty
            assert not loaded_rsi_only.empty
            assert set(loaded_no_ta["feature_config_hash"]) == {no_ta_meta["extra"]["feature_config_hash"]}
            assert set(loaded_rsi_only["feature_config_hash"]) == {rsi_only_meta["extra"]["feature_config_hash"]}
        finally:
            service.close()


def test_factor_generation_resource_plan_scales_down_on_low_memory():
    original_available_memory_bytes = MarketDataService._available_memory_bytes

    try:
        MarketDataService._available_memory_bytes = staticmethod(lambda: 1 * 1024 ** 3)
        low_memory_plan = MarketDataService._resolve_factor_generation_resource_plan(
            requested_workers=8,
            total_stocks=100,
            expected_feature_count=193,
            days=365,
        )

        MarketDataService._available_memory_bytes = staticmethod(lambda: 32 * 1024 ** 3)
        high_memory_plan = MarketDataService._resolve_factor_generation_resource_plan(
            requested_workers=8,
            total_stocks=100,
            expected_feature_count=193,
            days=365,
        )
    finally:
        MarketDataService._available_memory_bytes = original_available_memory_bytes

    assert 1 <= low_memory_plan["max_workers"] < high_memory_plan["max_workers"] <= 8
    assert low_memory_plan["batch_flush_feature_rows"] < high_memory_plan["batch_flush_feature_rows"]
    assert low_memory_plan["max_pending_futures"] <= high_memory_plan["max_pending_futures"]


def test_warehouse_disables_clickhouse_after_feature_read_failure():
    class FailingClickHouseStore:
        def read_frame(self, *args, **kwargs):
            raise RuntimeError("Unexpected Http Driver Exception")

    class FallbackParquetStore:
        def __init__(self):
            self.calls = 0

        def read_frame(self, *args, **kwargs):
            self.calls += 1
            return pd.DataFrame()

    with tempfile.TemporaryDirectory() as tmp_dir:
        fallback = FallbackParquetStore()
        warehouse = MarketDataWarehouse(
            DataLayout(base_dir=tmp_dir),
            clickhouse_store=FailingClickHouseStore(),
        )
        warehouse.parquet_store = fallback

        frame = warehouse.read_features(
            stock_code="00700",
            market="HK",
            feature_set="qlib_alpha158",
            feature_version="0.1.0",
            feature_config_hash="unit",
        )

        assert frame.empty
        assert "Unexpected Http Driver Exception" in warehouse._clickhouse_disabled_reason
        assert fallback.calls == 1


def test_warehouse_skips_clickhouse_when_configured_endpoint_is_unreachable():
    with patch.dict("os.environ", {"CLICKHOUSE_HOST": "localhost", "CLICKHOUSE_PORT": "8123"}):
        with patch("socket.create_connection", side_effect=OSError("connection refused")):
            with tempfile.TemporaryDirectory() as tmp_dir:
                warehouse = MarketDataWarehouse(DataLayout(base_dir=tmp_dir))

    assert warehouse.clickhouse_store is None
    assert "connection refused" in warehouse._clickhouse_disabled_reason


def test_warehouse_uses_clickhouse_http_port_fallback():
    with patch.dict(
        "os.environ",
        {
            "CLICKHOUSE_HOST": "localhost",
            "CLICKHOUSE_HTTP_PORT": "18123",
            "CLICKHOUSE_PORT": "",
        },
    ):
        with patch("socket.create_connection") as create_connection:
            with tempfile.TemporaryDirectory() as tmp_dir:
                warehouse = MarketDataWarehouse(DataLayout(base_dir=tmp_dir))

    create_connection.assert_called_once()
    assert create_connection.call_args.args[0] == ("localhost", 18123)
    assert warehouse.clickhouse_store.port == 18123


def test_clickhouse_insert_frame_chunks_large_stock_info_batches():
    class RecordingClient:
        def __init__(self):
            self.chunk_sizes = []

        def insert_df(self, table, frame):
            self.chunk_sizes.append(len(frame))

    frame = pd.DataFrame(
        [
            {
                "stock_code": f"{code:05d}",
                "market": "HK",
                "exchange": "HKEX",
                "asset_type": "equity",
                "industry_l1": "资讯科技业",
                "industry_l2": "软件服务",
                "industry_source": "manual_csv",
                "is_fund_like": False,
                "tradable_flag": True,
                "ingest_time": pd.Timestamp("2026-06-03"),
            }
            for code in range(1, 4)
        ]
    )
    client = RecordingClient()

    with patch.dict("os.environ", {"CLICKHOUSE_INSERT_CHUNK_ROWS": "2"}):
        ClickHouseStore()._insert_frame(
            client,
            "stock_info_registry_meta",
            "stock_info_registry",
            frame,
            date_column="ingest_time",
        )

    assert client.chunk_sizes == [2, 1]


def test_clickhouse_ensure_stock_info_table_adds_financial_columns_for_existing_tables():
    class RecordingClient:
        def __init__(self):
            self.commands = []

        def command(self, sql):
            self.commands.append(sql)

    client = RecordingClient()

    table = ClickHouseStore()._ensure_table(client, "stock_info_registry", "meta")

    assert table == "stock_info_registry_meta"
    assert any("ADD COLUMN IF NOT EXISTS amount Nullable(Float64)" in sql for sql in client.commands)
    assert any("ADD COLUMN IF NOT EXISTS daily_turnover Nullable(Float64)" in sql for sql in client.commands)
    assert any("ADD COLUMN IF NOT EXISTS turnover_rate Nullable(Float64)" in sql for sql in client.commands)


def test_warehouse_append_features_uses_append_only_store_path():
    class AppendOnlyStore:
        def __init__(self):
            self.append_calls = 0

        def append_frame(self, *args, **kwargs):
            self.append_calls += 1
            return "/tmp/features"

        def upsert_frame(self, *args, **kwargs):
            raise AssertionError("append feature batches must not upsert the full dataset")

    frame = pd.DataFrame(
        [
            {
                "trade_date": pd.Timestamp("2026-01-01"),
                "stock_code": "00700",
                "market": "HK",
                "exchange": None,
                "asset_type": "equity",
                "frequency": "daily",
                "adjust": "qfq",
                "feature_set": "qlib_alpha158",
                "feature_version": "0.1.0",
                "feature_config_hash": "unit",
                "feature_name": "KMID",
                "feature_value": 1.0,
                "source": "unit_test",
                "ingest_time": pd.Timestamp("2026-01-01"),
            }
        ],
        columns=FEATURE_COLUMNS,
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        store = AppendOnlyStore()
        warehouse = MarketDataWarehouse(DataLayout(base_dir=tmp_dir))
        warehouse.parquet_store = store

        result = warehouse.append_features(frame)

    assert result["rows"] == 1
    assert store.append_calls == 1


def test_parquet_rps_feature_count_returns_written_rows():
    trade_date = pd.Timestamp("2026-01-01")
    frame = pd.DataFrame(
        [
            {
                "trade_date": trade_date,
                "stock_code": code,
                "market": "HK",
                "exchange": "HKEX",
                "asset_type": "equity",
                "frequency": "daily",
                "adjust": "qfq",
                "feature_set": "unit_alpha",
                "feature_version": "0.1.0",
                "feature_config_hash": "unit",
                "feature_name": "ROC5",
                "feature_value": value,
                "source": "unit_test",
                "ingest_time": trade_date,
            }
            for code, value in [("00001", 0.9), ("00002", 1.1)]
        ],
        columns=FEATURE_COLUMNS,
    )

    with patch.dict("os.environ", {"CLICKHOUSE_HOST": ""}):
        with tempfile.TemporaryDirectory() as tmp_dir:
            warehouse = MarketDataWarehouse(DataLayout(base_dir=tmp_dir))
            warehouse.append_features(frame)

            rows_written = warehouse.compute_rps_features(
                factor_set="unit_alpha",
                windows=(5,),
            )
            rps = warehouse.read_features(
                market="HK",
                feature_set="unit_alpha",
                feature_name="RPS_5",
            )

    assert rows_written == 2
    assert len(rps) == 2
    assert set(rps["feature_name"]) == {"RPS_5"}


def test_parquet_rps_uses_append_only_and_skips_existing_rows():
    trade_date = pd.Timestamp("2026-01-01")
    frame = pd.DataFrame(
        [
            {
                "trade_date": trade_date,
                "stock_code": code,
                "market": "HK",
                "exchange": "HKEX",
                "asset_type": "equity",
                "frequency": "daily",
                "adjust": "qfq",
                "feature_set": "unit_alpha",
                "feature_version": "0.1.0",
                "feature_config_hash": "unit",
                "feature_name": "ROC5",
                "feature_value": value,
                "source": "unit_test",
                "ingest_time": trade_date,
            }
            for code, value in [("00001", 0.9), ("00002", 1.1)]
        ],
        columns=FEATURE_COLUMNS,
    )

    with patch.dict("os.environ", {"CLICKHOUSE_HOST": ""}):
        with tempfile.TemporaryDirectory() as tmp_dir:
            warehouse = MarketDataWarehouse(DataLayout(base_dir=tmp_dir))
            warehouse.append_features(frame)

            with patch.object(
                warehouse.parquet_store,
                "upsert_frame",
                side_effect=AssertionError("RPS should not rewrite the full feature dataset"),
            ):
                rows_written = warehouse.compute_rps_features(
                    factor_set="unit_alpha",
                    windows=(5,),
                )
                rows_written_again = warehouse.compute_rps_features(
                    factor_set="unit_alpha",
                    windows=(5,),
                )

            rps = warehouse.read_features(
                market="HK",
                feature_set="unit_alpha",
                feature_name="RPS_5",
            )

    assert rows_written == 2
    assert rows_written_again == 0
    assert len(rps) == 2


def test_generate_factor_set_reuses_batch_coverage_check_instead_of_per_stock_feature_reads():
    with patch.dict("os.environ", {"CLICKHOUSE_HOST": ""}):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = MarketDataService(base_dir=tmp_dir)
            try:
                for code in ["00700", "00005"]:
                    raw_frame = _make_ohlcv_frame(rows=40)
                    raw_frame["date"] = pd.date_range(
                        pd.Timestamp.utcnow().tz_localize(None).normalize() - pd.Timedelta(days=39),
                        periods=40,
                        freq="D",
                    )
                    normalized = normalize_ohlcv_frame(
                        raw_frame,
                        stock_code=code,
                        market="HK",
                        source="unit_test",
                    )
                    service.warehouse.upsert_ohlcv(normalized)

                read_feature_calls = []

                def fake_read_features(**kwargs):
                    read_feature_calls.append(kwargs.get("stock_code"))
                    return pd.DataFrame()

                service.warehouse.read_features = fake_read_features
                append_feature_calls = []

                def fake_append_features(frame):
                    append_feature_calls.append(len(frame))
                    return {"rows": len(frame), "dataset_path": "/tmp/features"}

                def fake_upsert_features(frame):
                    raise AssertionError("batch factor generation should append feature batches")

                service.warehouse.append_features = fake_append_features
                service.warehouse.upsert_features = fake_upsert_features

                result = service.generate_factor_set(
                    stock_codes=["00700", "00005"],
                    factor_set="qlib_alpha158",
                    days=5,
                    warmup_days=5,
                    max_workers=2,
                    show_progress=False,
                )
            finally:
                service.close()

        assert result["success_count"] == 2
        assert read_feature_calls == [["00700", "00005"]]
        assert append_feature_calls
        assert result["rows_written"] == sum(append_feature_calls)


def test_generate_gtja_alpha191_skips_rps_postprocess_without_roc_sources():
    gtja_metadata = create_factor_set("gtja_alpha191").metadata().to_dict()
    alpha158_metadata = create_factor_set("qlib_alpha158").metadata().to_dict()
    alpha158_hk_metadata = create_factor_set("alpha158_hk").metadata().to_dict()

    assert not MarketDataService._should_compute_rps_for_factor_set(
        "gtja_alpha191",
        gtja_metadata,
        computed_count=1,
    )
    assert MarketDataService._should_compute_rps_for_factor_set(
        "qlib_alpha158",
        alpha158_metadata,
        computed_count=1,
    )
    assert MarketDataService._should_compute_rps_for_factor_set(
        "alpha158_hk",
        alpha158_hk_metadata,
        computed_count=1,
    )
    assert MarketDataService._should_compute_rps_for_factor_set(
        "qlib_alpha158",
        alpha158_metadata,
        computed_count=0,
    )


def test_lightgbm_ranker_loader_reports_missing_libomp_on_macos():
    original_import = __import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "lightgbm":
            raise OSError("dlopen(...): Library not loaded: @rpath/libomp.dylib")
        return original_import(name, globals, locals, fromlist, level)

    with patch("platform.system", return_value="Darwin"):
        with patch("builtins.__import__", side_effect=fake_import):
            try:
                _load_lightgbm_regressor_class()
                raised = None
            except ImportError as exc:
                raised = exc

    assert raised is not None
    assert "brew install libomp" in str(raised)


def test_lightgbm_ranker_cs_rank_norm_label():
    """Test that CSRankNorm produces correct cross-sectional normalized labels."""
    from factor_engine.ml.lightgbm_ranker import _cs_rank_norm

    # 5 stocks with different returns
    series = pd.Series([0.01, 0.05, 0.10, 0.15, 0.20])
    result = _cs_rank_norm(series)

    # Should be approximately centered around 0
    assert result.notna().all()
    # Rank order preserved
    assert result.iloc[0] < result.iloc[1] < result.iloc[2] < result.iloc[3] < result.iloc[4]
    # Values should be in reasonable range (approx -1.7 to +1.7 for 5 elements)
    assert result.min() > -2.0
    assert result.max() < 2.0


def test_lightgbm_ranker_cs_rank_norm_handles_nan():
    """Test CSRankNorm handles NaN gracefully."""
    from factor_engine.ml.lightgbm_ranker import _cs_rank_norm

    series = pd.Series([0.01, np.nan, 0.10])
    result = _cs_rank_norm(series)
    # NaN input stays NaN
    assert pd.isna(result.iloc[1])
    # Non-NaN values get normalized
    assert result.iloc[0] < result.iloc[2]


def test_lightgbm_ranker_excludes_target_columns_from_features():
    pipeline = LightGBMRankerPipeline()
    merged = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2025-01-02"]),
            "stock_code": ["00001"],
            "MA5": [1.01],
            "STD20": [0.12],
            "target_return": [0.08],
            "target_drawdown": [-0.03],
            "target_max_return": [0.11],
            "target_drawdown_penalty": [0.03],
            "target_breakout_bonus": [0.0],
            "target_score": [0.05],
            "label": [4],
        }
    )

    feature_columns = pipeline._resolve_feature_columns(merged)

    assert feature_columns == ["MA5", "STD20"]


def test_lightgbm_ranker_prepare_dedupes_trade_date_before_neutralization():
    pipeline = LightGBMRankerPipeline(neutralization_mode="industry_size")
    rows = []
    target_rows = []
    trade_date = pd.Timestamp("2026-01-02")
    for idx in range(10):
        stock_code = f"{idx + 1:05d}"
        rows.append([
            trade_date,
            trade_date,
            stock_code,
            float(idx),
            "Tech" if idx % 2 else "Finance",
            1_000_000.0 + idx * 10_000.0,
        ])
        target_rows.append(
            {
                "trade_date": trade_date,
                "stock_code": stock_code,
                "forward_return_20": idx / 100.0,
            }
        )

    panel_features = pd.DataFrame(rows)
    panel_features.columns = [
        "trade_date", "trade_date", "stock_code",
        "factor_a", "industry_l1", "market_cap",
    ]
    panel_targets = pd.DataFrame(target_rows)

    merged = pipeline._prepare_merged_frame(panel_features, panel_targets)
    assert not merged.columns.has_duplicates
    assert list(merged.columns).count("trade_date") == 1

    preprocessed = pipeline._preprocess_features_by_date(merged, ["factor_a"])
    assert len(preprocessed) == len(merged)
    assert list(preprocessed.columns).count("trade_date") == 1

    neutralized = pipeline._neutralize_industry_size_features(
        preprocessed,
        ["factor_a"],
        target_col="forward_return_20",
        use_size=True,
        neutralize_target=True,
    )

    assert len(neutralized) == len(preprocessed)
    assert list(neutralized.columns).count("trade_date") == 1


if __name__ == "__main__":
    test_factor_registry_contains_qlib_sets()
    test_alpha360_shape_and_basics()
    test_alpha158_default_feature_count_and_values()
    test_service_can_compute_and_persist_factor_set()
    test_lightgbm_ranker_loader_reports_missing_libomp_on_macos()
    test_lightgbm_ranker_builds_risk_adjusted_target()
    test_lightgbm_ranker_target_prefers_monthly_breakout_shape()
    test_lightgbm_ranker_excludes_target_columns_from_features()
    print("factor engine tests passed")
