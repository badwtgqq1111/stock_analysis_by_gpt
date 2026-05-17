#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""港股批量同步 smoke test，不依赖外网。"""

import io
import sys
import tempfile
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.ingest.service import MarketDataService


def _fake_daily_frame(start_date, rows):
    trade_dates = pd.date_range(start=start_date, periods=rows, freq="B")
    base = pd.Series(range(rows), dtype="float64")
    return pd.DataFrame(
        {
            "date": trade_dates,
            "open": 300 + base,
            "high": 301 + base,
            "low": 299 + base,
            "close": 300.5 + base,
            "volume": 1000000 + (base * 1000),
        }
    )


def test_hk_bulk_sync_smoke():
    stocks = [
        {"code": "00700", "name": "Tencent"},
        {"code": "00005", "name": "HSBC"},
        {"code": "09988", "name": "Alibaba"},
    ]

    frames = {
        ("00700", "daily"): _fake_daily_frame("2014-01-02", 3),
        ("00700", "1min"): _fake_daily_frame("2024-01-02", 2),
        ("00700", "5min"): _fake_daily_frame("2024-01-02", 2),
        ("00700", "60min"): _fake_daily_frame("2024-01-02", 2),
        ("00005", "daily"): _fake_daily_frame("2014-01-02", 4),
        ("00005", "1min"): _fake_daily_frame("2024-01-02", 1),
        ("00005", "5min"): _fake_daily_frame("2024-01-02", 1),
        ("00005", "60min"): _fake_daily_frame("2024-01-02", 1),
        ("09988", "daily"): _fake_daily_frame("2019-11-26", 2),
        ("09988", "1min"): _fake_daily_frame("2024-01-02", 3),
        ("09988", "5min"): _fake_daily_frame("2024-01-02", 3),
        ("09988", "60min"): _fake_daily_frame("2024-01-02", 3),
    }

    def fake_fetch_list(self, limit=None):
        return list(stocks)

    def fake_fetch(self, start_date=None, end_date=None, period="daily", adjust="qfq"):
        self.last_successful_source = "unit_test"
        return frames[(self.stock_code, period)].copy()

    with tempfile.TemporaryDirectory() as tmp_dir:
        service = MarketDataService(base_dir=tmp_dir)
        try:
            with patch("data.ingest.service.HKMarketListFetcher.fetch", new=fake_fetch_list):
                with patch("data.ingest.service.HistoryDataFetcher.fetch", new=fake_fetch):
                    summary = service.bulk_sync_hk_history(
                        start_date="2014-01-01",
                        limit=None,
                        max_workers=4,
                        flush_stock_count=2,
                        flush_row_count=5,
                        compact_after=True,
                        frequencies=("daily", "1min", "5min", "60min"),
                        intraday_start_date="2024-01-01",
                    )

            assert summary["status"] == "completed"
            assert summary["adjust"] == "qfq"
            assert summary["adjust_profile"]["adjust"] == "qfq"
            assert summary["total_stocks"] == 3
            assert summary["processed_stocks"] == 3
            assert summary["success_count"] == 3
            assert summary["failed_count"] == 0
            assert summary["partial_count"] == 0
            assert summary["rows_written"] == 27
            assert summary["raw_snapshots_written"] == 12
            assert summary["quality_issue_stocks"] == 0
            assert summary["quality_issue_count"] == 0
            assert summary["rows_by_frequency"] == {"daily": 9, "1min": 6, "5min": 6, "60min": 6}
            assert summary["quality_by_frequency"] == {
                "daily": {"error_stocks": 0, "warning_stocks": 0, "error_issues": 0, "warning_issues": 0},
                "1min": {"error_stocks": 0, "warning_stocks": 0, "error_issues": 0, "warning_issues": 0},
                "5min": {"error_stocks": 0, "warning_stocks": 0, "error_issues": 0, "warning_issues": 0},
                "60min": {"error_stocks": 0, "warning_stocks": 0, "error_issues": 0, "warning_issues": 0},
            }
            raw_dataset = Path(summary["raw_dataset_path"])
            assert raw_dataset.exists()
            assert len(list(raw_dataset.rglob("*.parquet"))) == 12

            loaded = service.warehouse.read_ohlcv(market="HK", frequency="daily", adjust="qfq")
            assert len(loaded) == 9
            assert set(loaded["stock_code"].unique()) == {"00700", "00005", "09988"}
            loaded_1min = service.warehouse.read_ohlcv(market="HK", frequency="1min", adjust="qfq")
            loaded_5min = service.warehouse.read_ohlcv(market="HK", frequency="5min", adjust="qfq")
            loaded_60min = service.warehouse.read_ohlcv(market="HK", frequency="60min", adjust="qfq")
            assert len(loaded_1min) == 6
            assert len(loaded_5min) == 6
            assert len(loaded_60min) == 6

            stock_info = service.get_hk_stock_info("00700")
            assert stock_info is not None
            assert stock_info["name"] == "Tencent"
        finally:
            service.close()


def test_hk_bulk_sync_show_progress_reports_frequency_counters():
    stocks = [
        {"code": "00700", "name": "Tencent"},
        {"code": "00005", "name": "HSBC"},
    ]

    frames = {
        ("00700", "daily"): _fake_daily_frame("2014-01-02", 3),
        ("00700", "1min"): _fake_daily_frame("2024-01-02", 2),
        ("00005", "daily"): _fake_daily_frame("2014-01-02", 4),
        ("00005", "1min"): _fake_daily_frame("2024-01-02", 1),
    }

    def fake_fetch_list(self, limit=None):
        return list(stocks)

    def fake_fetch(self, start_date=None, end_date=None, period="daily", adjust="qfq"):
        self.last_successful_source = "unit_test"
        return frames[(self.stock_code, period)].copy()

    with tempfile.TemporaryDirectory() as tmp_dir:
        service = MarketDataService(base_dir=tmp_dir)
        stderr_buffer = io.StringIO()
        try:
            with patch("data.ingest.service.HKMarketListFetcher.fetch", new=fake_fetch_list):
                with patch("data.ingest.service.HistoryDataFetcher.fetch", new=fake_fetch):
                    with redirect_stderr(stderr_buffer):
                        summary = service.bulk_sync_hk_history(
                            start_date="2014-01-01",
                            max_workers=2,
                            flush_stock_count=10,
                            flush_row_count=1000,
                            compact_after=False,
                            frequencies=("daily", "1min"),
                            intraday_start_date="2024-01-01",
                            show_progress=True,
                        )

            assert summary["status"] == "completed"
            output = stderr_buffer.getvalue()
            assert "phase=ohlcv" in output
            assert "stocks_done=" in output
            assert "tasks_done=" in output
            assert "daily=" in output
            assert "1min=" in output
        finally:
            service.close()


if __name__ == "__main__":
    test_hk_bulk_sync_smoke()
    print("hk bulk sync smoke test passed")
