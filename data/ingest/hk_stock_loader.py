#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""港股数据 loader，连接 legacy fetcher 与新数据层。"""

from pathlib import Path

from data.ingest.base import BaseMarketDataLoader
from data.ingest.providers.history_utils import normalize_period
from data.ingest.providers import HistoryDataFetcher, StockInfoFetcher
from data.model import infer_exchange, normalize_ohlcv_frame, normalize_stock_code, normalize_stock_info
from data.store.layout import DataLayout
from data.store.warehouse import MarketDataWarehouse


class HKStockDataLoader(BaseMarketDataLoader):
    """港股数据加载器。"""

    def __init__(self, base_dir="./assets/data", data_source="akshare", warehouse=None):
        self.layout = DataLayout(base_dir=base_dir)
        self.db_dir = str(Path(base_dir).resolve().parent)
        self.data_source = data_source
        self.warehouse = warehouse or MarketDataWarehouse(self.layout)

    def fetch_history(self, stock_code, start_date=None, end_date=None, num_records=None, adjust="qfq", period="daily"):
        """抓取并标准化港股历史数据。"""
        normalized_code = normalize_stock_code(stock_code, market="HK")
        normalized_period = normalize_period(period)
        fetcher = HistoryDataFetcher(
            normalized_code,
            db_dir=self.db_dir,
            data_source=self.data_source,
            adjust=adjust,
        )
        frame = fetcher.fetch(
            start_date=start_date,
            end_date=end_date,
            num_records=num_records,
            adjust=adjust,
            period=normalized_period,
        )
        if frame is None:
            return None
        source = fetcher.last_successful_source or self.data_source
        return normalize_ohlcv_frame(
            frame,
            stock_code=normalized_code,
            market="HK",
            exchange="HKEX",
            asset_type="equity",
            frequency=normalized_period,
            source=source,
            adjust=adjust,
            currency="HKD",
        )

    def fetch_info(self, stock_code):
        """抓取并标准化港股基础信息。"""
        normalized_code = normalize_stock_code(stock_code, market="HK")
        fetcher = StockInfoFetcher(normalized_code, data_source=self.data_source)
        info = fetcher.fetch()
        if info is None:
            return None
        source = fetcher.last_successful_source or self.data_source
        return normalize_stock_info(
            info,
            stock_code=normalized_code,
            market="HK",
            exchange=infer_exchange(normalized_code, market="HK"),
            source=source,
        )

    def sync(self, stock_code, start_date=None, end_date=None, num_records=None, adjust="qfq", period="daily", include_info=True):
        """抓取港股数据并写入新数据层。"""
        history = self.fetch_history(
            stock_code,
            start_date=start_date,
            end_date=end_date,
            num_records=num_records,
            adjust=adjust,
            period=period,
        )
        result = {"history_rows": 0, "info_rows": 0, "parquet_path": None}
        if history is not None and not history.empty:
            warehouse_result = self.warehouse.upsert_ohlcv(history)
            result["history_rows"] = warehouse_result["rows"]
            result["parquet_path"] = warehouse_result["dataset_path"]

        if include_info:
            info = self.fetch_info(stock_code)
            if info:
                result["info_rows"] = self.warehouse.upsert_stock_info(info)["rows"]

        return result
