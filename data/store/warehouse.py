#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Parquet 主存储 + DuckDB 元数据查询层。"""

from pathlib import Path

import duckdb
import pandas as pd

from data.model import CLEAN_OHLCV_COLUMNS, STOCK_INFO_FIELDS
from data.store.parquet_store import ParquetDataStore


class MarketDataWarehouse:
    """管理 clean 层市场数据与元数据。"""

    OHLCV_DATASET = "ohlcv"

    def __init__(self, layout):
        self.layout = layout
        self.parquet_store = ParquetDataStore(layout)
        self.db_path = Path(self.layout.duckdb_path())
        self.conn = duckdb.connect(str(self.db_path))
        self._init_schema()

    def _init_schema(self):
        """初始化元数据表结构。"""
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stock_info_registry (
                stock_code VARCHAR NOT NULL,
                market VARCHAR NOT NULL,
                exchange VARCHAR NOT NULL,
                asset_type VARCHAR NOT NULL,
                name VARCHAR,
                current_price DOUBLE,
                close_price DOUBLE,
                open_price DOUBLE,
                high DOUBLE,
                low DOUBLE,
                volume DOUBLE,
                market_cap DOUBLE,
                pe_ratio DOUBLE,
                week_52_high DOUBLE,
                week_52_low DOUBLE,
                source VARCHAR,
                ingest_time TIMESTAMP,
                PRIMARY KEY (market, stock_code)
            )
            """
        )
        for statement in [
            "ALTER TABLE stock_info_registry ADD COLUMN IF NOT EXISTS exchange VARCHAR",
            "ALTER TABLE stock_info_registry ADD COLUMN IF NOT EXISTS asset_type VARCHAR",
            "ALTER TABLE stock_info_registry ADD COLUMN IF NOT EXISTS source VARCHAR",
            "ALTER TABLE stock_info_registry ADD COLUMN IF NOT EXISTS ingest_time TIMESTAMP",
        ]:
            self.conn.execute(statement)

    def upsert_ohlcv(self, frame, dataset_name=OHLCV_DATASET):
        """将标准 OHLCV 数据 upsert 到分区 parquet 数据集。"""
        if frame is None or frame.empty:
            return {"rows": 0, "dataset_path": str(self.layout.dataset_path(dataset_name, layer="clean"))}

        payload = frame[CLEAN_OHLCV_COLUMNS].copy()
        target = self.parquet_store.upsert_frame(
            dataset_name=dataset_name,
            frame=payload,
            dedupe_keys=["market", "stock_code", "trade_date", "frequency", "adjust"],
            layer="clean",
            sort_by=["market", "stock_code", "trade_date", "frequency", "adjust", "ingest_time"],
        )
        return {"rows": len(payload), "dataset_path": str(target)}

    def append_ohlcv(self, frame, dataset_name=OHLCV_DATASET):
        """批量追加 OHLCV 到分区 parquet 数据集，不做单次去重。"""
        if frame is None or frame.empty:
            return {"rows": 0, "dataset_path": str(self.layout.dataset_path(dataset_name, layer="clean"))}

        payload = frame[CLEAN_OHLCV_COLUMNS].copy()
        target = self.parquet_store.append_frame(
            dataset_name=dataset_name,
            frame=payload,
            layer="clean",
        )
        return {"rows": len(payload), "dataset_path": str(target)}

    def upsert_stock_info(self, info):
        """保存标准化后的股票信息。"""
        payload = pd.DataFrame([info], columns=STOCK_INFO_FIELDS)
        self.conn.register("stock_info_frame", payload)
        self.conn.execute(
            """
            INSERT OR REPLACE INTO stock_info_registry (
                stock_code,
                market,
                exchange,
                asset_type,
                name,
                current_price,
                close_price,
                open_price,
                high,
                low,
                volume,
                market_cap,
                pe_ratio,
                week_52_high,
                week_52_low,
                source,
                ingest_time
            )
            SELECT
                stock_code,
                market,
                exchange,
                asset_type,
                name,
                current_price,
                close_price,
                open_price,
                high,
                low,
                volume,
                market_cap,
                pe_ratio,
                week_52_high,
                week_52_low,
                source,
                ingest_time
            FROM stock_info_frame
            """
        )
        self.conn.unregister("stock_info_frame")
        return {"rows": 1}

    def upsert_stock_info_batch(self, info_list):
        """批量保存标准化后的股票信息。"""
        if not info_list:
            return {"rows": 0}

        payload = pd.DataFrame(info_list, columns=STOCK_INFO_FIELDS)
        payload.drop_duplicates(subset=["market", "stock_code"], keep="last", inplace=True)
        self.conn.register("stock_info_frame_batch", payload)
        self.conn.execute(
            """
            INSERT OR REPLACE INTO stock_info_registry (
                stock_code,
                market,
                exchange,
                asset_type,
                name,
                current_price,
                close_price,
                open_price,
                high,
                low,
                volume,
                market_cap,
                pe_ratio,
                week_52_high,
                week_52_low,
                source,
                ingest_time
            )
            SELECT
                stock_code,
                market,
                exchange,
                asset_type,
                name,
                current_price,
                close_price,
                open_price,
                high,
                low,
                volume,
                market_cap,
                pe_ratio,
                week_52_high,
                week_52_low,
                source,
                ingest_time
            FROM stock_info_frame_batch
            """
        )
        self.conn.unregister("stock_info_frame_batch")
        return {"rows": len(payload)}

    def read_ohlcv(
        self,
        stock_code=None,
        market=None,
        exchange=None,
        asset_type=None,
        frequency=None,
        adjust=None,
        start_date=None,
        end_date=None,
        dataset_name=OHLCV_DATASET,
    ):
        """按条件读取 clean 层 OHLCV 数据。"""
        filters = {
            "stock_code": stock_code,
            "market": market,
            "exchange": exchange,
            "asset_type": asset_type,
            "frequency": frequency,
            "adjust": adjust,
        }
        frame = self.parquet_store.read_frame(
            dataset_name=dataset_name,
            layer="clean",
            filters=filters,
            order_by="market, stock_code, trade_date",
        )
        if frame.empty:
            return frame

        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        if start_date:
            frame = frame.loc[frame["trade_date"] >= pd.to_datetime(start_date)]
        if end_date:
            frame = frame.loc[frame["trade_date"] <= pd.to_datetime(end_date)]
        frame.reset_index(drop=True, inplace=True)
        return frame

    def sync_ohlcv_to_parquet(self, dataset_name=OHLCV_DATASET, stock_code=None):
        """兼容旧接口，直接返回 parquet 数据集路径。"""
        dataset_path = self.layout.dataset_path(dataset_name, layer="clean")
        if stock_code and not self.parquet_store.dataset_exists(dataset_name, layer="clean"):
            return None
        return dataset_path if dataset_path.exists() else None

    def get_stock_info(self, stock_code, market=None):
        """读取标准化股票信息。"""
        clauses = ["stock_code = ?"]
        params = [stock_code]
        if market:
            clauses.append("market = ?")
            params.append(market)

        result = self.conn.execute(
            f"""
            SELECT {", ".join(STOCK_INFO_FIELDS)}
            FROM stock_info_registry
            WHERE {" AND ".join(clauses)}
            ORDER BY ingest_time DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
        if not result:
            return None
        return dict(zip(STOCK_INFO_FIELDS, result))

    def get_latest_trade_date(
        self,
        stock_code,
        market=None,
        exchange=None,
        asset_type=None,
        frequency=None,
        adjust=None,
        dataset_name=OHLCV_DATASET,
    ):
        """获取某只证券的最新交易日。"""
        filters = {
            "stock_code": stock_code,
            "market": market,
            "exchange": exchange,
            "asset_type": asset_type,
            "frequency": frequency,
            "adjust": adjust,
        }
        latest = self.parquet_store.scalar_query(
            dataset_name=dataset_name,
            expression="MAX(trade_date)",
            layer="clean",
            filters=filters,
        )
        if latest is None:
            return None
        latest_ts = pd.to_datetime(latest)
        if frequency and frequency != "daily":
            return latest_ts.strftime("%Y-%m-%d %H:%M:%S")
        return str(latest_ts.date())

    def get_statistics(
        self,
        stock_code=None,
        market=None,
        exchange=None,
        asset_type=None,
        frequency=None,
        adjust=None,
        dataset_name=OHLCV_DATASET,
    ):
        """获取 parquet 数据集统计信息。"""
        filters = {
            "stock_code": stock_code,
            "market": market,
            "exchange": exchange,
            "asset_type": asset_type,
            "frequency": frequency,
            "adjust": adjust,
        }
        total_records = self.parquet_store.scalar_query(
            dataset_name=dataset_name,
            expression="COUNT(*)",
            layer="clean",
            filters=filters,
        )
        if total_records in (None, 0):
            return None

        min_date = self.parquet_store.scalar_query(
            dataset_name=dataset_name,
            expression="MIN(trade_date)",
            layer="clean",
            filters=filters,
        )
        max_date = self.parquet_store.scalar_query(
            dataset_name=dataset_name,
            expression="MAX(trade_date)",
            layer="clean",
            filters=filters,
        )
        return {
            "total_records": int(total_records),
            "date_range": (pd.to_datetime(min_date), pd.to_datetime(max_date)),
            "dataset_path": str(self.layout.dataset_path(dataset_name, layer="clean")),
        }

    def get_all_stock_codes(self, market=None, asset_type=None, frequency=None, adjust=None, dataset_name=OHLCV_DATASET):
        """获取 parquet 数据集中全部证券代码。"""
        filters = {
            "market": market,
            "asset_type": asset_type,
            "frequency": frequency,
            "adjust": adjust,
        }
        return self.parquet_store.values_query(
            dataset_name=dataset_name,
            column="stock_code",
            layer="clean",
            filters=filters,
            distinct=True,
            order_by="value",
        )

    def get_total_rows(self, market=None, asset_type=None, frequency=None, adjust=None, dataset_name=OHLCV_DATASET):
        """获取 parquet 数据集总行数。"""
        filters = {
            "market": market,
            "asset_type": asset_type,
            "frequency": frequency,
            "adjust": adjust,
        }
        total = self.parquet_store.scalar_query(
            dataset_name=dataset_name,
            expression="COUNT(*)",
            layer="clean",
            filters=filters,
        )
        return int(total or 0)

    def compact_ohlcv(self, dataset_name=OHLCV_DATASET):
        """对 OHLCV 数据集进行统一压实去重。"""
        target = self.parquet_store.compact_dataset(
            dataset_name=dataset_name,
            dedupe_keys=["market", "stock_code", "trade_date", "frequency", "adjust"],
            sort_by=["ingest_time", "trade_date"],
            layer="clean",
        )
        return {"dataset_path": str(target)}

    def close(self):
        """关闭仓库连接。"""
        self.conn.close()
