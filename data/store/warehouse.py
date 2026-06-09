#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Parquet/ClickHouse 主存储与本地元数据查询层。"""

from pathlib import Path

import pandas as pd

from data.model import (
    ATTENTION_SIGNAL_FIELDS,
    CLEAN_OHLCV_COLUMNS,
    COMPANY_RESEARCH_EVIDENCE_FIELDS,
    CORPORATE_ACTION_FIELDS,
    ENTITY_ALIAS_FIELDS,
    FEATURE_COLUMNS,
    SIGNAL_COLUMNS,
    STOCK_DEEP_TAG_FIELDS,
    STOCK_GRAPH_EDGE_FIELDS,
    STOCK_GRAPH_NODE_FIELDS,
    STOCK_TAG_CANDIDATE_FIELDS,
    STOCK_TAG_FIELDS,
    STOCK_INFO_FIELDS,
    STOCK_PROFILE_FIELDS,
    TAG_DICTIONARY_FIELDS,
    THEME_OPPORTUNITY_SCORE_FIELDS,
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
    TAG_DICTIONARY_DATASET = "tag_dictionary"
    STOCK_TAG_DATASET = "stock_tag_registry"
    STOCK_TAG_CANDIDATE_DATASET = "stock_tag_candidate"
    COMPANY_RESEARCH_EVIDENCE_DATASET = "company_research_evidence"
    ENTITY_ALIAS_DATASET = "entity_alias_registry"
    STOCK_PROFILE_DATASET = "stock_profile"
    STOCK_DEEP_TAG_DATASET = "stock_deep_tag_registry"
    STOCK_GRAPH_NODE_DATASET = "stock_graph_nodes"
    STOCK_GRAPH_EDGE_DATASET = "stock_graph_edges"
    ATTENTION_SIGNAL_DATASET = "attention_signal"
    THEME_OPPORTUNITY_SCORE_DATASET = "theme_opportunity_score"
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
        self._clickhouse_disabled_reason = None

        if clickhouse_store is not None:
            self.clickhouse_store = clickhouse_store
        elif os.environ.get("CLICKHOUSE_HOST"):
            host = os.environ.get("CLICKHOUSE_HOST", "localhost")
            port = int(os.environ.get("CLICKHOUSE_PORT", "8123"))
            endpoint_error = self._check_clickhouse_endpoint(host, port)
            if endpoint_error:
                self.clickhouse_store = None
                self._clickhouse_disabled_reason = endpoint_error
            else:
                from data.store.clickhouse_store import ClickHouseStore

                self.clickhouse_store = ClickHouseStore(
                    host=host,
                    port=port,
                    user=os.environ.get("CLICKHOUSE_USER", "default"),
                    password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
                    database=os.environ.get("CLICKHOUSE_DATABASE", "quant"),
                    layout=layout,
                )
        else:
            self.clickhouse_store = None

        self.stock_info_dataset = "stock_info_registry"

    @staticmethod
    def _check_clickhouse_endpoint(host, port):
        """Return an error string when the configured ClickHouse endpoint is unreachable."""
        import socket

        timeout = float(os.environ.get("CLICKHOUSE_CONNECT_TIMEOUT", "0.5"))
        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                return None
        except OSError as exc:
            return str(exc)

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

    def _clean_store_candidates(self):
        """返回 clean 层数据可用后端，ClickHouse 优先，Parquet 兜底。"""
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

        chunk_rows = max(1, int(os.environ.get("STOCK_INFO_LOOKUP_CHUNK_ROWS", "100")))
        existing_frames = []
        key_records = keys.astype(str).to_dict("records")
        for start in range(0, len(key_records), chunk_rows):
            chunk = key_records[start:start + chunk_rows]
            existing_chunk = self._read_stock_info_registry(
                filters={
                    "market": list(dict.fromkeys(record["market"] for record in chunk)),
                    "stock_code": list(dict.fromkeys(record["stock_code"] for record in chunk)),
                },
                columns=["market", "stock_code", *available_fields],
            )
            if existing_chunk is not None and not existing_chunk.empty:
                existing_frames.append(existing_chunk)
        existing = pd.concat(existing_frames, ignore_index=True) if existing_frames else pd.DataFrame()
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
        """将标准 OHLCV 数据 upsert 到 clean 层，ClickHouse 优先。"""
        if frame is None or frame.empty:
            return {"rows": 0, "dataset_path": str(self.layout.dataset_path(dataset_name, layer="clean"))}

        payload = frame[CLEAN_OHLCV_COLUMNS].copy()
        last_error = None
        for store in self._clean_store_candidates():
            try:
                target = store.upsert_frame(
                    dataset_name=dataset_name,
                    frame=payload,
                    dedupe_keys=["market", "stock_code", "trade_date", "frequency", "adjust"],
                    layer="clean",
                    sort_by=["market", "stock_code", "trade_date", "frequency", "adjust", "ingest_time"],
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

    def append_ohlcv(self, frame, dataset_name=OHLCV_DATASET):
        """批量追加 OHLCV 到 clean 层，不做单次去重，ClickHouse 优先。"""
        if frame is None or frame.empty:
            return {"rows": 0, "dataset_path": str(self.layout.dataset_path(dataset_name, layer="clean"))}

        payload = frame[CLEAN_OHLCV_COLUMNS].copy()
        last_error = None
        for store in self._clean_store_candidates():
            try:
                target = store.append_frame(
                    dataset_name=dataset_name,
                    frame=payload,
                    layer="clean",
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
        columns=None,
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
        range_filters = {}
        if start_date or end_date:
            range_filters["trade_date"] = {
                "gte": start_date,
                "lte": end_date,
            }
        frame = pd.DataFrame()
        for store in self._feature_store_candidates():
            try:
                frame = store.read_frame(
                    dataset_name=dataset_name,
                    layer="feature",
                    filters=filters,
                    range_filters=range_filters or None,
                    columns=columns,
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
                            range_filters=range_filters or None,
                            columns=columns,
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

    def _meta_store_candidates(self):
        stores = []
        if self.clickhouse_store is not None and self._clickhouse_disabled_reason is None:
            stores.append(self.clickhouse_store)
        stores.append(self.parquet_store)
        return stores

    def _upsert_meta_frame(
        self,
        dataset_name,
        frame,
        dedupe_keys,
        sort_by,
        date_column,
        partition_columns=("market",),
    ):
        last_error = None
        for store in self._meta_store_candidates():
            try:
                return store.upsert_frame(
                    dataset_name=dataset_name,
                    frame=frame,
                    dedupe_keys=dedupe_keys,
                    layer="meta",
                    sort_by=sort_by,
                    date_column=date_column,
                    partition_columns=partition_columns,
                )
            except Exception as exc:
                last_error = exc
                if store is self.clickhouse_store:
                    self._clickhouse_disabled_reason = str(exc)
                if store is self.parquet_store:
                    raise
                continue
        if last_error is not None:
            raise last_error
        return self.layout.dataset_path(dataset_name, layer="meta")

    def _write_meta_frame(
        self,
        dataset_name,
        frame,
        date_column,
        partition_columns=("market",),
    ):
        last_error = None
        for store in self._meta_store_candidates():
            try:
                return store.write_frame(
                    dataset_name=dataset_name,
                    frame=frame,
                    layer="meta",
                    date_column=date_column,
                    partition_columns=partition_columns,
                )
            except Exception as exc:
                last_error = exc
                if store is self.clickhouse_store:
                    self._clickhouse_disabled_reason = str(exc)
                if store is self.parquet_store:
                    raise
                continue
        if last_error is not None:
            raise last_error
        return self.layout.dataset_path(dataset_name, layer="meta")

    def _read_meta_frame(self, dataset_name, filters=None, columns=None, order_by=None):
        frame = pd.DataFrame()
        for store in self._meta_store_candidates():
            try:
                frame = store.read_frame(
                    dataset_name,
                    layer="meta",
                    filters=filters,
                    columns=columns,
                    order_by=order_by,
                )
            except Exception as exc:
                if store is self.clickhouse_store:
                    self._clickhouse_disabled_reason = str(exc)
                frame = pd.DataFrame(columns=columns)
            if frame is not None and not frame.empty:
                break
        return frame if frame is not None else pd.DataFrame(columns=columns)

    def upsert_tag_dictionary(self, frame, dataset_name=TAG_DICTIONARY_DATASET):
        self._ensure_writable()
        if frame is None or frame.empty:
            return {"rows": 0, "dataset_path": str(self.layout.dataset_path(dataset_name, layer="meta"))}
        payload = frame[TAG_DICTIONARY_FIELDS].copy()
        target = self._upsert_meta_frame(
            dataset_name=dataset_name,
            frame=payload,
            dedupe_keys=["tag_type", "tag"],
            sort_by=["tag_type", "tag", "updated_at"],
            date_column="updated_at",
            partition_columns=("tag_type",),
        )
        return {"rows": len(payload), "dataset_path": str(target)}

    def replace_tag_dictionary(self, frame, dataset_name=TAG_DICTIONARY_DATASET):
        self._ensure_writable()
        payload = (
            frame[TAG_DICTIONARY_FIELDS].copy()
            if frame is not None and not frame.empty
            else pd.DataFrame(columns=TAG_DICTIONARY_FIELDS)
        )
        target = self._write_meta_frame(
            dataset_name=dataset_name,
            frame=payload,
            date_column="updated_at",
            partition_columns=("tag_type",),
        )
        return {"rows": len(payload), "dataset_path": str(target)}

    def upsert_stock_tags(self, frame, dataset_name=STOCK_TAG_DATASET):
        self._ensure_writable()
        if frame is None or frame.empty:
            return {"rows": 0, "dataset_path": str(self.layout.dataset_path(dataset_name, layer="meta"))}
        payload = frame[STOCK_TAG_FIELDS].copy()
        target = self._upsert_meta_frame(
            dataset_name=dataset_name,
            frame=payload,
            dedupe_keys=["market", "stock_code", "tag_type", "tag"],
            sort_by=["market", "stock_code", "tag_type", "tag", "updated_at"],
            date_column="updated_at",
            partition_columns=("market",),
        )
        return {"rows": len(payload), "dataset_path": str(target)}

    def replace_stock_tags(self, frame, dataset_name=STOCK_TAG_DATASET):
        self._ensure_writable()
        payload = (
            frame[STOCK_TAG_FIELDS].copy()
            if frame is not None and not frame.empty
            else pd.DataFrame(columns=STOCK_TAG_FIELDS)
        )
        target = self._write_meta_frame(
            dataset_name=dataset_name,
            frame=payload,
            date_column="updated_at",
            partition_columns=("market",),
        )
        return {"rows": len(payload), "dataset_path": str(target)}

    def upsert_stock_tag_candidates(self, frame, dataset_name=STOCK_TAG_CANDIDATE_DATASET):
        self._ensure_writable()
        if frame is None or frame.empty:
            return {"rows": 0, "dataset_path": str(self.layout.dataset_path(dataset_name, layer="meta"))}
        payload = frame[STOCK_TAG_CANDIDATE_FIELDS].copy()
        target = self._upsert_meta_frame(
            dataset_name=dataset_name,
            frame=payload,
            dedupe_keys=["market", "stock_code", "tag_type", "tag"],
            sort_by=["market", "stock_code", "tag_type", "tag", "updated_at"],
            date_column="updated_at",
            partition_columns=("market",),
        )
        return {"rows": len(payload), "dataset_path": str(target)}

    def replace_stock_tag_candidates(self, frame, dataset_name=STOCK_TAG_CANDIDATE_DATASET):
        self._ensure_writable()
        payload = (
            frame[STOCK_TAG_CANDIDATE_FIELDS].copy()
            if frame is not None and not frame.empty
            else pd.DataFrame(columns=STOCK_TAG_CANDIDATE_FIELDS)
        )
        target = self._write_meta_frame(
            dataset_name=dataset_name,
            frame=payload,
            date_column="updated_at",
            partition_columns=("market",),
        )
        return {"rows": len(payload), "dataset_path": str(target)}

    def upsert_company_research_evidence(self, frame, dataset_name=COMPANY_RESEARCH_EVIDENCE_DATASET):
        self._ensure_writable()
        if frame is None or frame.empty:
            return {"rows": 0, "dataset_path": str(self.layout.dataset_path(dataset_name, layer="meta"))}
        payload = frame[COMPANY_RESEARCH_EVIDENCE_FIELDS].copy()
        target = self._upsert_meta_frame(
            dataset_name=dataset_name,
            frame=payload,
            dedupe_keys=["market", "stock_code", "source", "title"],
            sort_by=["market", "stock_code", "source", "title", "fetched_at"],
            date_column="fetched_at",
            partition_columns=("market",),
        )
        return {"rows": len(payload), "dataset_path": str(target)}

    def replace_company_research_evidence(self, frame, dataset_name=COMPANY_RESEARCH_EVIDENCE_DATASET):
        self._ensure_writable()
        payload = (
            frame[COMPANY_RESEARCH_EVIDENCE_FIELDS].copy()
            if frame is not None and not frame.empty
            else pd.DataFrame(columns=COMPANY_RESEARCH_EVIDENCE_FIELDS)
        )
        target = self._write_meta_frame(
            dataset_name=dataset_name,
            frame=payload,
            date_column="fetched_at",
            partition_columns=("market",),
        )
        return {"rows": len(payload), "dataset_path": str(target)}

    def _replace_meta_dataset(self, frame, fields, dataset_name, date_column="updated_at", partition_columns=None):
        self._ensure_writable()
        payload = (
            frame[fields].copy()
            if frame is not None and not frame.empty
            else pd.DataFrame(columns=fields)
        )
        target = self._write_meta_frame(
            dataset_name=dataset_name,
            frame=payload,
            date_column=date_column,
            partition_columns=partition_columns,
        )
        return {"rows": len(payload), "dataset_path": str(target)}

    def _upsert_meta_dataset(self, frame, fields, dataset_name, dedupe_keys, sort_by, date_column="updated_at", partition_columns=None):
        self._ensure_writable()
        if frame is None or frame.empty:
            return {"rows": 0, "dataset_path": str(self.layout.dataset_path(dataset_name, layer="meta"))}
        payload = frame[fields].copy()
        target = self._upsert_meta_frame(
            dataset_name=dataset_name,
            frame=payload,
            dedupe_keys=dedupe_keys,
            sort_by=sort_by,
            date_column=date_column,
            partition_columns=partition_columns,
        )
        return {"rows": len(payload), "dataset_path": str(target)}

    def replace_entity_aliases(self, frame, dataset_name=ENTITY_ALIAS_DATASET):
        return self._replace_meta_dataset(
            frame, ENTITY_ALIAS_FIELDS, dataset_name, partition_columns=("market",)
        )

    def upsert_entity_aliases(self, frame, dataset_name=ENTITY_ALIAS_DATASET):
        return self._upsert_meta_dataset(
            frame,
            ENTITY_ALIAS_FIELDS,
            dataset_name,
            dedupe_keys=["market", "stock_code", "alias_type", "alias"],
            sort_by=["market", "stock_code", "alias_type", "alias", "updated_at"],
            partition_columns=("market",),
        )

    def replace_stock_profiles(self, frame, dataset_name=STOCK_PROFILE_DATASET):
        return self._replace_meta_dataset(
            frame, STOCK_PROFILE_FIELDS, dataset_name, partition_columns=("market",)
        )

    def replace_stock_deep_tags(self, frame, dataset_name=STOCK_DEEP_TAG_DATASET):
        return self._replace_meta_dataset(
            frame, STOCK_DEEP_TAG_FIELDS, dataset_name, partition_columns=("market",)
        )

    def replace_stock_graph_nodes(self, frame, dataset_name=STOCK_GRAPH_NODE_DATASET):
        return self._replace_meta_dataset(
            frame, STOCK_GRAPH_NODE_FIELDS, dataset_name, partition_columns=("node_type",)
        )

    def replace_stock_graph_edges(self, frame, dataset_name=STOCK_GRAPH_EDGE_DATASET):
        return self._replace_meta_dataset(
            frame, STOCK_GRAPH_EDGE_FIELDS, dataset_name, partition_columns=("edge_type",)
        )

    def replace_attention_signals(self, frame, dataset_name=ATTENTION_SIGNAL_DATASET):
        return self._replace_meta_dataset(
            frame, ATTENTION_SIGNAL_FIELDS, dataset_name, date_column="asof_date", partition_columns=("source", "metric")
        )

    def replace_theme_opportunity_scores(self, frame, dataset_name=THEME_OPPORTUNITY_SCORE_DATASET):
        return self._replace_meta_dataset(
            frame,
            THEME_OPPORTUNITY_SCORE_FIELDS,
            dataset_name,
            date_column="asof_date",
            partition_columns=("market", "theme"),
        )

    def read_theme_opportunity_scores(self, stock_codes=None, theme=None, market=None):
        filters = {}
        if stock_codes:
            filters["stock_code"] = list(dict.fromkeys(stock_codes))
        if theme:
            filters["theme"] = theme
        if market:
            filters["market"] = market
        return self._read_meta_frame(
            self.THEME_OPPORTUNITY_SCORE_DATASET,
            filters=filters,
            columns=THEME_OPPORTUNITY_SCORE_FIELDS,
            order_by="market, theme, asof_date, score",
        )

    def read_entity_aliases(self, stock_codes=None, market=None):
        filters = {}
        if stock_codes:
            filters["stock_code"] = list(dict.fromkeys(stock_codes))
        if market:
            filters["market"] = market
        return self._read_meta_frame(
            self.ENTITY_ALIAS_DATASET,
            filters=filters,
            columns=ENTITY_ALIAS_FIELDS,
            order_by="market, stock_code, alias_type, alias",
        )

    def read_stock_graph_edges(self, src_id=None, dst_id=None, edge_type=None):
        filters = {}
        if src_id:
            filters["src_id"] = src_id
        if dst_id:
            filters["dst_id"] = dst_id
        if edge_type:
            filters["edge_type"] = edge_type
        return self._read_meta_frame(
            self.STOCK_GRAPH_EDGE_DATASET,
            filters=filters,
            columns=STOCK_GRAPH_EDGE_FIELDS,
            order_by="src_type, src_id, edge_type, dst_type, dst_id",
        )

    def read_stock_graph_nodes(self, node_ids=None, node_type=None):
        filters = {}
        if node_ids:
            filters["node_id"] = list(dict.fromkeys(node_ids))
        if node_type:
            filters["node_type"] = node_type
        return self._read_meta_frame(
            self.STOCK_GRAPH_NODE_DATASET,
            filters=filters,
            columns=STOCK_GRAPH_NODE_FIELDS,
            order_by="node_type, node_id",
        )

    def read_stock_tags(self, stock_codes=None, market=None, tag=None, tag_type=None, min_confidence=None):
        filters = {}
        if stock_codes:
            filters["stock_code"] = list(dict.fromkeys(stock_codes))
        if market:
            filters["market"] = market
        if tag:
            filters["tag"] = tag
        if tag_type:
            filters["tag_type"] = tag_type
        frame = self._read_meta_frame(
            self.STOCK_TAG_DATASET,
            filters=filters,
            columns=STOCK_TAG_FIELDS,
            order_by="market, stock_code, tag_type, tag",
        )
        if frame is None or frame.empty:
            return pd.DataFrame(columns=STOCK_TAG_FIELDS)
        frame["confidence"] = pd.to_numeric(frame["confidence"], errors="coerce").fillna(0.0)
        if min_confidence is not None:
            frame = frame.loc[frame["confidence"] >= float(min_confidence)]
        frame.reset_index(drop=True, inplace=True)
        return frame

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
        """按条件读取 clean 层 OHLCV 数据，ClickHouse 优先。"""
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
        base_filters = {
            "stock_code": stock_code,
            "market": market,
            "exchange": exchange,
            "asset_type": asset_type,
            "frequency": frequency,
            "adjust": adjust,
        }
        frame = pd.DataFrame()
        for store in self._clean_store_candidates():
            try:
                filters = dict(base_filters)
                if store is self.parquet_store:
                    filters["year"] = year_filter
                frame = store.read_frame(
                    dataset_name=dataset_name,
                    layer="clean",
                    filters=filters,
                    order_by="market, stock_code, trade_date",
                    range_filters=range_filters,
                )
            except Exception as exc:
                if store is self.clickhouse_store:
                    self._clickhouse_disabled_reason = str(exc)
                frame = pd.DataFrame()
            if frame is not None and not frame.empty:
                break
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
        """获取某只证券的最新交易日，ClickHouse 优先。"""
        filters = {
            "stock_code": stock_code,
            "market": market,
            "exchange": exchange,
            "asset_type": asset_type,
            "frequency": frequency,
            "adjust": adjust,
        }
        latest = None
        for store in self._clean_store_candidates():
            try:
                latest = store.scalar_query(
                    dataset_name=dataset_name,
                    expression="MAX(trade_date)",
                    layer="clean",
                    filters=filters,
                )
            except Exception as exc:
                if store is self.clickhouse_store:
                    self._clickhouse_disabled_reason = str(exc)
                latest = None
            if latest is not None:
                break
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
        """批量获取证券最新交易日，ClickHouse 优先，避免逐只扫描。"""
        filters = {
            "stock_code": list(dict.fromkeys(stock_codes)) if stock_codes else None,
            "market": market,
            "exchange": exchange,
            "asset_type": asset_type,
            "frequency": list(dict.fromkeys(frequencies)) if frequencies else None,
            "adjust": adjust,
        }
        columns = ["stock_code", "market", "frequency", "adjust", "trade_date"]
        frame = pd.DataFrame()
        for store in self._clean_store_candidates():
            try:
                frame = store.read_frame(
                    dataset_name=dataset_name,
                    layer="clean",
                    filters=filters,
                    columns=columns,
                )
            except Exception as exc:
                if store is self.clickhouse_store:
                    self._clickhouse_disabled_reason = str(exc)
                frame = pd.DataFrame(columns=columns)
            if frame is not None and not frame.empty:
                break
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
        """获取 OHLCV 数据集统计信息，ClickHouse 优先。"""
        filters = {
            "stock_code": stock_code,
            "market": market,
            "exchange": exchange,
            "asset_type": asset_type,
            "frequency": frequency,
            "adjust": adjust,
        }
        stats_store = None
        total_records = None
        for store in self._clean_store_candidates():
            try:
                total_records = store.scalar_query(
                    dataset_name=dataset_name,
                    expression="COUNT(*)",
                    layer="clean",
                    filters=filters,
                )
            except Exception as exc:
                if store is self.clickhouse_store:
                    self._clickhouse_disabled_reason = str(exc)
                total_records = None
            if total_records not in (None, 0):
                stats_store = store
                break
        if total_records in (None, 0):
            return None

        min_date = stats_store.scalar_query(
            dataset_name=dataset_name,
            expression="MIN(trade_date)",
            layer="clean",
            filters=filters,
        )
        max_date = stats_store.scalar_query(
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
        """获取 OHLCV 数据集中全部证券代码，ClickHouse 优先。"""
        filters = {
            "market": market,
            "asset_type": asset_type,
            "frequency": frequency,
            "adjust": adjust,
        }
        for store in self._clean_store_candidates():
            try:
                values = store.values_query(
                    dataset_name=dataset_name,
                    column="stock_code",
                    layer="clean",
                    filters=filters,
                    distinct=True,
                    order_by="value",
                )
            except Exception as exc:
                if store is self.clickhouse_store:
                    self._clickhouse_disabled_reason = str(exc)
                values = []
            if values:
                return values
        return []

    def get_total_rows(self, market=None, asset_type=None, frequency=None, adjust=None, dataset_name=OHLCV_DATASET):
        """获取 OHLCV 数据集总行数，ClickHouse 优先。"""
        filters = {
            "market": market,
            "asset_type": asset_type,
            "frequency": frequency,
            "adjust": adjust,
        }
        total = None
        for store in self._clean_store_candidates():
            try:
                total = store.scalar_query(
                    dataset_name=dataset_name,
                    expression="COUNT(*)",
                    layer="clean",
                    filters=filters,
                )
            except Exception as exc:
                if store is self.clickhouse_store:
                    self._clickhouse_disabled_reason = str(exc)
                total = None
            if total not in (None, 0):
                break
        return int(total or 0)

    def compute_rps_features(self, factor_set="qlib_alpha158",
                             windows=(5, 10, 20, 30, 60),
                             progress_callback=None):
        """基于已有 ROC 因子计算横截面 RPS 排名并写入 feature 层。"""
        return self._feature_store.compute_rps_features(
            factor_set=factor_set, windows=windows, progress_callback=progress_callback,
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
