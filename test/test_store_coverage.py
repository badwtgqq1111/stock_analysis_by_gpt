#!/usr/bin/env python3
"""The OHLCV coverage aggregate must prefer ClickHouse over the lagging mirror."""

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.store.warehouse import MarketDataWarehouse  # noqa: E402


class _StubStore:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def group_count_and_max(self, **_kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def _warehouse(clickhouse, parquet):
    warehouse = MarketDataWarehouse.__new__(MarketDataWarehouse)
    warehouse.clickhouse_store = clickhouse
    warehouse.parquet_store = parquet
    warehouse._clickhouse_disabled_reason = None
    return warehouse


def test_coverage_merges_the_newest_date_with_the_long_history():
    # ClickHouse carries the newest session but is a sparse window; the mirror has
    # the long history but lags.  The merged answer must take both.
    clickhouse = _StubStore(({"A": 37}, {"A": pd.Timestamp("2026-09-23")}))
    parquet = _StubStore(({"A": 13916}, {"A": pd.Timestamp("2026-09-22")}))
    counts, latest = _warehouse(clickhouse, parquet).ohlcv_coverage_by_stock(stock_codes=["A"], market="CN")
    assert latest["A"] == pd.Timestamp("2026-09-23")
    assert counts["A"] == 13916       # enough history for the 120-row gate
    assert clickhouse.calls == 1 and parquet.calls == 1


def test_coverage_falls_back_to_the_mirror_when_clickhouse_fails():
    clickhouse = _StubStore(error=RuntimeError("too many form fields"))
    parquet = _StubStore(({"A": 9}, {"A": pd.Timestamp("2026-09-22")}))
    warehouse = _warehouse(clickhouse, parquet)
    counts, latest = warehouse.ohlcv_coverage_by_stock(stock_codes=["A"], market="CN")
    assert latest["A"] == pd.Timestamp("2026-09-22")
    assert parquet.calls == 1
    assert warehouse._clickhouse_disabled_reason is not None


def test_coverage_returns_empty_when_both_stores_have_nothing():
    counts, latest = _warehouse(_StubStore(({}, {})), _StubStore(({}, {}))).ohlcv_coverage_by_stock(
        stock_codes=["A"], market="CN"
    )
    assert counts == {} and latest == {}
