#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""OHLCV 数据质量巡检测试。"""

import sys
import warnings
from pathlib import Path

import pandas as pd
import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.model import (
    aggregate_quality_reports,
    clean_feature_panel,
    feature_panel_to_long,
    validate_intraday_frame,
    validate_ohlcv_frame,
    validate_pit_frame,
)


def test_validate_ohlcv_frame_happy_path():
    frame = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2026-04-24 09:30:00", "2026-04-24 09:31:00"]),
            "open": [10.0, 10.1],
            "high": [10.2, 10.3],
            "low": [9.9, 10.0],
            "close": [10.1, 10.2],
            "volume": [100, 200],
        }
    )

    report = validate_ohlcv_frame(frame, market="HK", frequency="1min")
    assert report["passed"] is True
    assert report["error_count"] == 0
    assert report["warning_count"] == 0
    assert report["issue_counts"] == {}


def test_validate_ohlcv_frame_detects_errors_and_warnings():
    frame = pd.DataFrame(
        {
            "trade_date": [
                "2026-04-24 09:31:00",
                "2026-04-24 09:30:00",
                "2026-04-24 09:30:00",
                None,
            ],
            "open": [10.0, -1.0, 10.0, 11.0],
            "high": [9.5, 10.0, 10.0, 11.0],
            "low": [10.2, 9.0, 10.0, 11.0],
            "close": [10.1, 9.5, 10.0, 11.0],
            "volume": [100, -5, 0, 0],
        }
    )

    report = validate_ohlcv_frame(frame, market="HK", frequency="1min")
    assert report["passed"] is False
    assert report["error_count"] >= 5
    assert report["warning_count"] >= 1
    assert report["issue_counts"]["missing_required_values"] == 1
    assert report["issue_counts"]["duplicate_trade_date"] == 1
    assert report["issue_counts"]["non_monotonic_trade_date"] == 1
    assert report["issue_counts"]["non_positive_price"] == 1
    assert report["issue_counts"]["negative_volume"] == 1
    assert report["issue_counts"]["invalid_ohlc_relationship"] >= 1
    assert report["issue_counts"]["zero_volume_flat_bar"] == 2
    assert report["issue_counts"]["zero_volume_nonflat_bar"] == 1


def test_validate_intraday_uses_timestamp_not_trade_date_uniqueness():
    frame = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-04-24 09:30:00", "2026-04-24 09:31:00", "2026-04-24 12:00:00"]),
        "open": [10, 10, 10], "high": [10.1, 10.1, 10.1],
        "low": [9.9, 9.9, 9.9], "close": [10, 10, 10], "volume": [1, 1, 1],
    })
    report = validate_intraday_frame(frame, frequency="1min", stock_code="000001")
    assert report["passed"] is True
    assert report["issue_counts"]["ohlcv.session_bounds"] == 1
    assert "duplicate_trade_date" not in report["issue_counts"]


def test_validate_pit_detects_future_and_duplicate_key():
    frame = pd.DataFrame({
        "stock_code": ["000001", "000001"],
        "trade_date": ["2026-04-01", "2026-04-01"],
        "available_at": ["2026-04-01", "2026-04-02"],
        "roe": [0.1, 0.2],
    })
    report = validate_pit_frame(frame, key_columns=["stock_code", "trade_date"], market="CN")
    assert report["passed"] is False
    assert report["issue_counts"]["pit.available_at"] == 1
    assert report["issue_counts"]["pit.duplicate_key"] == 1


def test_clean_feature_panel_preserves_missing_and_pit_flags():
    frame = pd.DataFrame({
        "market": ["CN", "CN"], "stock_code": ["000001", "000002"],
        "trade_date": ["2026-04-01", "2026-04-02"],
        "available_at": ["2026-04-01", "2026-04-03"],
        "ret": [1.0, None],
    })
    cleaned, manifest = clean_feature_panel(frame, feature_columns=["ret"])
    assert manifest["feature_count"] == 1
    assert bool(cleaned.loc[1, "ret_is_missing"])
    assert not bool(cleaned.loc[1, "ret_is_imputed"])
    assert cleaned.loc[1, "quality_status"] == "invalid"
    long = feature_panel_to_long(cleaned, feature_columns=["ret"])
    assert set(["value_raw", "value_clean", "is_missing", "is_imputed", "available_at"]).issubset(long.columns)


def test_aggregate_quality_reports_is_deterministic():
    reports = [
        {"rows": 2, "error_count": 1, "warning_count": 0, "stock_code": "000002", "issue_counts": {"b": 1}},
        {"rows": 3, "error_count": 0, "warning_count": 1, "stock_code": "000001", "issue_counts": {"a": 1}},
    ]
    result = aggregate_quality_reports(reports)
    assert result["rows"] == 5
    assert result["issue_stock_count"] == 2
    assert list(result["issue_counts"]) == ["a", "b"]


def test_clean_feature_panel_reuses_training_scaler_manifest():
    train = pd.DataFrame({"stock_code": ["000001", "000002"], "trade_date": ["2026-01-01", "2026-01-02"], "ret": [1.0, 2.0]})
    test = pd.DataFrame({"stock_code": ["000003"], "trade_date": ["2026-01-03"], "ret": [100.0]})
    _, manifest = clean_feature_panel(train, feature_columns=["ret"])
    transformed, _ = clean_feature_panel(test, feature_columns=["ret"], scaler_manifest=manifest)
    assert np.isclose(transformed.loc[0, "ret_clean"], (manifest["features"][0]["upper"] - manifest["features"][0]["center"]) / manifest["features"][0]["scale"])


def test_clean_feature_panel_builds_wide_columns_without_fragmentation_warning():
    frame = pd.DataFrame({
        "market": ["CN"] * 4,
        "stock_code": ["A"] * 4,
        "trade_date": pd.date_range("2025-01-01", periods=4),
        **{f"factor_{i}": [float(i), None, float(i + 1), float(i + 2)] for i in range(80)},
    })
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", pd.errors.PerformanceWarning)
        cleaned, _ = clean_feature_panel(frame)
    assert not any(isinstance(item.message, pd.errors.PerformanceWarning) for item in captured)
    assert cleaned.shape[1] == frame.shape[1] + 1 + 80 * 5 + 2


if __name__ == "__main__":
    test_validate_ohlcv_frame_happy_path()
    test_validate_ohlcv_frame_detects_errors_and_warnings()
    print("ohlcv quality tests passed")
