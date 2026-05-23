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
from data.model import normalize_ohlcv_frame
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

    assert feature_frame.shape[1] == 158
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
            assert result["metadata"]["extra"]["feature_count"] == 158

            loaded = service.get_feature_frame(
                stock_code="00700",
                market="HK",
                feature_set="qlib_alpha158",
            )
            assert not loaded.empty
            assert {"KMID", "MA5", "RSQR60"}.issubset(set(loaded["feature_name"].unique()))
        finally:
            service.close()


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
