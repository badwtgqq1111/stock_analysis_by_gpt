#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""ClickHouse 数据存储后端 —— 支持并发写入。"""

import os

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

_TAG_DICTIONARY_COLUMNS = [
    "tag", "tag_type", "canonical_tag", "aliases", "description",
    "parent_tag", "active", "updated_at",
]

_TAG_DICTIONARY_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    tag String,
    tag_type LowCardinality(String),
    canonical_tag String,
    aliases String,
    description String,
    parent_tag String,
    active Bool,
    updated_at DateTime
) ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY tag_type
ORDER BY ({order_by})
"""

_TAG_DICTIONARY_ORDER_BY = ["tag_type", "tag"]

_STOCK_TAG_COLUMNS = [
    "stock_code", "market", "tag", "tag_type", "confidence", "is_primary",
    "source", "evidence", "evidence_url", "updated_at",
]

_STOCK_TAG_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    stock_code LowCardinality(String),
    market LowCardinality(String),
    tag String,
    tag_type LowCardinality(String),
    confidence Float64,
    is_primary Bool,
    source String,
    evidence String,
    evidence_url String,
    updated_at DateTime
) ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY market
ORDER BY ({order_by})
"""

_STOCK_TAG_ORDER_BY = ["market", "stock_code", "tag_type", "tag"]

_STOCK_TAG_CANDIDATE_COLUMNS = [
    *_STOCK_TAG_COLUMNS,
    "review_status", "review_note",
]

_STOCK_TAG_CANDIDATE_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    stock_code LowCardinality(String),
    market LowCardinality(String),
    tag String,
    tag_type LowCardinality(String),
    confidence Float64,
    is_primary Bool,
    source String,
    evidence String,
    evidence_url String,
    updated_at DateTime,
    review_status LowCardinality(String),
    review_note String
) ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY market
ORDER BY ({order_by})
"""

_COMPANY_RESEARCH_EVIDENCE_COLUMNS = [
    "stock_code", "market", "source", "title", "summary", "url",
    "raw_text", "fetched_at",
]

_COMPANY_RESEARCH_EVIDENCE_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    stock_code LowCardinality(String),
    market LowCardinality(String),
    source String,
    title String,
    summary String,
    url String,
    raw_text String,
    fetched_at DateTime
) ENGINE = ReplacingMergeTree(fetched_at)
PARTITION BY market
ORDER BY ({order_by})
"""

_COMPANY_RESEARCH_EVIDENCE_ORDER_BY = ["market", "stock_code", "source", "title"]

_ENTITY_ALIAS_COLUMNS = [
    "stock_code", "market", "alias", "alias_type", "source", "confidence", "updated_at",
]

_ENTITY_ALIAS_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    stock_code LowCardinality(String),
    market LowCardinality(String),
    alias String,
    alias_type LowCardinality(String),
    source String,
    confidence Float64,
    updated_at DateTime
) ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY market
ORDER BY ({order_by})
"""

_ENTITY_ALIAS_ORDER_BY = ["market", "stock_code", "alias_type", "alias"]

_STOCK_PROFILE_COLUMNS = [
    "stock_code", "market", "profile_json", "summary", "strengths", "risks",
    "open_questions", "evidence_count", "confidence", "updated_at",
]

_STOCK_PROFILE_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    stock_code LowCardinality(String),
    market LowCardinality(String),
    profile_json String,
    summary String,
    strengths String,
    risks String,
    open_questions String,
    evidence_count UInt32,
    confidence Float64,
    updated_at DateTime
) ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY market
ORDER BY ({order_by})
"""

_STOCK_PROFILE_ORDER_BY = ["market", "stock_code"]

_STOCK_DEEP_TAG_COLUMNS = [
    "stock_code", "market", "tag", "tag_type", "confidence", "evidence_count",
    "source_count", "freshness_days", "attention_velocity_7d", "is_primary",
    "evidence_refs", "source", "updated_at",
]

_STOCK_DEEP_TAG_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    stock_code LowCardinality(String),
    market LowCardinality(String),
    tag String,
    tag_type LowCardinality(String),
    confidence Float64,
    evidence_count UInt32,
    source_count UInt32,
    freshness_days Float64,
    attention_velocity_7d Float64,
    is_primary Bool,
    evidence_refs String,
    source String,
    updated_at DateTime
) ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY market
ORDER BY ({order_by})
"""

_STOCK_DEEP_TAG_ORDER_BY = ["market", "stock_code", "tag_type", "tag"]

_STOCK_GRAPH_NODE_COLUMNS = [
    "node_id", "node_type", "name", "canonical_name", "properties_json",
    "source", "confidence", "updated_at",
]

_STOCK_GRAPH_NODE_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    node_id String,
    node_type LowCardinality(String),
    name String,
    canonical_name String,
    properties_json String,
    source String,
    confidence Float64,
    updated_at DateTime
) ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY node_type
ORDER BY ({order_by})
"""

_STOCK_GRAPH_NODE_ORDER_BY = ["node_type", "node_id"]

_STOCK_GRAPH_EDGE_COLUMNS = [
    "src_type", "src_id", "edge_type", "dst_type", "dst_id", "confidence",
    "evidence_refs", "source", "updated_at",
]

_STOCK_GRAPH_EDGE_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    src_type LowCardinality(String),
    src_id String,
    edge_type LowCardinality(String),
    dst_type LowCardinality(String),
    dst_id String,
    confidence Float64,
    evidence_refs String,
    source String,
    updated_at DateTime
) ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY edge_type
ORDER BY ({order_by})
"""

_STOCK_GRAPH_EDGE_ORDER_BY = ["src_type", "src_id", "edge_type", "dst_type", "dst_id"]

_ATTENTION_SIGNAL_COLUMNS = [
    "entity_type", "entity_id", "source", "metric", "value", "window",
    "velocity", "quality_score", "asof_date",
]

_ATTENTION_SIGNAL_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    entity_type LowCardinality(String),
    entity_id String,
    source LowCardinality(String),
    metric LowCardinality(String),
    value Float64,
    window LowCardinality(String),
    velocity Float64,
    quality_score Float64,
    asof_date Date
) ENGINE = ReplacingMergeTree()
PARTITION BY (source, metric)
ORDER BY ({order_by})
"""

_ATTENTION_SIGNAL_ORDER_BY = ["entity_type", "entity_id", "source", "metric", "window", "asof_date"]

_THEME_OPPORTUNITY_SCORE_COLUMNS = [
    "stock_code", "market", "theme", "score", "technology_score",
    "commercialization_score", "value_chain_score", "bottleneck_score",
    "catalyst_score", "attention_score", "evidence_quality_score",
    "liquidity_score", "technical_trend_score", "risk_penalty",
    "crowding_penalty", "verdict", "rank_reason", "bull_case",
    "bear_case", "key_evidence_refs", "component_scores_json",
    "asof_date", "updated_at",
]

_THEME_OPPORTUNITY_SCORE_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    stock_code LowCardinality(String),
    market LowCardinality(String),
    theme String,
    score Float64,
    technology_score Float64,
    commercialization_score Float64,
    value_chain_score Float64,
    bottleneck_score Float64,
    catalyst_score Float64,
    attention_score Float64,
    evidence_quality_score Float64,
    liquidity_score Float64,
    technical_trend_score Float64,
    risk_penalty Float64,
    crowding_penalty Float64,
    verdict LowCardinality(String),
    rank_reason String,
    bull_case String,
    bear_case String,
    key_evidence_refs String,
    component_scores_json String,
    asof_date Date,
    updated_at DateTime
) ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY (market, theme)
ORDER BY ({order_by})
"""

_THEME_OPPORTUNITY_SCORE_ORDER_BY = ["market", "theme", "asof_date", "stock_code"]

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
    "tag_dictionary": {
        "columns": _TAG_DICTIONARY_COLUMNS,
        "ddl": _TAG_DICTIONARY_DDL,
        "order_by": _TAG_DICTIONARY_ORDER_BY,
    },
    "stock_tag_registry": {
        "columns": _STOCK_TAG_COLUMNS,
        "ddl": _STOCK_TAG_DDL,
        "order_by": _STOCK_TAG_ORDER_BY,
    },
    "stock_tag_candidate": {
        "columns": _STOCK_TAG_CANDIDATE_COLUMNS,
        "ddl": _STOCK_TAG_CANDIDATE_DDL,
        "order_by": _STOCK_TAG_ORDER_BY,
    },
    "company_research_evidence": {
        "columns": _COMPANY_RESEARCH_EVIDENCE_COLUMNS,
        "ddl": _COMPANY_RESEARCH_EVIDENCE_DDL,
        "order_by": _COMPANY_RESEARCH_EVIDENCE_ORDER_BY,
    },
    "entity_alias_registry": {
        "columns": _ENTITY_ALIAS_COLUMNS,
        "ddl": _ENTITY_ALIAS_DDL,
        "order_by": _ENTITY_ALIAS_ORDER_BY,
    },
    "stock_profile": {
        "columns": _STOCK_PROFILE_COLUMNS,
        "ddl": _STOCK_PROFILE_DDL,
        "order_by": _STOCK_PROFILE_ORDER_BY,
    },
    "stock_deep_tag_registry": {
        "columns": _STOCK_DEEP_TAG_COLUMNS,
        "ddl": _STOCK_DEEP_TAG_DDL,
        "order_by": _STOCK_DEEP_TAG_ORDER_BY,
    },
    "stock_graph_nodes": {
        "columns": _STOCK_GRAPH_NODE_COLUMNS,
        "ddl": _STOCK_GRAPH_NODE_DDL,
        "order_by": _STOCK_GRAPH_NODE_ORDER_BY,
    },
    "stock_graph_edges": {
        "columns": _STOCK_GRAPH_EDGE_COLUMNS,
        "ddl": _STOCK_GRAPH_EDGE_DDL,
        "order_by": _STOCK_GRAPH_EDGE_ORDER_BY,
    },
    "attention_signal": {
        "columns": _ATTENTION_SIGNAL_COLUMNS,
        "ddl": _ATTENTION_SIGNAL_DDL,
        "order_by": _ATTENTION_SIGNAL_ORDER_BY,
    },
    "theme_opportunity_score": {
        "columns": _THEME_OPPORTUNITY_SCORE_COLUMNS,
        "ddl": _THEME_OPPORTUNITY_SCORE_DDL,
        "order_by": _THEME_OPPORTUNITY_SCORE_ORDER_BY,
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
        client = self._connect()
        try:
            result = client.query(
                "SELECT 1 FROM system.tables WHERE database = {db:String} AND name = {tbl:String}",
                parameters={"db": self.database, "tbl": table},
            )
            return len(result.result_rows) > 0
        finally:
            client.close()

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
                             layer="feature",
                             progress_callback=None):
        """基于已有 ROC 因子计算横截面 RPS 排名并写入同一张表。

        对每个窗口的 ROC 因子做跨股票百分位排名，生成 RPS_{w} 特征。
        ROC{w} = close_past / close_today，值越低收益越高，所以升序排名。
        """
        def _progress(message):
            if progress_callback is not None:
                progress_callback(message)

        table = self._table_name("features", layer=layer)
        client = self._connect()
        rows_written = 0
        try:
            self._ensure_table(client, "features", layer)
            for w in windows:
                roc_name = f"ROC{w}"
                rps_name = f"RPS_{w}"
                _progress(f"rps window={w} querying ClickHouse")

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
                rows_written += len(rps_df)
                _progress(f"rps window={w} inserted_rows={len(rps_df)}")
        finally:
            client.close()
        return rows_written

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

        for column in ["industry_updated_at", "instrument_updated_at", "ingest_time", "updated_at", "fetched_at"]:
            if column in prepared.columns:
                prepared[column] = pd.to_datetime(prepared[column], errors="coerce")
        for column in ["is_fund_like", "tradable_flag", "is_primary", "active"]:
            if column in prepared.columns:
                prepared[column] = prepared[column].fillna(False).astype(bool)
        for column in ["confidence"]:
            if column in prepared.columns:
                prepared[column] = pd.to_numeric(prepared[column], errors="coerce").fillna(0.0).astype(float)
        for column in [
            "name", "industry_l1", "industry_l2", "industry_l3", "theme_tags",
            "industry_source", "instrument_type", "instrument_source", "source",
            "tag", "tag_type", "canonical_tag", "aliases", "description", "parent_tag",
            "evidence", "evidence_url", "review_status", "review_note",
            "title", "summary", "url", "raw_text",
        ]:
            if column in prepared.columns:
                prepared[column] = prepared[column].fillna("").astype(str)

        schema = DATASET_SCHEMA.get(dataset_name)
        if schema:
            cols = [c for c in schema["columns"] if c in prepared.columns]
            prepared = prepared[cols]

        configured_chunk_rows = os.environ.get("CLICKHOUSE_INSERT_CHUNK_ROWS")
        if configured_chunk_rows:
            chunk_rows = max(1, int(configured_chunk_rows))
        else:
            chunk_rows = 50_000
        for start in range(0, len(prepared), chunk_rows):
            client.insert_df(table, prepared.iloc[start:start + chunk_rows])
