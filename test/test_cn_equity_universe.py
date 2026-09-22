#!/usr/bin/env python3
"""CN equity-universe whitelist tests (P1.19 §1).

The OHLCV/feature layers historically held 128 SSE index codes (000001.SH is
上证指数) as if they were equity: 395,008 rows, 2.43% of every cross-section in
the 2025-09-19..2026-04-27 training window.  These tests pin the whitelist that
keeps them out of universes, panels and training frames.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.ingest.service import (
    CN_EQUITY_PREFIXES,
    MarketDataService,
    filter_cn_equity_codes,
    is_cn_equity_code,
)


@pytest.mark.parametrize(
    "code,expected",
    [
        ("600000.SH", True),
        ("601398.SH", True),
        ("603179.SH", True),
        ("605303.SH", True),
        ("688981.SH", True),
        ("689009.SH", True),
        ("000001.SZ", True),
        ("001979.SZ", True),
        ("002831.SZ", True),
        ("003816.SZ", True),
        ("300750.SZ", True),
        ("301139.SZ", True),
        ("302132.SZ", True),
        ("sh600000", True),
        ("300750", True),
        # SSE 000xxx is index space; 000001.SH is 上证指数.
        ("000001.SH", False),
        ("000148.SH", False),
        ("000300.SH", False),
        ("399001.SZ", False),
        ("900901.SH", False),   # SH B share
        ("200011.SZ", False),   # SZ B share
        ("920001.SH", False),   # BJ, no reliable daily OHLCV source
        ("430047.BJ", False),
        ("600000.SS", False),   # malformed suffix
        ("", False),
        ("600000", True),
    ],
)
def test_whitelist_matches_expected_prefixes(code, expected):
    assert is_cn_equity_code(code) is expected


def test_prefix_table_covers_both_exchanges():
    assert CN_EQUITY_PREFIXES["SH"] == ("600", "601", "603", "605", "688", "689")
    assert CN_EQUITY_PREFIXES["SZ"] == ("000", "001", "002", "003", "300", "301", "302")


def test_filter_keeps_order_and_deduplicates():
    kept, dropped = filter_cn_equity_codes(
        ["600000.SH", "000001.SH", "600000.SH", "300750.SZ", "000002.SH"]
    )
    assert kept == ["600000.SH", "300750.SZ"]
    assert dropped == ["000001.SH", "000002.SH"]


def test_filter_handles_empty_input():
    assert filter_cn_equity_codes(None) == ([], [])
    assert filter_cn_equity_codes([]) == ([], [])


class _StubWarehouse:
    def get_all_stock_codes(self, **kwargs):
        return ["600000.SH", "000001.SH", "300750.SZ", "000148.SH"]


def test_service_universe_drops_index_codes_for_cn_only():
    service = MarketDataService.__new__(MarketDataService)
    service.warehouse = _StubWarehouse()

    assert service.get_all_stock_codes(market="CN") == ["600000.SH", "300750.SZ"]
    # HK (and any other market) must be untouched by the CN whitelist.
    assert service.get_all_stock_codes(market="HK") == [
        "600000.SH", "000001.SH", "300750.SZ", "000148.SH",
    ]
