#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""OHLCV 数据质量巡检测试。"""

import sys
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.model import validate_ohlcv_frame


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


if __name__ == "__main__":
    test_validate_ohlcv_frame_happy_path()
    test_validate_ohlcv_frame_detects_errors_and_warnings()
    print("ohlcv quality tests passed")
