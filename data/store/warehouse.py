#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Parquet/ClickHouse 主存储与本地元数据查询层。"""

from pathlib import Path

import pandas as pd

from data.model import (
    CLEAN_OHLCV_COLUMNS,
    CORPORATE_ACTION_FIELDS,
    FEATURE_COLUMNS,
    SIGNAL_COLUMNS,
    STOCK_INFO_FIELDS,
    TRADE_COLUMNS,
)
import os

from data.store.parquet_store import ParquetDataStore


class MarketDataWarehouse:
    """管理 clean 层市场数据与元数据。"""

    OHLCV_DATASET = "ohlcv"
    CORPORATE_ACTIONS_DATASET = "corporate_actions"
    FEATURES_DATASET = "features"
    SIGNALS_DATASET = "signals"
    TRADES_DATASET = "trades"
    CORPORATE_ACTIONS_PARTITION_COLUMNS = ("market", "exchange", "asset_type", "action_type", "year")
    FEATURES_PARTITION_COLUMNS = (
        "market",
        "exchange",
        "asset_type",
        "frequency",
        "adjust",
        "feature_set",
        "feature_version",
        "feature_config_hash",
        "year",
    )
    SIGNALS_PARTITION_COLUMNS = ("market", "exchange", "asset_type", "frequency", "adjust", "signal_set", "year")
    TRADES_PARTITION_COLUMNS = ("market", "exchange", "asset_type", "frequency", "adjust", "account_id", "year")

    def __init__(self, layout, read_only=False, clickhouse_store=None):
        self.layout = layout
        self.read_only = bool(read_only)
        self.parquet_store = ParquetDataStore(layout)

        if clickhouse_store is not None:
            self.clickhouse_store = clickhouse_store
        elif os.environ.get("CLICKHOUSE_HOST"):
            from data.store.clickhouse_store import ClickHouseStore

            self.clickhouse_store = ClickHouseStore(
                host=os.environ.get("CLICKHOUSE_HOST", "localhost"),
                port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
                user=os.environ.get("CLICKHOUSE_USER", "default"),
                password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
                database=os.environ.get("CLICKHOUSE_DATABASE", "quant"),
                layout=layout,
            )
        else:
            self.clickhouse_store = None

        self.stock_info_dataset = "stock_info_registry"
        self._clickhouse_disabled_reason = None

    @property
    def _feature_store(self):
        """返回 features 数据集的后端存储，优先使用 ClickHouse。"""
        if self.clickhouse_store is not None and self._clickhouse_disabled_reason is None:
            return self.clickhouse_store
        return self.parquet_store

    def _feature_store_candidates(self):
        """返回 features 可用后端，ClickHouse 不可用时允许降级到 parquet。"""
        stores = []
        if self.clickhouse_store is not None and self._clickhouse_disabled_reason is None:
            stores.append(self.clickhouse_store)
        stores.append(self.parquet_store)
        return stores

    def _ensure_writable(self):
        if self.read_only:
            raise RuntimeError(f"只读仓库不支持写入: {self.layout.base_path}")

    @property
    def _stock_info_store(self):
        """返回 stock_info registry 后端，优先使用 ClickHouse。"""
        return self.clickhouse_store or self.parquet_store

    def _stock_info_store_candidates(self):
        stores = []
        if self.clickhouse_store is not None and self._clickhouse_disabled_reason is None:
            stores.append(self.clickhouse_store)
        stores.append(self.parquet_store)
        return stores

    def _read_stock_info_registry(self, filters=None, columns=None, order_by=None):
        frame = pd.DataFrame()
        stores = []
        if self.clickhouse_store is not None and self._clickhouse_disabled_reason is None:
            stores.append(self.clickhouse_store)
        stores.append(self.parquet_store)
        for store in stores:
            try:
                frame = store.read_frame(
                    self.stock_info_dataset,
                    layer="meta",
                    filters=filters,
                    columns=columns,
                    order_by=order_by,
                )
            except Exception as exc:
                if store is self.clickhouse_store:
                    self._clickhouse_disabled_reason = str(exc)
                frame = pd.DataFrame(columns=columns or STOCK_INFO_FIELDS)
            if frame is not None and not frame.empty:
                break
        if frame is None or frame.empty:
            return pd.DataFrame(columns=columns or STOCK_INFO_FIELDS)
        for column in STOCK_INFO_FIELDS:
            if column not in frame.columns:
                frame[column] = None
        return frame[[column for column in STOCK_INFO_FIELDS if column in frame.columns]].copy()

    def _preserve_existing_stock_info_fields(self, payload, fields=None):
        """Fill missing metadata from the existing registry row before replace-upsert."""
        if payload is None or payload.empty:
            return payload

        preserve_fields = tuple(fields or (
            "industry_l1",
            "industry_l2",
            "industry_l3",
            "theme_tags",
            "industry_source",
            "industry_updated_at",
            "instrument_type",
            "is_fund_like",
            "tradable_flag",
            "instrument_source",
            "instrument_updated_at",
        ))
        available_fields = [field for field in preserve_fields if field in payload.columns]
        if not available_fields:
            return payload

        keys = payload[["market", "stock_code"]].dropna().drop_duplicates()
        if keys.empty:
            return payload

        existing = self._read_stock_info_registry(
            filters={
                "market": keys["market"].astype(str).unique().tolist(),
                "stock_code": keys["stock_code"].astype(str).unique().tolist(),
            },
            columns=["market", "stock_code", *available_fields],
        )
        if existing.empty:
            return payload

        merged = payload.merge(
            existing,
            on=["market", "stock_code"],
            how="left",
            suffixes=("", "_existing"),
        )
        # Convert all preserve fields to object dtype upfront to prevent
        # LossySetitemError when assigning across incompatible dtypes (e.g.
        # bool column receiving string "[]" from existing registry data).
        for field in available_fields:
            existing_field = f"{field}_existing"
            if existing_field not in merged.columns:
                continue
            merged[field] = merged[field].astype(object)
            merged[existing_field] = merged[existing_field].astype(object)

        for field in available_fields:
            existing_field = f"{field}_existing"
            if existing_field not in merged.columns:
                continue
            missing_mask = merged[field].isna()
            missing_mask = missing_mask | (merged[field].astype(str).str.strip() == "")
            merged.loc[missing_mask, field] = merged.loc[missing_mask, existing_field]
            merged.drop(columns=[existing_field], inplace=True)
        return merged[payload.columns].copy()

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

    def upsert_corporate_actions(self, frame, dataset_name=CORPORATE_ACTIONS_DATASET):
        """将标准企业行为数据 upsert 到分区 parquet 数据集。"""
        if frame is None or frame.empty:
            return {"rows": 0, "dataset_path": str(self.layout.dataset_path(dataset_name, layer="clean"))}

        payload = frame[CORPORATE_ACTION_FIELDS].copy()
        target = self.parquet_store.upsert_frame(
            dataset_name=dataset_name,
            frame=payload,
            dedupe_keys=["market", "stock_code", "event_date", "action_type"],
            layer="clean",
            sort_by=["market", "stock_code", "event_date", "action_type", "ingest_time"],
            date_column="event_date",
            partition_columns=self.CORPORATE_ACTIONS_PARTITION_COLUMNS,
        )
        return {"rows": len(payload), "dataset_path": str(target)}

    def read_corporate_actions(
        self,
        stock_code=None,
        market=None,
        exchange=None,
        asset_type=None,
        action_type=None,
        start_date=None,
        end_date=None,
        dataset_name=CORPORATE_ACTIONS_DATASET,
    ):
        """按条件读取 clean 层企业行为数据。"""
        filters = {
            "stock_code": stock_code,
            "market": market,
            "exchange": exchange,
            "asset_type": asset_type,
            "action_type": action_type,
        }
        frame = self.parquet_store.read_frame(
            dataset_name=dataset_name,
            layer="clean",
            filters=filters,
            order_by="market, stock_code, event_date, action_type",
        )
        if frame.empty:
            return frame

        for column in ["event_date", "announcement_date", "ex_date", "record_date", "payment_date"]:
            if column in frame.columns:
                frame[column] = pd.to_datetime(frame[column], errors="coerce")
        if start_date:
            frame = frame.loc[frame["event_date"] >= pd.to_datetime(start_date)]
        if end_date:
            frame = frame.loc[frame["event_date"] <= pd.to_datetime(end_date)]
        frame.reset_index(drop=True, inplace=True)
        return frame

    def upsert_features(self, frame, dataset_name=FEATURES_DATASET):
        """将标准特征数据 upsert 到 feature 层 parquet 数据集。"""
        self._ensure_writable()
        if frame is None or frame.empty:
            return {"rows": 0, "dataset_path": str(self.layout.dataset_path(dataset_name, layer="feature"))}

        payload = frame[FEATURE_COLUMNS].copy()
        last_error = None
        for store in self._feature_store_candidates():
            try:
                target = store.upsert_frame(
                    dataset_name=dataset_name,
                    frame=payload,
                    dedupe_keys=[
                        "market",
                        "stock_code",
                        "trade_date",
                        "frequency",
                        "adjust",
                        "feature_set",
                        "feature_version",
                        "feature_config_hash",
                        "feature_name",
                    ],
                    layer="feature",
                    sort_by=[
                        "market",
                        "stock_code",
                        "trade_date",
                        "frequency",
                        "adjust",
                        "feature_set",
                        "feature_version",
                        "feature_config_hash",
                        "feature_name",
                        "ingest_time",
                    ],
                    date_column="trade_date",
                    partition_columns=self.FEATURES_PARTITION_COLUMNS,
                )
                return {"rows": len(payload), "dataset_path": str(target)}
            except Exception as exc:
                last_error = exc
                if store is self.clickhouse_store:
                    self._clickhouse_disabled_reason = str(exc)
                if store is self.parquet_store:
                    raise
                continue
        if last_error is not None:
            raise last_error
        return {"rows": len(payload), "dataset_path": str(target)}

    def append_features(self, frame, dataset_name=FEATURES_DATASET):
        """Append feature batches without reading and rewriting the full dataset."""
        self._ensure_writable()
        if frame is None or frame.empty:
            return {"rows": 0, "dataset_path": str(self.layout.dataset_path(dataset_name, layer="feature"))}

        payload = frame[FEATURE_COLUMNS].copy()
        last_error = None
        for store in self._feature_store_candidates():
            try:
                target = store.append_frame(
                    dataset_name=dataset_name,
                    frame=payload,
                    layer="feature",
                    date_column="trade_date",
                    partition_columns=self.FEATURES_PARTITION_COLUMNS,
                )
                return {"rows": len(payload), "dataset_path": str(target)}
            except Exception as exc:
                last_error = exc
                if store is self.clickhouse_store:
                    self._clickhouse_disabled_reason = str(exc)
                if store is self.parquet_store:
                    raise
                continue
        if last_error is not None:
            raise last_error
        return {"rows": len(payload), "dataset_path": str(target)}

    def read_features(
        self,
        stock_code=None,
        market=None,
        exchange=None,
        asset_type=None,
        frequency=None,
        adjust=None,
        feature_set=None,
        feature_version=None,
        feature_config_hash=None,
        feature_name=None,
        start_date=None,
        end_date=None,
        dataset_name=FEATURES_DATASET,
    ):
        """按条件读取 feature 层数据。"""
        filters = {
            "stock_code": stock_code,
            "market": market,
            "exchange": exchange,
            "asset_type": asset_type,
            "frequency": frequency,
            "adjust": adjust,
            "feature_set": feature_set,
            "feature_version": feature_version,
            "feature_config_hash": feature_config_hash,
            "feature_name": feature_name,
        }
        frame = pd.DataFrame()
        for store in self._feature_store_candidates():
            try:
                frame = store.read_frame(
                    dataset_name=dataset_name,
                    layer="feature",
                    filters=filters,
                    order_by=(
                        "market, stock_code, trade_date, feature_set, "
                        "feature_version, feature_config_hash, feature_name"
                    ),
                )
            except Exception as exc:
                if store is self.clickhouse_store:
                    self._clickhouse_disabled_reason = str(exc)
                if feature_version is not None or feature_config_hash is not None:
                    frame = pd.DataFrame(columns=FEATURE_COLUMNS)
                else:
                    try:
                        frame = store.read_frame(
                            dataset_name=dataset_name,
                            layer="feature",
                            filters={
                                "stock_code": stock_code,
                                "market": market,
                                "exchange": exchange,
                                "asset_type": asset_type,
                                "frequency": frequency,
                                "adjust": adjust,
                                "feature_set": feature_set,
                                "feature_name": feature_name,
                            },
                            order_by="market, stock_code, trade_date, feature_set, feature_name",
                        )
                    except Exception:
                        frame = pd.DataFrame(columns=FEATURE_COLUMNS)
            if frame is not None and not frame.empty:
                break
        if frame.empty:
            return frame

        if "feature_version" not in frame.columns:
            frame["feature_version"] = "0.1.0"
        if "feature_config_hash" not in frame.columns:
            frame["feature_config_hash"] = "legacy"
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        if start_date:
            frame = frame.loc[frame["trade_date"] >= pd.to_datetime(start_date)]
        if end_date:
            frame = frame.loc[frame["trade_date"] <= pd.to_datetime(end_date)]
        frame.reset_index(drop=True, inplace=True)
        return frame

    def upsert_signals(self, frame, dataset_name=SIGNALS_DATASET):
        """将标准信号数据 upsert 到 signal 层 parquet 数据集。"""
        if frame is None or frame.empty:
            return {"rows": 0, "dataset_path": str(self.layout.dataset_path(dataset_name, layer="signal"))}

        payload = frame[SIGNAL_COLUMNS].copy()
        target = self.parquet_store.upsert_frame(
            dataset_name=dataset_name,
            frame=payload,
            dedupe_keys=["market", "stock_code", "trade_date", "frequency", "adjust", "signal_set", "signal_type"],
            layer="signal",
            sort_by=[
                "market",
                "stock_code",
                "trade_date",
                "frequency",
                "adjust",
                "signal_set",
                "signal_type",
                "ingest_time",
            ],
            date_column="trade_date",
            partition_columns=self.SIGNALS_PARTITION_COLUMNS,
        )
        return {"rows": len(payload), "dataset_path": str(target)}

    def read_signals(
        self,
        stock_code=None,
        market=None,
        exchange=None,
        asset_type=None,
        frequency=None,
        adjust=None,
        signal_set=None,
        signal_type=None,
        batch_id=None,
        strategy_name=None,
        start_date=None,
        end_date=None,
        dataset_name=SIGNALS_DATASET,
    ):
        """按条件读取 signal 层数据。"""
        filters = {
            "stock_code": stock_code,
            "market": market,
            "exchange": exchange,
            "asset_type": asset_type,
            "frequency": frequency,
            "adjust": adjust,
            "signal_set": signal_set,
            "signal_type": signal_type,
            "batch_id": batch_id,
            "strategy_name": strategy_name,
        }
        frame = self.parquet_store.read_frame(
            dataset_name=dataset_name,
            layer="signal",
            filters=filters,
            order_by="market, stock_code, trade_date, signal_set, signal_type",
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

    def upsert_trades(self, frame, dataset_name=TRADES_DATASET):
        """将标准成交数据 upsert 到 trade 层 parquet 数据集。"""
        if frame is None or frame.empty:
            return {"rows": 0, "dataset_path": str(self.layout.dataset_path(dataset_name, layer="trade"))}

        payload = frame[TRADE_COLUMNS].copy()
        target = self.parquet_store.upsert_frame(
            dataset_name=dataset_name,
            frame=payload,
            dedupe_keys=["market", "stock_code", "trade_date", "account_id", "order_id"],
            layer="trade",
            sort_by=[
                "market",
                "stock_code",
                "trade_date",
                "account_id",
                "order_id",
                "ingest_time",
            ],
            date_column="trade_date",
            partition_columns=self.TRADES_PARTITION_COLUMNS,
        )
        return {"rows": len(payload), "dataset_path": str(target)}

    def read_trades(
        self,
        stock_code=None,
        market=None,
        exchange=None,
        asset_type=None,
        frequency=None,
        adjust=None,
        account_id=None,
        strategy_name=None,
        order_id=None,
        trade_type=None,
        start_date=None,
        end_date=None,
        dataset_name=TRADES_DATASET,
    ):
        """按条件读取 trade 层数据。"""
        filters = {
            "stock_code": stock_code,
            "market": market,
            "exchange": exchange,
            "asset_type": asset_type,
            "frequency": frequency,
            "adjust": adjust,
            "account_id": account_id,
            "strategy_name": strategy_name,
            "order_id": order_id,
            "trade_type": trade_type,
        }
        frame = self.parquet_store.read_frame(
            dataset_name=dataset_name,
            layer="trade",
            filters=filters,
            order_by="market, stock_code, trade_date, account_id, order_id",
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

    def upsert_stock_info(self, info):
        """保存标准化后的股票信息。"""
        self._ensure_writable()
        payload = pd.DataFrame([info], columns=STOCK_INFO_FIELDS)
        payload = self._preserve_existing_stock_info_fields(payload)
        self._upsert_stock_info_payload(payload)
        return {"rows": 1}

    def upsert_stock_info_batch(self, info_list):
        """批量保存标准化后的股票信息。"""
        self._ensure_writable()
        if not info_list:
            return {"rows": 0}

        payload = pd.DataFrame(info_list, columns=STOCK_INFO_FIELDS)
        payload.drop_duplicates(subset=["market", "stock_code"], keep="last", inplace=True)
        payload = self._preserve_existing_stock_info_fields(payload)
        self._upsert_stock_info_payload(payload)
        return {"rows": len(payload)}

    def _upsert_stock_info_payload(self, payload):
        last_error = None
        for store in self._stock_info_store_candidates():
            try:
                store.upsert_frame(
                    dataset_name=self.stock_info_dataset,
                    frame=payload,
                    dedupe_keys=["market", "stock_code"],
                    layer="meta",
                    sort_by=["market", "stock_code", "ingest_time"],
                    date_column="ingest_time",
                    partition_columns=("market",),
                )
                return
            except Exception as exc:
                last_error = exc
                if store is self.parquet_store:
                    raise
                continue
        if last_error is not None:
            raise last_error

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
        year_filter = None
        range_filters = {}
        start_ts = pd.to_datetime(start_date) if start_date else None
        end_ts = pd.to_datetime(end_date) if end_date else None
        if start_ts is not None or end_ts is not None:
            start_year = int(start_ts.year) if start_ts is not None else None
            end_year = int(end_ts.year) if end_ts is not None else None
            if start_year is not None and end_year is not None and end_year >= start_year:
                year_filter = list(range(start_year, end_year + 1))
            elif start_year is not None:
                year_filter = [start_year]
            elif end_year is not None:
                year_filter = [end_year]
            range_filters["trade_date"] = {
                "gte": start_ts.strftime("%Y-%m-%d") if start_ts is not None else None,
                "lte": end_ts.strftime("%Y-%m-%d") if end_ts is not None else None,
            }
        filters = {
            "stock_code": stock_code,
            "market": market,
            "exchange": exchange,
            "asset_type": asset_type,
            "frequency": frequency,
            "adjust": adjust,
            "year": year_filter,
        }
        frame = self.parquet_store.read_frame(
            dataset_name=dataset_name,
            layer="clean",
            filters=filters,
            order_by="market, stock_code, trade_date",
            range_filters=range_filters,
        )
        if frame.empty:
            return frame

        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        if start_ts is not None:
            frame = frame.loc[frame["trade_date"] >= start_ts]
        if end_ts is not None:
            frame = frame.loc[frame["trade_date"] <= end_ts]
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
        filters = {"stock_code": stock_code}
        if market:
            filters["market"] = market
        frame = self._read_stock_info_registry(
            filters=filters,
            columns=STOCK_INFO_FIELDS,
            order_by="ingest_time DESC",
        )
        if frame.empty:
            return None
        row = frame.iloc[0]
        return {field: row.get(field) for field in STOCK_INFO_FIELDS}

    def read_stock_info(self, stock_codes=None, market=None, columns=None, order_by=None):
        """批量读取 stock info registry。"""
        filters = {}
        if stock_codes:
            filters["stock_code"] = list(dict.fromkeys(stock_codes))
        if market:
            filters["market"] = market
        return self._read_stock_info_registry(
            filters=filters,
            columns=columns or STOCK_INFO_FIELDS,
            order_by=order_by or "market, stock_code",
        )

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

    def get_latest_trade_dates(
        self,
        stock_codes=None,
        market=None,
        exchange=None,
        asset_type=None,
        frequencies=None,
        adjust=None,
        dataset_name=OHLCV_DATASET,
    ):
        """批量获取证券最新交易日，避免全市场同步前逐只扫描 Parquet。"""
        filters = {
            "stock_code": list(dict.fromkeys(stock_codes)) if stock_codes else None,
            "market": market,
            "exchange": exchange,
            "asset_type": asset_type,
            "frequency": list(dict.fromkeys(frequencies)) if frequencies else None,
            "adjust": adjust,
        }
        columns = ["stock_code", "market", "frequency", "adjust", "trade_date"]
        frame = self.parquet_store.read_frame(
            dataset_name=dataset_name,
            layer="clean",
            filters=filters,
            columns=columns,
        )
        if frame is None or frame.empty:
            return {}
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        frame.dropna(subset=["stock_code", "frequency", "trade_date"], inplace=True)
        if frame.empty:
            return {}
        grouped = (
            frame.groupby(["stock_code", "frequency"], dropna=False)["trade_date"]
            .max()
            .reset_index()
        )
        latest = {}
        for row in grouped.itertuples(index=False):
            latest[(str(row.stock_code), str(row.frequency))] = row.trade_date
        return latest

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

    def compute_rps_features(self, factor_set="qlib_alpha158",
                             windows=(5, 10, 20, 30, 60)):
        """基于已有 ROC 因子计算横截面 RPS 排名并写入 feature 层。"""
        return self._feature_store.compute_rps_features(
            factor_set=factor_set, windows=windows,
        )

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
        return None
