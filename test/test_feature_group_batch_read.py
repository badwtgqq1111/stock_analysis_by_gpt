"""回归：分组批读不能因为“部分写的新批次”丢掉同一只股票的其它行组。

2026-09-26 事故：RPS 步骤只写每股 2~3 行、时间戳最新；旧的索引逻辑“只保留该股票
最新 ingest 的行组”，于是把该股票全部历史行组一起丢掉，导致最新交易日面板里
3,473/5,208 只股票只剩 RPS 等少量列（GTJA116 只有 1,735 只有值）。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from data.store.parquet_store import ParquetDataStore
from data.store.warehouse import MarketDataWarehouse


class _Layout:
    def __init__(self, base: Path):
        self.base = base

    def dataset_path(self, name: str, layer: str = "clean") -> Path:
        return self.base / layer / name

    def layer_path(self, layer: str) -> Path:
        path = self.base / layer
        path.mkdir(parents=True, exist_ok=True)
        return path


def _feature_frame(stock: str, dates: list[str], feature: str, value: float, ingest: str) -> pd.DataFrame:
    return pd.DataFrame({
        "market": "CN", "stock_code": stock, "trade_date": pd.to_datetime(dates),
        "frequency": "daily", "adjust": "qfq", "asset_type": "equity", "exchange": "SZSE",
        "feature_set": "alpha_zoo_hk", "feature_version": "0.1.0", "feature_config_hash": "hash1",
        "feature_name": feature, "feature_value": value, "source": "factor_engine", "currency": "CNY",
        "ingest_time": pd.to_datetime([ingest] * len(dates)),
    })


def test_partial_newer_batch_does_not_hide_older_row_groups(tmp_path):
    store = ParquetDataStore(_Layout(tmp_path))
    stock = "000001.SZ"
    # 第一批：全窗口的常规特征（较旧 ingest）
    store.append_frame("features", _feature_frame(stock, ["2026-09-22", "2026-09-23", "2026-09-24"], "GTJA116", 1.0,
                                                  "2026-09-24T12:00:00"), layer="feature",
                       date_column="trade_date",
                       partition_columns=MarketDataWarehouse.FEATURES_PARTITION_COLUMNS)
    # 第二批：RPS 的“部分写”，只有最后一个交易日、更新时间更晚
    store.append_frame("features", _feature_frame(stock, ["2026-09-24"], "RPS_20", 0.5,
                                                  "2026-09-25T12:00:00"), layer="feature",
                       date_column="trade_date",
                       partition_columns=MarketDataWarehouse.FEATURES_PARTITION_COLUMNS)

    frames = []
    for _codes, frame in store.iter_frames_by_group_batches(
        "features", layer="feature", group_column="stock_code", group_values=[stock], batch_size=10,
        filters={"market": "CN", "asset_type": "equity", "frequency": "daily", "adjust": "qfq",
                 "feature_set": "alpha_zoo_hk", "feature_version": "0.1.0", "feature_config_hash": "hash1"},
        range_filters={"trade_date": {"gte": "2026-09-01", "lte": "2026-09-30"}},
        columns=["stock_code", "trade_date", "feature_name", "feature_value", "ingest_time"],
    ):
        frames.append(frame)
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    assert not combined.empty
    got = set(zip(combined.feature_name, pd.to_datetime(combined.trade_date).dt.strftime("%Y-%m-%d")))
    assert ("GTJA116", "2026-09-24") in got, "较旧但更完整的批次被部分写的新批次挤掉了"
    assert ("GTJA116", "2026-09-22") in got
    assert ("RPS_20", "2026-09-24") in got
