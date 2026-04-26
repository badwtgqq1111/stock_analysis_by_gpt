#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Qlib 风格因子引擎本地测试。"""

import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.ingest.service import MarketDataService
from data.model import normalize_ohlcv_frame
from factor_engine import create_factor_set, list_factor_sets


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


if __name__ == "__main__":
    test_factor_registry_contains_qlib_sets()
    test_alpha360_shape_and_basics()
    test_alpha158_default_feature_count_and_values()
    test_service_can_compute_and_persist_factor_set()
    print("factor engine tests passed")
