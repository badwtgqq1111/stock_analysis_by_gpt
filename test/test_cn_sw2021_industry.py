#!/usr/bin/env python3
"""申万 2021 行业源与覆盖率门禁测试。"""

import sys
import tempfile
from pathlib import Path

import pandas as pd
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.ingest.providers.cn_sw2021 import SW2021IndustryFetcher
from data.ingest.service import MarketDataService
from data.store.warehouse import MarketDataWarehouse


class _Response:
    status_code = 200
    text = "<table><tr><th>序号</th><th>股票代码</th><th>股票简称</th><th>纳入时间</th><th>申万1级</th></tr><tr><td>1</td><td>600000.SH</td><td>浦发银行</td><td>2021-12-01</td><td>银行</td></tr></table>"

    def raise_for_status(self):
        return None


def test_sw2021_fetcher_parses_stable_composition_columns(monkeypatch):
    fetcher = SW2021IndustryFetcher(sleep_seconds=0, request_get=lambda *args, **kwargs: _Response())
    monkeypatch.setattr(
        fetcher, "fetch_first_level_catalog",
        lambda: pd.DataFrame({"行业代码": ["801780.SI"], "行业名称": ["银行"]}),
    )

    frame = fetcher.fetch()

    assert frame.to_dict("records") == [{
        "stock_code": "600000.SH", "name": "浦发银行", "industry_l1": "银行",
        "industry_l2": None, "industry_l3": None, "industry_source": "sw2021_legulegu",
        "taxonomy": "sw2021", "industry_code": "801780.SI",
    }]


def test_cn_industry_backfill_rejects_insufficient_coverage(monkeypatch):
    from data.ingest import service as service_module

    class _IncompleteFetcher:
        def fetch(self, stock_codes=None):
            return pd.DataFrame([{
                "stock_code": "600000.SH", "name": "浦发银行", "industry_l1": "银行",
                "industry_l2": None, "industry_l3": None, "industry_source": "sw2021_legulegu",
            }])

    monkeypatch.setattr(service_module, "SW2021IndustryFetcher", _IncompleteFetcher)
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = MarketDataService(base_dir=tmp_dir)
        try:
            result = service.backfill_cn_industry(
                stock_codes=["600000.SH", "000001.SZ"], min_coverage=0.95
            )
        finally:
            service.close()

    assert result["status"] == "coverage_failed"
    assert result["coverage"] == pytest.approx(0.5)
    assert result["detail"] == "coverage gate failed; registry was not modified"


def test_stock_info_bulk_read_filters_locally_and_keeps_latest_row(monkeypatch):
    warehouse = MarketDataWarehouse.__new__(MarketDataWarehouse)
    requested = [f"{index:06d}.SZ" for index in range(501)]
    rows = pd.DataFrame([
        {"market": "CN", "stock_code": "000001.SZ", "industry_l1": "旧", "ingest_time": "2026-01-01"},
        {"market": "CN", "stock_code": "000001.SZ", "industry_l1": "银行", "ingest_time": "2026-02-01"},
        {"market": "CN", "stock_code": "999999.SZ", "industry_l1": "无关", "ingest_time": "2026-02-01"},
    ])
    observed = {}

    def fake_read(filters=None, columns=None, order_by=None):
        observed["filters"] = filters
        return rows.copy()

    warehouse._read_stock_info_registry = fake_read
    frame = warehouse.read_stock_info(stock_codes=requested, market="CN")

    assert observed["filters"] == {"market": "CN"}
    assert frame.to_dict("records")[0]["industry_l1"] == "银行"
