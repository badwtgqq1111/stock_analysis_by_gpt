#!/usr/bin/env python3
"""Tushare-compatible SW2021 relay provider tests."""

import sys
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.ingest.providers.cn_sw2021_relay import SW2021RelayIndustryFetcher


class _Response:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


def test_sw2021_relay_fetches_catalog_and_three_level_membership():
    catalog = {
        "code": 0,
        "data": {
            "fields": ["index_code", "industry_name", "industry_code", "level", "src"],
            "items": [["801780.SI", "银行", "480000", "L1", "SW2021"]],
        },
    }
    members = {
        "code": 0,
        "data": {
            "fields": ["l1_code", "l1_name", "l2_code", "l2_name", "l3_code", "l3_name", "ts_code", "name", "in_date", "out_date", "is_new"],
            "items": [["801780.SI", "银行", "801783.SI", "股份制银行Ⅱ", "857831.SI", "股份制银行Ⅲ", "000001.SZ", "平安银行", "19910403", None, "Y"]],
        },
    }

    def fake_get(url, params, headers, timeout):
        assert headers == {"X-API-Key": "test-key"}
        return _Response(catalog if url.endswith("index_classify") else members)

    frame = SW2021RelayIndustryFetcher(base_api_key="test-key", request_get=fake_get).fetch()

    assert frame[["stock_code", "industry_l1", "industry_l2", "industry_l3", "effective_from"]].to_dict("records") == [{
        "stock_code": "000001.SZ", "industry_l1": "银行", "industry_l2": "股份制银行Ⅱ",
        "industry_l3": "股份制银行Ⅲ", "effective_from": "19910403",
    }]
    assert frame.loc[0, "taxonomy"] == "sw2021"


def test_sw2021_relay_uses_promax_after_base_error():
    catalog = {"code": 0, "data": {"fields": ["index_code", "industry_name", "industry_code", "level", "src"], "items": [["801780.SI", "银行", "480000", "L1", "SW2021"]]}}
    members = {"code": 0, "data": {"fields": ["l1_code", "l1_name", "l2_code", "l2_name", "l3_code", "l3_name", "ts_code"], "items": [["801780.SI", "银行", "801783.SI", "股份制银行Ⅱ", "857831.SI", "股份制银行Ⅲ", "600000.SH"]]}}

    def fake_get(url, params, headers, timeout):
        if headers["X-API-Key"] == "base-key":
            return type("ErrorResponse", (), {"status_code": 503})()
        return _Response(catalog if url.endswith("index_classify") else members)

    frame = SW2021RelayIndustryFetcher(
        base_api_key="base-key", promax_api_key="pro-key", request_get=fake_get
    ).fetch(stock_codes=["600000.SH"])

    assert frame.loc[0, "relay_gateway"] == "promax"
