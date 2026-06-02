#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Qlib 风格因子引擎本地测试。"""

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.ingest.service import MarketDataService
from data.model import FEATURE_COLUMNS, normalize_ohlcv_frame
from data.store.layout import DataLayout
from data.store.warehouse import MarketDataWarehouse
from factor_engine import create_factor_set, list_factor_sets
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
