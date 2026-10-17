"""A 股日线缓存的失效口径（2026-10-17 事故回归）。

事故：600252.SH 的 2026-10-16 日线在盘后 `daily_bars` 补数后才入 parquet，
但 `/api/stocks/600252.SH/ohlcv?days=365` 命中了补数前的 `lru_cache`，
页面一直停在 10-15；换个 `days` 又能看到 10-16。

判据：缓存键必须带"分区数据版本"（parquet mtime），新数据落盘后同参数再取要能看见。
"""
from __future__ import annotations

import os
import time

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from backend.services import data_service as ds


def _write_shard(path, rows):
    frame = pd.DataFrame(rows, columns=["trade_date", "stock_code", "open", "high", "low", "close", "volume"])
    pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), path)


def test_ohlcv_cache_invalidates_when_partition_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "_CN_BARS_DIR", tmp_path)
    ds._load_cn_stock_ohlcv_cached.cache_clear()

    shard = tmp_path / "exchange=SSE/asset_type=equity/frequency=daily/adjust=qfq/year=2026"
    shard.mkdir(parents=True)
    path = shard / "part.parquet"

    _write_shard(path, [
        ["2026-10-14", "600252.SH", 2.24, 2.48, 2.23, 2.48, 196_457_600],
        ["2026-10-15", "600252.SH", 2.45, 2.49, 2.26, 2.30, 278_218_500],
    ])
    first = ds.load_cn_stock_ohlcv("600252.SH", days=365)
    assert first is not None and str(first.index[-1].date()) == "2026-10-15"

    # 补数：写入 10-16，并把 mtime 推后（模拟 daily_bars 重写分区）
    _write_shard(path, [
        ["2026-10-14", "600252.SH", 2.24, 2.48, 2.23, 2.48, 196_457_600],
        ["2026-10-15", "600252.SH", 2.45, 2.49, 2.26, 2.30, 278_218_500],
        ["2026-10-16", "600252.SH", 2.32, 2.53, 2.32, 2.46, 338_501_600],
    ])
    os.utime(path, (time.time() + 5, time.time() + 5))

    second = ds.load_cn_stock_ohlcv("600252.SH", days=365)
    assert second is not None and str(second.index[-1].date()) == "2026-10-16"
    assert float(second["Close"].iloc[-1]) == 2.46


def test_ohlcv_cache_hits_for_same_version(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "_CN_BARS_DIR", tmp_path)
    ds._load_cn_stock_ohlcv_cached.cache_clear()
    shard = tmp_path / "exchange=SSE/asset_type=equity/frequency=daily/adjust=qfq/year=2026"
    shard.mkdir(parents=True)
    _write_shard(shard / "part.parquet", [
        ["2026-10-16", "600252.SH", 2.32, 2.53, 2.32, 2.46, 338_501_600],
    ])
    ds.load_cn_stock_ohlcv("600252.SH", days=365)
    info = ds._load_cn_stock_ohlcv_cached.cache_info()
    ds.load_cn_stock_ohlcv("600252.SH", days=365)
    assert ds._load_cn_stock_ohlcv_cached.cache_info().hits == info.hits + 1
