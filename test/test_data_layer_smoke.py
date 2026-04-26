#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""数据层 smoke test，不依赖外网。"""

import tempfile
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.model import normalize_ohlcv_frame, normalize_stock_info
from data.store import DataLayout, MarketDataWarehouse
from data.store import DatabaseManager


def test_data_layer_smoke():
    raw_frame = pd.DataFrame(
        {
            "date": ["2024-01-02", "2024-01-03", "2024-01-03"],
            "Open": [100, 101, 101],
            "High": [102, 103, 103],
            "Low": [99, 100, 100],
            "Close": [101, 102, 102],
            "Volume": [1000, 2000, 2000],
        }
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        layout = DataLayout(base_dir=tmp_dir)
        warehouse = MarketDataWarehouse(layout)

        clean_frame = normalize_ohlcv_frame(raw_frame, stock_code="00700", source="unit_test")
        assert len(clean_frame) == 2
        assert clean_frame["stock_code"].iloc[0] == "00700"
        assert clean_frame["exchange"].iloc[0] == "HKEX"
        assert clean_frame["frequency"].iloc[0] == "daily"

        upsert_stats = warehouse.upsert_ohlcv(clean_frame)
        assert upsert_stats["rows"] == 2

        info = normalize_stock_info(
            {
                "name": "Tencent",
                "current_price": 102.0,
                "close_price": 101.0,
            },
            stock_code="00700",
            source="unit_test",
        )
        warehouse.upsert_stock_info(info)

        loaded = warehouse.read_ohlcv(stock_code="00700")
        assert len(loaded) == 2
        assert set(["stock_code", "trade_date", "exchange", "frequency", "open", "high", "low", "close", "volume"]).issubset(loaded.columns)

        parquet_path = warehouse.sync_ohlcv_to_parquet()
        assert parquet_path is not None
        assert parquet_path.exists()
        assert any(parquet_path.rglob("*.parquet"))

        warehouse.close()

    with tempfile.TemporaryDirectory() as tmp_dir:
        manager = DatabaseManager(tmp_dir)
        index_frame = raw_frame.copy()
        index_frame["date"] = pd.to_datetime(index_frame["date"])
        index_frame.set_index("date", inplace=True)
        index_frame.index.name = "date"

        bridge_payload = index_frame.rename(columns={
            "Open": "Open",
            "High": "High",
            "Low": "Low",
            "Close": "Close",
            "Volume": "Volume",
        })

        save_stats = manager.save_kline_data(bridge_payload, "00700")
        assert save_stats["total_records"] == 2
        assert save_stats["parquet_path"] is not None

        loaded = manager.get_kline_data("00700")
        assert loaded is not None
        assert len(loaded) == 2
        assert list(loaded.columns) == ["Open", "Close", "High", "Low", "Volume"]

        stats = manager.get_statistics("00700")
        assert stats is not None
        assert stats["total_records"] == 2

        manager.close()


if __name__ == "__main__":
    test_data_layer_smoke()
    print("data layer smoke test passed")
