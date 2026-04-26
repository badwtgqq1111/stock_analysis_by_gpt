#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Parquet 数据存储抽象。"""

import shutil
from pathlib import Path

import duckdb
import pandas as pd


class ParquetDataStore:
    """基于 DuckDB 的分区 Parquet 数据集存储。"""

    PARTITION_COLUMNS = ("market", "exchange", "asset_type", "frequency", "adjust", "year")

    def __init__(self, layout):
        self.layout = layout

    def dataset_exists(self, dataset_name, layer="clean"):
        """判断 parquet 数据集是否存在。"""
        target = self.layout.dataset_path(dataset_name, layer=layer)
        return target.exists() and any(target.rglob("*.parquet"))

    def read_frame(self, dataset_name, layer="clean", filters=None, columns=None, order_by=None):
        """按条件读取 parquet 数据集。"""
        if not self.dataset_exists(dataset_name, layer=layer):
            return pd.DataFrame()

        select_sql = ", ".join(columns) if columns else "*"
        query, params = self._build_query(
            dataset_name=dataset_name,
            layer=layer,
            select_sql=select_sql,
            filters=filters,
            order_by=order_by,
        )
        conn = duckdb.connect(database=":memory:")
        try:
            frame = conn.execute(query, params).df()
        finally:
            conn.close()

        if "year" in frame.columns:
            frame.drop(columns=["year"], inplace=True)
        return frame

    def scalar_query(self, dataset_name, expression, layer="clean", filters=None):
        """执行单值聚合查询。"""
        if not self.dataset_exists(dataset_name, layer=layer):
            return None

        query, params = self._build_query(
            dataset_name=dataset_name,
            layer=layer,
            select_sql=f"{expression} AS value",
            filters=filters,
        )
        conn = duckdb.connect(database=":memory:")
        try:
            result = conn.execute(query, params).fetchone()
        finally:
            conn.close()
        return result[0] if result else None

    def values_query(self, dataset_name, column, layer="clean", filters=None, distinct=False, order_by=None):
        """执行单列值查询。"""
        if not self.dataset_exists(dataset_name, layer=layer):
            return []

        prefix = "DISTINCT " if distinct else ""
        query, params = self._build_query(
            dataset_name=dataset_name,
            layer=layer,
            select_sql=f"{prefix}{column} AS value",
            filters=filters,
            order_by=order_by,
        )
        conn = duckdb.connect(database=":memory:")
        try:
            result = conn.execute(query, params).fetchall()
        finally:
            conn.close()
        return [row[0] for row in result]

    def write_frame(self, dataset_name, frame, layer="clean"):
        """覆盖写入 parquet 数据集。"""
        target = self.layout.dataset_path(dataset_name, layer=layer)
        self._overwrite_dataset(target, frame)
        return target

    def upsert_frame(self, dataset_name, frame, dedupe_keys, layer="clean", sort_by=None):
        """按主键去重后写回 parquet 数据集。"""
        existing = self.read_frame(dataset_name, layer=layer)
        combined = pd.concat([existing, frame], ignore_index=True) if not existing.empty else frame.copy()
        if sort_by:
            combined.sort_values(sort_by, inplace=True)
        combined.drop_duplicates(subset=dedupe_keys, keep="last", inplace=True)
        combined.reset_index(drop=True, inplace=True)
        target = self.layout.dataset_path(dataset_name, layer=layer)
        self._overwrite_dataset(target, combined)
        return target

    def _build_query(self, dataset_name, layer, select_sql, filters=None, order_by=None):
        dataset_glob = self.layout.dataset_glob(dataset_name, layer=layer)
        clauses = []
        params = [dataset_glob]

        for column, value in (filters or {}).items():
            if value is None:
                continue
            if isinstance(value, (list, tuple, set)):
                values = list(value)
                if not values:
                    continue
                placeholders = ", ".join(["?"] * len(values))
                clauses.append(f"{column} IN ({placeholders})")
                params.extend(values)
            else:
                clauses.append(f"{column} = ?")
                params.append(value)

        query = f"SELECT {select_sql} FROM read_parquet(?, hive_partitioning = true)"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        if order_by:
            query += f" ORDER BY {order_by}"
        return query, params

    def _overwrite_dataset(self, dataset_dir, frame):
        dataset_path = Path(dataset_dir)
        dataset_path.parent.mkdir(parents=True, exist_ok=True)

        if frame is None or frame.empty:
            if dataset_path.exists():
                shutil.rmtree(dataset_path)
            dataset_path.mkdir(parents=True, exist_ok=True)
            return

        prepared = frame.copy()
        prepared["trade_date"] = pd.to_datetime(prepared["trade_date"], errors="coerce")
        prepared.dropna(subset=["trade_date"], inplace=True)
        prepared["year"] = prepared["trade_date"].dt.year.astype("int32")

        temp_dir = dataset_path.parent / f".{dataset_path.name}_tmp"
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        if dataset_path.exists():
            shutil.rmtree(dataset_path)

        conn = duckdb.connect(database=":memory:")
        try:
            conn.register("frame_view", prepared)
            partition_sql = ", ".join(self.PARTITION_COLUMNS)
            conn.execute(
                f"""
                COPY (
                    SELECT * FROM frame_view
                ) TO ? (
                    FORMAT PARQUET,
                    PARTITION_BY ({partition_sql})
                )
                """,
                [str(temp_dir)],
            )
        finally:
            conn.close()

        temp_dir.rename(dataset_path)
