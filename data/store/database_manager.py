#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""统一数据库管理器。"""

import json
import os
from datetime import datetime

import duckdb
import pandas as pd

from data.model import infer_exchange, normalize_ohlcv_frame, normalize_stock_code, normalize_stock_info
from data.store.layout import DataLayout
from data.store.warehouse import MarketDataWarehouse


class DatabaseManager:
    """元数据 DuckDB + Parquet 主存储管理器。"""

    def __init__(self, db_dir="./assets"):
        self.db_dir = db_dir
        os.makedirs(db_dir, exist_ok=True)
        self.db_path = os.path.join(db_dir, "stock_data.duckdb")
        self.conn = None
        self.data_layout = DataLayout(os.path.join(db_dir, "data"))
        self.market_warehouse = MarketDataWarehouse(self.data_layout)
        self._init_db()

    def _init_db(self):
        try:
            self.conn = duckdb.connect(self.db_path)
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS stock_info (
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
                    update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (market, stock_code)
                )
                """
            )
            for statement in [
                "ALTER TABLE stock_info ADD COLUMN IF NOT EXISTS market VARCHAR DEFAULT 'HK'",
                "ALTER TABLE stock_info ADD COLUMN IF NOT EXISTS exchange VARCHAR DEFAULT 'HKEX'",
                "ALTER TABLE stock_info ADD COLUMN IF NOT EXISTS asset_type VARCHAR DEFAULT 'equity'",
                "ALTER TABLE stock_info ADD COLUMN IF NOT EXISTS source VARCHAR DEFAULT 'unknown'",
                "ALTER TABLE stock_info ADD COLUMN IF NOT EXISTS update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
            ]:
                self.conn.execute(statement)
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS update_log (
                    stock_code VARCHAR NOT NULL,
                    market VARCHAR NOT NULL,
                    action VARCHAR,
                    new_records INTEGER,
                    updated_records INTEGER,
                    extra JSON,
                    update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            for statement in [
                "ALTER TABLE update_log ADD COLUMN IF NOT EXISTS market VARCHAR DEFAULT 'HK'",
                "ALTER TABLE update_log ADD COLUMN IF NOT EXISTS extra JSON",
            ]:
                self.conn.execute(statement)
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scanned_stocks (
                    stock_code VARCHAR PRIMARY KEY NOT NULL,
                    name VARCHAR,
                    scan_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_update TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status VARCHAR DEFAULT 'active'
                )
                """
            )
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_stock_info_code ON stock_info(market, stock_code)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_update_log_code ON update_log(market, stock_code, update_time)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_scanned_code ON scanned_stocks(stock_code)")
            print(f"[OK] DuckDB 元数据库已初始化: {self.db_path}")
        except Exception as e:
            print(f"[ERROR] 数据库初始化错误: {e}")

    def execute_query(self, query, params=None):
        """执行元数据 SQL，主要用于扫描辅助逻辑。"""
        if self.conn is None:
            raise RuntimeError(f"数据库连接不可用: {self.db_path}")
        return self.conn.execute(query, params or []).fetchall()

    def save_stock_info(self, stock_info, stock_code, market="HK", exchange=None, asset_type="equity"):
        if not stock_info:
            return False

        try:
            if self.conn is None:
                print(f"[WARNING] stock_info 元数据库连接不可用，跳过元表写入: {self.db_path}")
                return False
            normalized_market = (market or "HK").upper()
            normalized_code = normalize_stock_code(stock_code, market=normalized_market)
            normalized_info = normalize_stock_info(
                stock_info,
                stock_code=normalized_code,
                market=normalized_market,
                exchange=exchange,
                asset_type=asset_type,
                source=stock_info.get("source"),
            )

            self.conn.execute(
                """
                INSERT OR REPLACE INTO stock_info (
                    stock_code, market, exchange, asset_type, name, current_price, close_price, open_price,
                    high, low, volume, market_cap, pe_ratio, week_52_high, week_52_low, source, update_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_info["stock_code"],
                    normalized_info["market"],
                    normalized_info["exchange"],
                    normalized_info["asset_type"],
                    normalized_info.get("name"),
                    normalized_info.get("current_price"),
                    normalized_info.get("close_price"),
                    normalized_info.get("open_price"),
                    normalized_info.get("high"),
                    normalized_info.get("low"),
                    normalized_info.get("volume"),
                    normalized_info.get("market_cap"),
                    normalized_info.get("pe_ratio"),
                    normalized_info.get("week_52_high"),
                    normalized_info.get("week_52_low"),
                    normalized_info.get("source"),
                    datetime.now().isoformat(),
                ),
            )
            self.market_warehouse.upsert_stock_info(normalized_info)
            return True
        except Exception as e:
            print(f"[ERROR] 保存股票信息错误: {e}")
            return False

    def _ensure_stock_info_exists(self, stock_code, market="HK", exchange=None, asset_type="equity"):
        try:
            if self.conn is None:
                return False
            normalized_market = (market or "HK").upper()
            normalized_code = normalize_stock_code(stock_code, market=normalized_market)
            normalized_exchange = (exchange or infer_exchange(normalized_code, market=normalized_market)).upper()
            exists = self.conn.execute(
                "SELECT COUNT(*) FROM stock_info WHERE market = ? AND stock_code = ?",
                (normalized_market, normalized_code),
            ).fetchone()[0]
            if exists:
                return True
            self.conn.execute(
                """
                INSERT OR IGNORE INTO stock_info (
                    stock_code, market, exchange, asset_type, name, source, update_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_code,
                    normalized_market,
                    normalized_exchange,
                    asset_type,
                    normalized_code,
                    "placeholder",
                    datetime.now().isoformat(),
                ),
            )
            return True
        except Exception as e:
            print(f"[ERROR] 初始化股票信息占位记录失败: {e}")
            return False

    def save_kline_data(
        self,
        data,
        stock_code,
        market="HK",
        exchange=None,
        asset_type="equity",
        frequency="daily",
        adjust="qfq",
        currency=None,
        source="database_manager",
    ):
        if data is None or data.empty:
            print("[ERROR] 没有数据可保存")
            return None

        try:
            if self.conn is None:
                print(f"[WARNING] 元数据库连接不可用，跳过旧式 save_kline_data 写入: {self.db_path}")
                return None
            normalized_market = (market or "HK").upper()
            normalized_code = normalize_stock_code(stock_code, market=normalized_market)
            normalized_exchange = (exchange or infer_exchange(normalized_code, market=normalized_market)).upper()
            self._ensure_stock_info_exists(
                normalized_code,
                market=normalized_market,
                exchange=normalized_exchange,
                asset_type=asset_type,
            )

            normalized_frame = normalize_ohlcv_frame(
                data,
                stock_code=normalized_code,
                market=normalized_market,
                exchange=normalized_exchange,
                asset_type=asset_type,
                frequency=frequency,
                source=source,
                adjust=adjust,
                currency=currency,
            )
            if normalized_frame.empty:
                print("[ERROR] 标准化后没有可保存的数据")
                return None

            existing = self.market_warehouse.read_ohlcv(
                stock_code=normalized_code,
                market=normalized_market,
                exchange=normalized_exchange,
                asset_type=asset_type,
                frequency=frequency,
                adjust=adjust,
            )
            existing_keys = set()
            if existing is not None and not existing.empty:
                existing_keys = {
                    (
                        row.market,
                        row.stock_code,
                        pd.Timestamp(row.trade_date).date().isoformat(),
                        row.frequency,
                        row.adjust,
                    )
                    for row in existing.itertuples(index=False)
                }
            new_keys = {
                (
                    row.market,
                    row.stock_code,
                    pd.Timestamp(row.trade_date).date().isoformat(),
                    row.frequency,
                    row.adjust,
                )
                for row in normalized_frame.itertuples(index=False)
            }
            updated_count = len(existing_keys & new_keys)
            inserted_count = len(new_keys - existing_keys)

            warehouse_result = self.market_warehouse.upsert_ohlcv(normalized_frame)
            now = datetime.now().isoformat()
            extra = json.dumps(
                {
                    "exchange": normalized_exchange,
                    "asset_type": asset_type,
                    "frequency": frequency,
                    "adjust": adjust,
                    "dataset_path": warehouse_result["dataset_path"],
                },
                ensure_ascii=False,
            )
            self.conn.execute(
                """
                INSERT INTO update_log (stock_code, market, action, new_records, updated_records, extra, update_time)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_code,
                    normalized_market,
                    "upsert_parquet_batch",
                    inserted_count,
                    updated_count,
                    extra,
                    now,
                ),
            )

            stats = {
                "new_records": inserted_count,
                "updated_records": updated_count,
                "total_records": len(normalized_frame),
                "parquet_path": warehouse_result["dataset_path"],
            }
            print(f"[OK] 数据已写入分区 Parquet (新增：{inserted_count}, 更新：{updated_count})")
            return stats
        except Exception as e:
            print(f"[ERROR] 保存 K 线数据错误：{e}")
            import traceback

            traceback.print_exc()
            return None

    def get_latest_date(self, stock_code, market="HK", exchange=None, asset_type="equity", frequency="daily", adjust="qfq"):
        try:
            normalized_market = (market or "HK").upper()
            normalized_code = normalize_stock_code(stock_code, market=normalized_market)
            normalized_exchange = (exchange or infer_exchange(normalized_code, market=normalized_market)).upper()
            return self.market_warehouse.get_latest_trade_date(
                stock_code=normalized_code,
                market=normalized_market,
                exchange=normalized_exchange,
                asset_type=asset_type,
                frequency=frequency,
                adjust=adjust,
            )
        except Exception as e:
            print(f"[ERROR] 查询最新日期错误: {e}")
            return None

    def get_kline_data(
        self,
        stock_code,
        start_date=None,
        end_date=None,
        market="HK",
        exchange=None,
        asset_type="equity",
        frequency="daily",
        adjust="qfq",
    ):
        try:
            normalized_market = (market or "HK").upper()
            normalized_code = normalize_stock_code(stock_code, market=normalized_market)
            normalized_exchange = (exchange or infer_exchange(normalized_code, market=normalized_market)).upper()
            warehouse_df = self.market_warehouse.read_ohlcv(
                stock_code=normalized_code,
                market=normalized_market,
                exchange=normalized_exchange,
                asset_type=asset_type,
                frequency=frequency,
                adjust=adjust,
                start_date=start_date,
                end_date=end_date,
            )
            if warehouse_df is None or warehouse_df.empty:
                return None

            warehouse_df = warehouse_df.copy()
            warehouse_df["trade_date"] = pd.to_datetime(warehouse_df["trade_date"])
            warehouse_df.set_index("trade_date", inplace=True)
            warehouse_df = warehouse_df[["open", "close", "high", "low", "volume"]].rename(
                columns={"open": "Open", "close": "Close", "high": "High", "low": "Low", "volume": "Volume"}
            )
            warehouse_df.index.name = "date"
            return warehouse_df
        except Exception as e:
            print(f"[ERROR] 查询 K 线数据错误: {e}")
            return None

    def get_stock_info(self, stock_code, market="HK"):
        try:
            normalized_market = (market or "HK").upper()
            normalized_code = normalize_stock_code(stock_code, market=normalized_market)
            warehouse_info = self.market_warehouse.get_stock_info(normalized_code, market=normalized_market)
            if warehouse_info:
                return warehouse_info

            result = self.conn.execute(
                """
                SELECT stock_code, market, exchange, asset_type, name, current_price, close_price, open_price,
                       high, low, volume, market_cap, pe_ratio, week_52_high, week_52_low, source, update_time
                FROM stock_info
                WHERE market = ? AND stock_code = ?
                """,
                (normalized_market, normalized_code),
            ).fetchone()
            if not result:
                return None

            columns = [
                "stock_code",
                "market",
                "exchange",
                "asset_type",
                "name",
                "current_price",
                "close_price",
                "open_price",
                "high",
                "low",
                "volume",
                "market_cap",
                "pe_ratio",
                "week_52_high",
                "week_52_low",
                "source",
                "ingest_time",
            ]
            return dict(zip(columns, result))
        except Exception as e:
            print(f"[ERROR] 查询股票信息错误: {e}")
            return None

    def get_update_log(self, stock_code=None, limit=10, market=None):
        try:
            clauses = []
            params = []
            if market:
                clauses.append("market = ?")
                params.append(market.upper())
            if stock_code:
                normalized_market = (market or "HK").upper()
                clauses.append("stock_code = ?")
                params.append(normalize_stock_code(stock_code, market=normalized_market))

            query = "SELECT stock_code, market, action, new_records, updated_records, extra, update_time FROM update_log"
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY update_time DESC LIMIT ?"
            params.append(limit)
            results = self.conn.execute(query, params).fetchall()
            return results if results else []
        except Exception as e:
            print(f"[ERROR] 查询更新日志错误: {e}")
            return []

    def get_statistics(self, stock_code, market="HK", exchange=None, asset_type="equity", frequency="daily", adjust="qfq"):
        try:
            normalized_market = (market or "HK").upper()
            normalized_code = normalize_stock_code(stock_code, market=normalized_market)
            normalized_exchange = (exchange or infer_exchange(normalized_code, market=normalized_market)).upper()
            warehouse_stats = self.market_warehouse.get_statistics(
                stock_code=normalized_code,
                market=normalized_market,
                exchange=normalized_exchange,
                asset_type=asset_type,
                frequency=frequency,
                adjust=adjust,
            )
            if warehouse_stats:
                db_size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
                warehouse_stats["db_file_size"] = f"{db_size / 1024 / 1024:.2f} MB"
                warehouse_stats["db_path"] = self.db_path
                return warehouse_stats
            return None
        except Exception as e:
            print(f"[ERROR] 获取统计信息错误: {e}")
            return None

    def export_to_json(self, stock_code, output_path="./output", market="HK"):
        try:
            os.makedirs(output_path, exist_ok=True)
            stock_info = self.get_stock_info(stock_code, market=market)
            kline_data = self.get_kline_data(stock_code, market=market)
            if kline_data is None or kline_data.empty:
                print("[ERROR] 无数据可导出")
                return None

            kline_reset = kline_data.reset_index()
            kline_reset["date"] = kline_reset["date"].dt.strftime("%Y-%m-%d")
            normalized_code = normalize_stock_code(stock_code, market=market)
            data_dict = {
                "stock_code": normalized_code,
                "market": market.upper(),
                "stock_info": stock_info,
                "record_count": len(kline_data),
                "date_range": f"{kline_reset['date'].iloc[0]} to {kline_reset['date'].iloc[-1]}",
                "export_time": datetime.now().isoformat(),
                "source": "parquet_warehouse",
                "data": kline_reset.to_dict("records"),
            }

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"stock_{market.lower()}_{normalized_code}_{timestamp}.json"
            filepath = os.path.join(output_path, filename)
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data_dict, f, ensure_ascii=False, indent=2)
            print(f"[OK] 数据已导出到：{filepath}")
            return filepath
        except Exception as e:
            print(f"[ERROR] 导出数据错误: {e}")
            return None

    def get_all_stocks(self, market=None, asset_type="equity", frequency="daily", adjust="qfq"):
        try:
            return self.market_warehouse.get_all_stock_codes(
                market=market.upper() if market else None,
                asset_type=asset_type,
                frequency=frequency,
                adjust=adjust,
            )
        except Exception as e:
            print(f"[ERROR] 获取股票列表错误：{e}")
            return []

    def save_scanned_stock(self, stock_code, name, status="active"):
        try:
            if self.conn is None:
                return False
            now = datetime.now().isoformat()
            self.conn.execute(
                """
                INSERT INTO scanned_stocks (stock_code, name, scan_time, last_update, status)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (stock_code) DO UPDATE SET
                    name = excluded.name,
                    last_update = excluded.last_update,
                    status = excluded.status
                """,
                (stock_code, name, now, now, status),
            )
            return True
        except Exception as e:
            print(f"[ERROR] 保存已扫描股票错误：{e}")
            return False

    def get_scanned_stocks(self, status_filter=None):
        try:
            if self.conn is None:
                return []
            if status_filter:
                result = self.conn.execute(
                    "SELECT stock_code, name, status FROM scanned_stocks WHERE status = ? ORDER BY stock_code",
                    (status_filter,),
                ).fetchall()
            else:
                result = self.conn.execute("SELECT stock_code, name, status FROM scanned_stocks ORDER BY stock_code").fetchall()
            return [{"code": row[0], "name": row[1], "status": row[2]} for row in result] if result else []
        except Exception as e:
            print(f"[ERROR] 获取已扫描股票列表错误：{e}")
            return []

    def is_stock_scanned(self, stock_code):
        try:
            if self.conn is None:
                return False
            result = self.conn.execute("SELECT COUNT(*) FROM scanned_stocks WHERE stock_code = ?", (stock_code,)).fetchone()[0]
            return result > 0
        except Exception as e:
            print(f"[ERROR] 检查股票扫描状态错误：{e}")
            return False

    def get_scanned_stock_count(self):
        try:
            if self.conn is None:
                return 0
            return self.conn.execute("SELECT COUNT(*) FROM scanned_stocks").fetchone()[0]
        except Exception as e:
            print(f"[ERROR] 获取已扫描股票数量错误：{e}")
            return 0

    def get_total_kline_records(self, market=None, asset_type="equity", frequency="daily", adjust="qfq"):
        try:
            return self.market_warehouse.get_total_rows(
                market=market.upper() if market else None,
                asset_type=asset_type,
                frequency=frequency,
                adjust=adjust,
            )
        except Exception as e:
            print(f"[ERROR] 获取总记录数错误：{e}")
            return 0

    def sort_database(self):
        try:
            print("[INFO] 开始整理元数据库索引...")
            stock_count = len(self.get_all_stocks())
            total_records = self.get_total_kline_records()
            self.conn.execute("VACUUM")
            stats = {"total_stocks": stock_count, "total_records": total_records, "status": "success"}
            print("[OK] 数据库整理完成")
            print(f"     总股票数：{stock_count}")
            print(f"     总记录数：{total_records}")
            return stats
        except Exception as e:
            print(f"[ERROR] 排序数据库错误：{e}")
            return {"status": "failed", "error": str(e)}

    def close(self):
        if getattr(self, "market_warehouse", None):
            self.market_warehouse.close()
            self.market_warehouse = None
        if self.conn:
            self.conn.close()
            self.conn = None
            print("[OK] 数据库连接已关闭")

    def __del__(self):
        self.close()
