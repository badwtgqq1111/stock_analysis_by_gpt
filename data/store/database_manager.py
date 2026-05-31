#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""兼容旧调用的市场数据管理器。

历史版本在这里维护 DuckDB 元数据库；现在统一透传到 MarketDataWarehouse，
元数据使用 ClickHouse 或本地 parquet registry。
"""

import json
import os
from datetime import datetime

import pandas as pd

from data.model import infer_exchange, normalize_ohlcv_frame, normalize_stock_code, normalize_stock_info
from data.store.layout import DataLayout
from data.store.warehouse import MarketDataWarehouse


class DatabaseManager:
    """旧接口适配层：不再创建 DuckDB 文件。"""

    def __init__(self, db_dir="./assets"):
        self.db_dir = db_dir
        os.makedirs(db_dir, exist_ok=True)
        self.data_layout = DataLayout(os.path.join(db_dir, "data"))
        self.market_warehouse = MarketDataWarehouse(self.data_layout)
        self._update_log = []
        self._scanned_stocks = {}

    def execute_query(self, query, params=None):
        """DuckDB SQL 已移除；保留方法用于显式暴露迁移后的限制。"""
        raise RuntimeError("DatabaseManager no longer supports ad-hoc DuckDB SQL; use MarketDataWarehouse APIs.")

    def save_stock_info(self, stock_info, stock_code, market="HK", exchange=None, asset_type="equity"):
        if not stock_info:
            return False
        try:
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
            self.market_warehouse.upsert_stock_info(normalized_info)
            return True
        except Exception as exc:
            print(f"[ERROR] 保存股票信息错误: {exc}")
            return False

    def _ensure_stock_info_exists(self, stock_code, market="HK", exchange=None, asset_type="equity"):
        normalized_market = (market or "HK").upper()
        normalized_code = normalize_stock_code(stock_code, market=normalized_market)
        existing = self.market_warehouse.get_stock_info(normalized_code, market=normalized_market)
        if existing:
            return True
        normalized_exchange = (exchange or infer_exchange(normalized_code, market=normalized_market)).upper()
        info = normalize_stock_info(
            {"name": normalized_code, "source": "placeholder"},
            stock_code=normalized_code,
            market=normalized_market,
            exchange=normalized_exchange,
            asset_type=asset_type,
            source="placeholder",
        )
        self.market_warehouse.upsert_stock_info(info)
        return True

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
            self._append_update_log(
                normalized_code,
                normalized_market,
                "upsert_parquet_batch",
                inserted_count,
                updated_count,
                {
                    "exchange": normalized_exchange,
                    "asset_type": asset_type,
                    "frequency": frequency,
                    "adjust": adjust,
                    "dataset_path": warehouse_result["dataset_path"],
                },
            )

            stats = {
                "new_records": inserted_count,
                "updated_records": updated_count,
                "total_records": len(normalized_frame),
                "parquet_path": warehouse_result["dataset_path"],
            }
            print(f"[OK] 数据已写入分区 Parquet (新增：{inserted_count}, 更新：{updated_count})")
            return stats
        except Exception as exc:
            print(f"[ERROR] 保存 K 线数据错误：{exc}")
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
        except Exception as exc:
            print(f"[ERROR] 查询最新日期错误: {exc}")
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
        except Exception as exc:
            print(f"[ERROR] 查询 K 线数据错误: {exc}")
            return None

    def get_stock_info(self, stock_code, market="HK"):
        try:
            normalized_market = (market or "HK").upper()
            normalized_code = normalize_stock_code(stock_code, market=normalized_market)
            return self.market_warehouse.get_stock_info(normalized_code, market=normalized_market)
        except Exception as exc:
            print(f"[ERROR] 查询股票信息错误: {exc}")
            return None

    def get_update_log(self, stock_code=None, limit=10, market=None):
        normalized_market = market.upper() if market else None
        normalized_code = normalize_stock_code(stock_code, market=normalized_market or "HK") if stock_code else None
        rows = [
            row for row in self._update_log
            if (normalized_market is None or row[1] == normalized_market)
            and (normalized_code is None or row[0] == normalized_code)
        ]
        return rows[: int(limit or 10)]

    def get_statistics(self, stock_code, market="HK", exchange=None, asset_type="equity", frequency="daily", adjust="qfq"):
        try:
            normalized_market = (market or "HK").upper()
            normalized_code = normalize_stock_code(stock_code, market=normalized_market)
            normalized_exchange = (exchange or infer_exchange(normalized_code, market=normalized_market)).upper()
            return self.market_warehouse.get_statistics(
                stock_code=normalized_code,
                market=normalized_market,
                exchange=normalized_exchange,
                asset_type=asset_type,
                frequency=frequency,
                adjust=adjust,
            )
        except Exception as exc:
            print(f"[ERROR] 获取统计信息错误: {exc}")
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
            with open(filepath, "w", encoding="utf-8") as handle:
                json.dump(data_dict, handle, ensure_ascii=False, indent=2)
            print(f"[OK] 数据已导出到：{filepath}")
            return filepath
        except Exception as exc:
            print(f"[ERROR] 导出数据错误: {exc}")
            return None

    def get_all_stocks(self, market=None, asset_type="equity", frequency="daily", adjust="qfq"):
        try:
            return self.market_warehouse.get_all_stock_codes(
                market=market.upper() if market else None,
                asset_type=asset_type,
                frequency=frequency,
                adjust=adjust,
            )
        except Exception as exc:
            print(f"[ERROR] 获取股票列表错误：{exc}")
            return []

    def save_scanned_stock(self, stock_code, name, status="active"):
        normalized_code = normalize_stock_code(stock_code, market="HK")
        now = datetime.now().isoformat()
        self._scanned_stocks[normalized_code] = {
            "stock_code": normalized_code,
            "name": name,
            "scan_time": self._scanned_stocks.get(normalized_code, {}).get("scan_time", now),
            "last_update": now,
            "status": status,
        }
        return True

    def get_scanned_stocks(self, status=None):
        rows = list(self._scanned_stocks.values())
        if status:
            rows = [row for row in rows if row.get("status") == status]
        return rows

    def close(self):
        self.market_warehouse.close()

    def _append_update_log(self, stock_code, market, action, new_records, updated_records, extra):
        self._update_log.insert(
            0,
            (
                stock_code,
                market,
                action,
                int(new_records or 0),
                int(updated_records or 0),
                json.dumps(extra or {}, ensure_ascii=False),
                datetime.now().isoformat(),
            ),
        )
