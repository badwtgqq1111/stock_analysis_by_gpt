#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""ClickHouse 数据存储后端 —— 支持并发写入。"""

import pandas as pd
from clickhouse_connect import get_client


_FEATURES_COLUMNS = [
    "trade_date", "stock_code", "market", "exchange", "asset_type",
    "frequency", "adjust", "feature_set", "feature_version",
    "feature_config_hash", "feature_name", "feature_value",
    "source", "ingest_time",
]

_FEATURES_ORDER_BY = [
    "market", "stock_code", "trade_date", "feature_set", "feature_version",
    "feature_config_hash", "feature_name",
]

_FEATURES_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    trade_date Date,
    stock_code LowCardinality(String),
    market LowCardinality(String),
    exchange LowCardinality(String),
    asset_type LowCardinality(String),
    frequency LowCardinality(String),
    adjust LowCardinality(String),
    feature_set LowCardinality(String),
    feature_version LowCardinality(String),
    feature_config_hash LowCardinality(String),
    feature_name LowCardinality(String),
    feature_value Float64,
    source LowCardinality(String),
    ingest_time DateTime
) ENGINE = ReplacingMergeTree()
PARTITION BY (feature_set, feature_config_hash)
ORDER BY ({order_by})
"""

_OHLCV_COLUMNS = [
    "trade_date", "stock_code", "market", "exchange", "asset_type",
    "frequency", "adjust", "open", "high", "low", "close", "volume",
    "amount", "turnover", "vwap", "source", "ingest_time",
]

_OHLCV_ORDER_BY = ["market", "stock_code", "trade_date", "frequency", "adjust"]

_OHLCV_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    trade_date Date,
    stock_code LowCardinality(String),
    market LowCardinality(String),
    exchange LowCardinality(String),
    asset_type LowCardinality(String),
    frequency LowCardinality(String),
    adjust LowCardinality(String),
    open Float64,
    high Float64,
    low Float64,
    close Float64,
    volume Float64,
    amount Float64,
    turnover Float64,
    vwap Float64,
    source LowCardinality(String),
    ingest_time DateTime
) ENGINE = ReplacingMergeTree()
PARTITION BY (market, frequency, adjust)
ORDER BY ({order_by})
"""

_STOCK_INFO_COLUMNS = [
    "stock_code", "market", "exchange", "asset_type", "name",
    "current_price", "close_price", "open_price", "high", "low",
    "volume", "market_cap", "pe_ratio", "pb_ratio", "dividend_yield",
    "total_shares", "circulating_shares", "week_52_high", "week_52_low",
    "industry_l1", "industry_l2", "industry_l3", "theme_tags",
    "industry_source", "industry_updated_at", "instrument_type",
    "is_fund_like", "tradable_flag", "instrument_source",
    "instrument_updated_at", "source", "ingest_time",
]

_STOCK_INFO_ORDER_BY = ["market", "stock_code"]

_STOCK_INFO_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    stock_code LowCardinality(String),
    market LowCardinality(String),
    exchange LowCardinality(String),
    asset_type LowCardinality(String),
    name String,
    current_price Nullable(Float64),
    close_price Nullable(Float64),
    open_price Nullable(Float64),
    high Nullable(Float64),
    low Nullable(Float64),
    volume Nullable(Float64),
    market_cap Nullable(Float64),
    pe_ratio Nullable(Float64),
    pb_ratio Nullable(Float64),
    dividend_yield Nullable(Float64),
    total_shares Nullable(Float64),
    circulating_shares Nullable(Float64),
    week_52_high Nullable(Float64),
    week_52_low Nullable(Float64),
    industry_l1 String,
    industry_l2 String,
    industry_l3 String,
    theme_tags String,
    industry_source String,
    industry_updated_at Nullable(DateTime),
    instrument_type LowCardinality(String),
    is_fund_like Bool,
    tradable_flag Bool,
    instrument_source String,
    instrument_updated_at Nullable(DateTime),
    source String,
    ingest_time DateTime
) ENGINE = ReplacingMergeTree(ingest_time)
PARTITION BY market
ORDER BY ({order_by})
"""

DATASET_SCHEMA = {
    "features": {
        "columns": _FEATURES_COLUMNS,
        "ddl": _FEATURES_DDL,
        "order_by": _FEATURES_ORDER_BY,
    },
    "ohlcv": {
        "columns": _OHLCV_COLUMNS,
        "ddl": _OHLCV_DDL,
        "order_by": _OHLCV_ORDER_BY,
    },
    "stock_info_registry": {
        "columns": _STOCK_INFO_COLUMNS,
        "ddl": _STOCK_INFO_DDL,
        "order_by": _STOCK_INFO_ORDER_BY,
    },
}


class ClickHouseStore:
    """ClickHouse 数据存储。

    所有 INSERT 天然支持并发写入，ReplacingMergeTree 自动去重。
    """

    def __init__(self, host="localhost", port=8123, user="default", password="",
                 database="quant", layout=None):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database

    def _connect(self):
        return get_client(
            host=self.host, port=self.port,
            username=self.user, password=self.password,
            database=self.database,
        )

    def _table_name(self, dataset_name, layer="clean"):
        return f"{dataset_name}_{layer}"

    def _ensure_table(self, client, dataset_name, layer):
        table = self._table_name(dataset_name, layer)
        schema = DATASET_SCHEMA.get(dataset_name)
        if schema is None:
            return table
        ddl = schema["ddl"].format(table=table, order_by=", ".join(schema["order_by"]))
        client.command(ddl)
        return table

    # ---- public interface (matches ParquetDataStore) ----

    def dataset_exists(self, dataset_name, layer="clean"):
        table = self._table_name(dataset_name, layer)
        try:
            client = self._connect()
            result = client.query(
                "SELECT 1 FROM system.tables WHERE database = {db:String} AND name = {tbl:String}",
                parameters={"db": self.database, "tbl": table},
            )
            return len(result.result_rows) > 0
        except Exception:
            return False

    def read_frame(self, dataset_name, layer="clean", filters=None, columns=None,
                   order_by=None, range_filters=None):
        if not self.dataset_exists(dataset_name, layer=layer):
            return pd.DataFrame()

        table = self._table_name(dataset_name, layer)
        select_sql = ", ".join(columns) if columns else "*"
        query = f"SELECT {select_sql} FROM {table}"
        params = {}
        clauses = []

        for i, (col, val) in enumerate((filters or {}).items()):
            if val is None:
                continue
            if isinstance(val, (list, tuple, set)):
                vlist = list(val)
                if not vlist:
                    continue
                placeholders = ", ".join(f"{{p{i}_{j}:String}}" for j in range(len(vlist)))
                clauses.append(f"{col} IN ({placeholders})")
                for j, v in enumerate(vlist):
                    params[f"p{i}_{j}"] = str(v)
            else:
                clauses.append(f"{col} = {{p{i}:String}}")
                params[f"p{i}"] = str(val)

        for col, bounds in (range_filters or {}).items():
            lower = bounds.get("gte")
            if lower is not None:
                clauses.append(f"{col} >= {{r_{col}_l:String}}")
                params[f"r_{col}_l"] = str(lower)
            upper = bounds.get("lte")
            if upper is not None:
                clauses.append(f"{col} <= {{r_{col}_u:String}}")
                params[f"r_{col}_u"] = str(upper)

        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        if order_by:
            query += f" ORDER BY {order_by}"

        client = self._connect()
        try:
            return client.query_df(query, parameters=params)
        finally:
            client.close()

    def scalar_query(self, dataset_name, expression, layer="clean", filters=None,
                     range_filters=None):
        if not self.dataset_exists(dataset_name, layer=layer):
            return None

        table = self._table_name(dataset_name, layer)
        query = f"SELECT {expression} AS value FROM {table} FINAL"
        params = {}
        clauses = []

        for i, (col, val) in enumerate((filters or {}).items()):
            if val is None:
                continue
            if isinstance(val, (list, tuple, set)):
                vlist = list(val)
                if not vlist:
                    continue
                placeholders = ", ".join(f"{{p{i}_{j}:String}}" for j in range(len(vlist)))
                clauses.append(f"{col} IN ({placeholders})")
                for j, v in enumerate(vlist):
                    params[f"p{i}_{j}"] = str(v)
            else:
                clauses.append(f"{col} = {{p{i}:String}}")
                params[f"p{i}"] = str(val)

        for col, bounds in (range_filters or {}).items():
            lower = bounds.get("gte")
            if lower is not None:
                clauses.append(f"{col} >= {{r_{col}_l:String}}")
                params[f"r_{col}_l"] = str(lower)
            upper = bounds.get("lte")
            if upper is not None:
                clauses.append(f"{col} <= {{r_{col}_u:String}}")
                params[f"r_{col}_u"] = str(upper)

        if clauses:
            query += " WHERE " + " AND ".join(clauses)

        client = self._connect()
        try:
            result = client.query(query, parameters=params)
            rows = result.result_rows
            return rows[0][0] if rows else None
        finally:
            client.close()

    def values_query(self, dataset_name, column, layer="clean", filters=None,
                     distinct=False, order_by=None, range_filters=None):
        if not self.dataset_exists(dataset_name, layer=layer):
            return []

        table = self._table_name(dataset_name, layer)
        prefix = "DISTINCT " if distinct else ""
        query = f"SELECT {prefix}{column} AS value FROM {table} FINAL"
        params = {}
        clauses = []

        for i, (col, val) in enumerate((filters or {}).items()):
            if val is None:
                continue
            if isinstance(val, (list, tuple, set)):
                vlist = list(val)
                if not vlist:
                    continue
                placeholders = ", ".join(f"{{p{i}_{j}:String}}" for j in range(len(vlist)))
                clauses.append(f"{col} IN ({placeholders})")
                for j, v in enumerate(vlist):
                    params[f"p{i}_{j}"] = str(v)
            else:
                clauses.append(f"{col} = {{p{i}:String}}")
                params[f"p{i}"] = str(val)

        for col, bounds in (range_filters or {}).items():
            lower = bounds.get("gte")
            if lower is not None:
                clauses.append(f"{col} >= {{r_{col}_l:String}}")
                params[f"r_{col}_l"] = str(lower)
            upper = bounds.get("lte")
            if upper is not None:
                clauses.append(f"{col} <= {{r_{col}_u:String}}")
                params[f"r_{col}_u"] = str(upper)

        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        if order_by:
            query += f" ORDER BY {order_by}"

        client = self._connect()
        try:
            result = client.query(query, parameters=params)
            return [row[0] for row in result.result_rows]
        finally:
            client.close()

    def write_frame(self, dataset_name, frame, layer="clean", date_column="trade_date",
                    partition_columns=None):
        table = self._table_name(dataset_name, layer)
        client = self._connect()
        try:
            self._ensure_table(client, dataset_name, layer)
            client.command(f"TRUNCATE TABLE {table}")
            if frame is not None and not frame.empty:
                self._insert_frame(client, table, dataset_name, frame, date_column)
        finally:
            client.close()
        return table

    def append_frame(self, dataset_name, frame, layer="clean", date_column="trade_date",
                     partition_columns=None):
        table = self._table_name(dataset_name, layer)
        client = self._connect()
        try:
            self._ensure_table(client, dataset_name, layer)
            if frame is not None and not frame.empty:
                self._insert_frame(client, table, dataset_name, frame, date_column)
        finally:
            client.close()
        return table

    def upsert_frame(self, dataset_name, frame, dedupe_keys, layer="clean",
                     sort_by=None, date_column="trade_date", partition_columns=None):
        table = self._table_name(dataset_name, layer)
        client = self._connect()
        try:
            self._ensure_table(client, dataset_name, layer)
            if frame is not None and not frame.empty:
                self._insert_frame(client, table, dataset_name, frame, date_column)
        finally:
            client.close()
        return table

    def compact_dataset(self, dataset_name, dedupe_keys, sort_by=None, layer="clean",
                        partition_columns=None):
        table = self._table_name(dataset_name, layer)
        client = self._connect()
        try:
            client.command(f"OPTIMIZE TABLE {table} FINAL DEDUPLICATE")
        finally:
            client.close()

    def compute_rps_features(self, factor_set="qlib_alpha158",
                             windows=(5, 10, 20, 30, 60),
                             layer="feature"):
        """基于已有 ROC 因子计算横截面 RPS 排名并写入同一张表。

        对每个窗口的 ROC 因子做跨股票百分位排名，生成 RPS_{w} 特征。
        ROC{w} = close_past / close_today，值越低收益越高，所以升序排名。
        """
        table = self._table_name("features", layer=layer)
        client = self._connect()
        try:
            self._ensure_table(client, "features", layer)
            for w in windows:
                roc_name = f"ROC{w}"
                rps_name = f"RPS_{w}"

                # Get existing metadata from one ROC row
                meta = client.query(
                    f"SELECT market, exchange, asset_type, frequency, adjust, "
                    f"feature_version, feature_config_hash "
                    f"FROM {table} "
                    f"WHERE feature_name = {{src:String}} "
                    f"AND feature_set = {{fs:String}} "
                    f"LIMIT 1",
                    parameters={"src": roc_name, "fs": factor_set},
                )
                if not meta.result_rows:
                    continue
                meta_row = meta.result_rows[0]
                mkt, exch, atype, freq, adj, ver, fhash = meta_row

                rps_df = client.query_df(
                    f"SELECT "
                    f"trade_date, stock_code, "
                    f"(1 - rank() OVER ("
                    f"    PARTITION BY trade_date ORDER BY feature_value ASC"
                    f") / CAST(count() OVER ("
                    f"    PARTITION BY trade_date"
                    f") AS Float64)) * 100 AS rps_val "
                    f"FROM {table} "
                    f"WHERE feature_name = {{src:String}} "
                    f"AND feature_set = {{fs:String}} "
                    f"AND feature_value IS NOT NULL",
                    parameters={"src": roc_name, "fs": factor_set},
                )
                if rps_df.empty:
                    continue

                import pandas as pd

                rps_df["market"] = mkt
                rps_df["exchange"] = exch
                rps_df["asset_type"] = atype
                rps_df["frequency"] = freq
                rps_df["adjust"] = adj
                rps_df["feature_set"] = factor_set
                rps_df["feature_version"] = ver
                rps_df["feature_config_hash"] = fhash
                rps_df["feature_name"] = rps_name
                rps_df["feature_value"] = rps_df["rps_val"]
                rps_df["source"] = "rps"
                rps_df["ingest_time"] = pd.Timestamp.utcnow()
                rps_df.drop(columns=["rps_val"], inplace=True)
                rps_df["trade_date"] = pd.to_datetime(rps_df["trade_date"])

                client.insert_df(table, rps_df)
        finally:
            client.close()
        return len(windows)

    # ---- internal helpers ----

    def _insert_frame(self, client, table, dataset_name, frame, date_column):
        if frame is None or frame.empty:
            return

        prepared = frame.copy()
        if date_column and date_column in prepared.columns:
            prepared[date_column] = pd.to_datetime(prepared[date_column], errors="coerce")
            prepared.dropna(subset=[date_column], inplace=True)

        if "ingest_time" not in prepared.columns:
            prepared["ingest_time"] = pd.Timestamp.utcnow()

        for column in ["industry_updated_at", "instrument_updated_at", "ingest_time"]:
            if column in prepared.columns:
                prepared[column] = pd.to_datetime(prepared[column], errors="coerce")
        for column in ["is_fund_like", "tradable_flag"]:
            if column in prepared.columns:
                prepared[column] = prepared[column].fillna(False).astype(bool)
        for column in [
            "name", "industry_l1", "industry_l2", "industry_l3", "theme_tags",
            "industry_source", "instrument_type", "instrument_source", "source",
        ]:
            if column in prepared.columns:
                prepared[column] = prepared[column].fillna("").astype(str)

        schema = DATASET_SCHEMA.get(dataset_name)
        if schema:
            cols = [c for c in schema["columns"] if c in prepared.columns]
            prepared = prepared[cols]

        client.insert_df(table, prepared)
