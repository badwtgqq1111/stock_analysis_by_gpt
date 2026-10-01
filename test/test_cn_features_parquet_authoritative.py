"""R1：CN 特征 Parquet 权威标记的路由回归测试。"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from data.store.warehouse import MarketDataWarehouse


class _Layout:
    def __init__(self, base: Path):
        self.base = base

    def layer_path(self, layer: str) -> Path:
        path = self.base / layer
        path.mkdir(parents=True, exist_ok=True)
        return path

    def dataset_path(self, name: str, layer: str = "clean") -> Path:
        return self.base / layer / name


def _warehouse(tmp_path: Path) -> MarketDataWarehouse:
    return MarketDataWarehouse(_Layout(tmp_path))


class _FakeStore:
    def __init__(self, name):
        self.name = name


def test_cn_features_use_clickhouse_until_marker_is_published(tmp_path):
    warehouse = _warehouse(tmp_path)
    warehouse.clickhouse_store = _FakeStore("clickhouse")
    names = [type(store).__name__ for store in warehouse._feature_store_candidates(market="CN")]
    assert names == ["_FakeStore", "ParquetDataStore"]


def test_marker_switches_cn_features_to_parquet_only(tmp_path):
    warehouse = _warehouse(tmp_path)
    warehouse.clickhouse_store = _FakeStore("clickhouse")
    warehouse.layout.layer_path("meta").joinpath("cn_features_parquet_authoritative.json").write_text("{}", encoding="utf-8")
    assert [type(store).__name__ for store in warehouse._feature_store_candidates(market="CN")] == ["ParquetDataStore"]
    # 其他市场不受影响
    assert [type(store).__name__ for store in warehouse._feature_store_candidates(market="HK")] == ["_FakeStore", "ParquetDataStore"]
    assert [type(store).__name__ for store in warehouse._feature_store_candidates()] == ["_FakeStore", "ParquetDataStore"]


def test_valuation_read_keeps_primary_rows_when_store_is_soft_disabled(tmp_path):
    """估值快照读取：主表被 soft-disable 后也必须拿到主表，而不是退化成残缺镜像。"""
    import pandas as pd

    from data.store.warehouse import MarketDataWarehouse

    class _Store:
        def __init__(self, frame):
            self.frame = frame
            self.calls = 0

        def read_frame(self, *args, **kwargs):
            self.calls += 1
            return self.frame.copy()

    primary = pd.DataFrame({"market": ["CN", "CN"], "stock_code": ["000002.SZ"] * 2,
                            "trade_date": ["2026-08-10", "2026-09-23"], "market_cap": [387.75, 467.68]})
    mirror = pd.DataFrame({"market": ["CN"], "stock_code": ["000002.SZ"],
                           "trade_date": ["2026-08-09"], "market_cap": [31289000000.0]})
    warehouse = _warehouse(tmp_path)
    warehouse.clickhouse_store = _Store(primary)
    warehouse._clickhouse_disabled_reason = "simulated bulky-query failure"
    warehouse.parquet_store = _Store(mirror)

    frame = warehouse.read_valuation_snapshots(stock_codes=["000002.SZ"], market="CN")
    assert warehouse.clickhouse_store.calls == 1  # 主表被显式重试
    assert len(frame) == 3  # 主表 2 行 + 镜像补充 1 行
    assert set(pd.to_datetime(frame["trade_date"]).dt.strftime("%Y-%m-%d")) == {"2026-08-09", "2026-08-10", "2026-09-23"}
