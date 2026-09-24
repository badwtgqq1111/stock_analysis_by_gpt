"""CN daily cutover keeps Parquet as one authoritative store after publish."""

from pathlib import Path

import pandas as pd
import pytest

from data.store.layout import DataLayout
from data.store.parquet_store import ParquetDataStore
from data.store.warehouse import MarketDataWarehouse
from scripts.migrate_cn_daily_clickhouse_to_parquet import migrate_cn_daily
from scripts.rollback_cn_daily_parquet_cutover import rollback_cutover


def _daily_frame(source="tencent"):
    return pd.DataFrame({
        "market": ["CN", "CN"], "exchange": ["SZSE", "SSE"],
        "asset_type": ["equity", "equity"], "frequency": ["daily", "daily"],
        "adjust": ["qfq", "qfq"], "stock_code": ["000721.SZ", "600000.SH"],
        "trade_date": pd.to_datetime(["2026-09-23", "2026-09-23"]),
        "open": [10.0, 20.0], "high": [10.2, 20.2], "low": [9.8, 19.8],
        "close": [10.1, 20.1], "volume": [1_000_000.0, 2_000_000.0],
        "amount": [10_100_000.0, 40_200_000.0], "turnover": [None, None],
        "vwap": [10.1, 20.1], "source": [source, source],
        "ingest_time": pd.to_datetime(["2026-09-23T16:00:00"] * 2),
    })


class _ClickHouseSource:
    def __init__(self, frame):
        self.frame = frame

    def read_frame(self, **kwargs):
        return self.frame.copy()


def test_dry_run_snapshots_but_does_not_change_parquet_or_marker(tmp_path):
    layout = DataLayout(str(tmp_path / "data"))
    parquet = ParquetDataStore(layout)
    result = migrate_cn_daily(
        clickhouse_store=_ClickHouseSource(_daily_frame()), parquet_store=parquet,
        dataset_name="ohlcv", data_root=layout.base_path, start_date="2026-09-23",
        end_date="2026-09-23", output_dir=tmp_path / "dry", publish=False,
    )
    assert result["selected_rows"] == 2
    assert result["production_mutated"] is False
    assert (tmp_path / "dry" / "SOURCE_SNAPSHOT.parquet").is_file()
    assert not parquet.dataset_exists("ohlcv")
    assert not (layout.layer_path("meta") / "cn_daily_parquet_authoritative.json").exists()


def test_publish_writes_missing_keys_then_enables_cn_daily_parquet_route(tmp_path):
    layout = DataLayout(str(tmp_path / "data"))
    parquet = ParquetDataStore(layout)
    source = _daily_frame()
    result = migrate_cn_daily(
        clickhouse_store=_ClickHouseSource(source), parquet_store=parquet,
        dataset_name="ohlcv", data_root=layout.base_path, start_date="2026-09-23",
        end_date="2026-09-23", output_dir=tmp_path / "publish", publish=True,
    )
    restored = parquet.read_frame("ohlcv", filters={"market": "CN"})
    assert result["production_mutated"] is True
    assert len(result["new_parquet_files"]) == 2
    assert len(restored) == 2
    assert restored.sort_values("stock_code")["volume"].tolist() == [1_000_000.0, 2_000_000.0]
    assert (layout.layer_path("meta") / "cn_daily_parquet_authoritative.json").is_file()
    warehouse = MarketDataWarehouse(layout, clickhouse_store=_ClickHouseSource(source))
    assert warehouse._ohlcv_store_candidates("daily", "CN") == [warehouse.parquet_store]
    assert warehouse._ohlcv_store_candidates("daily", "HK")[0] is warehouse.clickhouse_store


def test_rejects_synthetic_clickhouse_rows_before_output_or_publish(tmp_path):
    layout = DataLayout(str(tmp_path / "data"))
    with pytest.raises(ValueError, match="unknown synthetic bars"):
        migrate_cn_daily(
            clickhouse_store=_ClickHouseSource(_daily_frame(source="unknown")),
            parquet_store=ParquetDataStore(layout), dataset_name="ohlcv", data_root=layout.base_path,
            start_date="2026-09-23", end_date="2026-09-23", output_dir=tmp_path / "invalid", publish=True,
        )
    assert not (tmp_path / "invalid").exists()
    assert not (layout.layer_path("meta") / "cn_daily_parquet_authoritative.json").exists()


def test_rollback_removes_only_manifested_files_and_cutover_marker(tmp_path):
    layout = DataLayout(str(tmp_path / "data"))
    result = migrate_cn_daily(
        clickhouse_store=_ClickHouseSource(_daily_frame()), parquet_store=ParquetDataStore(layout),
        dataset_name="ohlcv", data_root=layout.base_path, start_date="2026-09-23",
        end_date="2026-09-23", output_dir=tmp_path / "publish", publish=True,
    )
    manifest = tmp_path / "publish" / "manifest.json"
    rollback = rollback_cutover(manifest_path=manifest, data_root=layout.base_path)
    assert len(rollback["removed_files"]) == len(result["new_parquet_files"])
    assert rollback["marker_removed"] is True
    assert not ParquetDataStore(layout).dataset_exists("ohlcv")
    assert not (layout.layer_path("meta") / "cn_daily_parquet_authoritative.json").exists()


def test_after_cutover_cn_daily_append_uses_only_parquet(tmp_path):
    layout = DataLayout(str(tmp_path / "data"))
    marker = layout.layer_path("meta") / "cn_daily_parquet_authoritative.json"
    marker.write_text('{"authoritative_store":"parquet"}', encoding="utf-8")

    class NoWriteClickHouse:
        def append_frame(self, **kwargs):
            raise AssertionError("CN daily must not write ClickHouse")

    warehouse = MarketDataWarehouse(layout, clickhouse_store=NoWriteClickHouse())
    frame = _daily_frame()
    frame["currency"] = "CNY"
    result = warehouse.append_ohlcv(frame)
    assert result["rows"] == 2
    assert len(warehouse.parquet_store.read_frame("ohlcv")) == 2
