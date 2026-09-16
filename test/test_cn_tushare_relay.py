#!/usr/bin/env python3
"""CN Tushare relay normalization and persistence tests."""

import sys
import tempfile
from pathlib import Path

import pandas as pd
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.ingest.providers.cn_tushare_relay import CNAdjustmentFactorRelayFetcher, CNDailyBasicRelayFetcher, CNTushareRelayClient
from data.ingest.service import MarketDataService


class _Response:
    status_code = 200

    def json(self):
        return {"code": 0, "data": {"fields": [
            "ts_code", "trade_date", "turnover_rate", "turnover_rate_f", "volume_ratio", "pe", "pe_ttm", "pb", "ps", "ps_ttm",
            "dv_ratio", "dv_ttm", "total_share", "float_share", "free_share", "total_mv", "circ_mv",
        ], "items": [[
            "000001.SZ", "20260831", 0.4683, 1.1137, 0.88, 5.3348, 5.2334, 0.4858, 1.7303, 1.7143,
            5.1025, 5.0854, 1940591.8198, 1940568.4991, 816056.5553, 22743736.1304, 22743462.82,
        ]]}}


def test_daily_basic_relay_normalizes_tushare_wan_units():
    def fake_get(url, params, headers, timeout):
        assert url.endswith("daily_basic")
        assert params["ts_code"] == "000001.SZ"
        return _Response()

    frame = CNDailyBasicRelayFetcher(
        "000001.SZ",
        client=__import__("data.ingest.providers.cn_tushare_relay", fromlist=["CNTushareRelayClient"]).CNTushareRelayClient(
            base_api_key="test-key", request_get=fake_get
        ),
    ).fetch(start_date="2026-08-01", end_date="2026-08-31")

    row = frame.iloc[0]
    assert row["market_cap"] == pytest.approx(227437361304.0)
    assert row["pe_ratio"] == 5.2334
    assert row["free_float_shares"] == 8160565553.0
    assert row["free_turnover_rate"] == 1.1137
    assert row["volume_ratio"] == 0.88
    assert row["source"] == "tushare_relay_daily_basic"


def test_adjustment_factor_relay_normalizes_daily_series():
    class Response:
        status_code = 200

        def json(self):
            return {"code": 0, "data": {"fields": ["ts_code", "trade_date", "adj_factor"], "items": [["000001.SZ", "20260831", 139.008]]}}

    frame = CNAdjustmentFactorRelayFetcher(
        "000001.SZ", client=CNTushareRelayClient(base_api_key="test-key", request_get=lambda *args, **kwargs: Response())
    ).fetch(start_date="2026-08-01", end_date="2026-08-31")
    assert frame.to_dict("records") == [{"stock_code": "000001.SZ", "trade_date": pd.Timestamp("2026-08-31"), "adj_factor": 139.008}]


def test_service_persists_relay_daily_basic(monkeypatch):
    from data.ingest import service as service_module

    for key in ("CLICKHOUSE_HOST", "CLICKHOUSE_PORT", "CLICKHOUSE_HTTP_PORT"):
        monkeypatch.delenv(key, raising=False)

    class FakeFetcher:
        def __init__(self, stock_code):
            self.stock_code = stock_code

        def fetch(self, **kwargs):
            return pd.DataFrame([{
                "trade_date": pd.Timestamp("2026-08-31"), "stock_code": self.stock_code, "market": "CN",
                "exchange": "SZSE", "asset_type": "equity", "currency": "CNY", "market_cap": 1.0,
                "circulating_market_cap": 1.0, "free_float_market_cap": 1.0, "pe_ratio": 1.0,
                "pb_ratio": 1.0, "ps_ratio": 1.0, "ev": None, "ev_ebitda": None, "dividend_yield": 1.0,
                "fcf_yield": None, "volume": None, "amount": None, "daily_turnover": None, "turnover_rate": 1.0,
                "free_turnover_rate": 1.1, "volume_ratio": 0.9,
                "total_shares": 1.0, "circulating_shares": 1.0, "free_float_shares": 1.0,
                "source": "tushare_relay_daily_basic", "ingest_time": "2026-09-01T00:00:00",
            }])

    monkeypatch.setattr(service_module, "CNDailyBasicRelayFetcher", FakeFetcher)
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = MarketDataService(base_dir=tmp_dir)
        try:
            result = service.refresh_cn_tushare_daily_basic(stock_codes=["000001.SZ"], max_workers=1)
            loaded = service.warehouse.read_valuation_snapshots(stock_codes=["000001.SZ"], market="CN")
        finally:
            service.close()
    assert result["rows_written"] == 1
    assert loaded.iloc[0]["source"] == "tushare_relay_daily_basic"


def test_daily_basic_write_preserves_existing_non_null_snapshot_values(monkeypatch):
    for key in ("CLICKHOUSE_HOST", "CLICKHOUSE_PORT", "CLICKHOUSE_HTTP_PORT"):
        monkeypatch.delenv(key, raising=False)
    from data.model import normalize_valuation_snapshot

    with tempfile.TemporaryDirectory() as tmp_dir:
        service = MarketDataService(base_dir=tmp_dir)
        try:
            existing = normalize_valuation_snapshot(
                {"trade_date": "2026-08-31", "amount": 123.0, "volume": 456.0, "pe_ratio": 2.0},
                stock_code="000001.SZ", market="CN", source="existing",
            )
            incoming = normalize_valuation_snapshot(
                {"trade_date": "2026-08-31", "pe_ratio": 3.0},
                stock_code="000001.SZ", market="CN", source="relay",
            )
            service.warehouse.upsert_valuation_snapshots(pd.DataFrame([existing]))
            service.warehouse.upsert_valuation_snapshots(
                pd.DataFrame([incoming]), preserve_existing_values=True,
            )
            loaded = service.warehouse.read_valuation_snapshots(stock_codes=["000001.SZ"], market="CN")
        finally:
            service.close()
    row = loaded.iloc[0]
    assert row["pe_ratio"] == 3.0
    assert row["amount"] == 123.0
    assert row["volume"] == 456.0
