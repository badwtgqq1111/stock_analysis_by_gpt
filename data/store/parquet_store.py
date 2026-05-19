#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Parquet 数据存储抽象。"""

import shutil
from pathlib import Path
import uuid

import duckdb
import pandas as pd


class ParquetDataStore:
    """基于 DuckDB 的分区 Parquet 数据集存储。"""

    DEFAULT_PARTITION_COLUMNS = ("market", "exchange", "asset_type", "frequency", "adjust", "year")

    def __init__(self, layout):
        self.layout = layout

    def dataset_exists(self, dataset_name, layer="clean"):
        """判断 parquet 数据集是否存在。"""
        target = self.layout.dataset_path(dataset_name, layer=layer)
        return target.exists() and any(target.rglob("*.parquet"))

    def read_frame(self, dataset_name, layer="clean", filters=None, columns=None, order_by=None, range_filters=None):
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
            range_filters=range_filters,
        )
        conn = duckdb.connect(database=":memory:")
        try:
            frame = conn.execute(query, params).df()
        finally:
            conn.close()

        if "year" in frame.columns:
            frame.drop(columns=["year"], inplace=True)
        return frame

    def scalar_query(self, dataset_name, expression, layer="clean", filters=None, range_filters=None):
        """执行单值聚合查询。"""
        if not self.dataset_exists(dataset_name, layer=layer):
            return None

        query, params = self._build_query(
            dataset_name=dataset_name,
            layer=layer,
            select_sql=f"{expression} AS value",
            filters=filters,
            range_filters=range_filters,
        )
        conn = duckdb.connect(database=":memory:")
        try:
            result = conn.execute(query, params).fetchone()
        finally:
            conn.close()
        return result[0] if result else None

    def values_query(self, dataset_name, column, layer="clean", filters=None, distinct=False, order_by=None, range_filters=None):
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
            range_filters=range_filters,
        )
        conn = duckdb.connect(database=":memory:")
        try:
            result = conn.execute(query, params).fetchall()
        finally:
            conn.close()
        return [row[0] for row in result]

    def write_frame(self, dataset_name, frame, layer="clean", date_column="trade_date", partition_columns=None):
        """覆盖写入 parquet 数据集。"""
        target = self.layout.dataset_path(dataset_name, layer=layer)
        self._overwrite_dataset(
            target,
            frame,
            date_column=date_column,
            partition_columns=partition_columns or self.DEFAULT_PARTITION_COLUMNS,
        )
        return target

    def append_frame(self, dataset_name, frame, layer="clean", date_column="trade_date", partition_columns=None):
        """向 parquet 数据集追加分区文件，不做去重。"""
        target = self.layout.dataset_path(dataset_name, layer=layer)
        self._append_dataset(
            target,
            frame,
            date_column=date_column,
            partition_columns=partition_columns or self.DEFAULT_PARTITION_COLUMNS,
        )
        return target

    def upsert_frame(
        self,
        dataset_name,
        frame,
        dedupe_keys,
        layer="clean",
        sort_by=None,
        date_column="trade_date",
        partition_columns=None,
    ):
        """按主键去重后写回 parquet 数据集。"""
        existing = self.read_frame(dataset_name, layer=layer)
        combined = pd.concat([existing, frame], ignore_index=True) if not existing.empty else frame.copy()
        if sort_by:
            combined.sort_values(sort_by, inplace=True)
        combined.drop_duplicates(subset=dedupe_keys, keep="last", inplace=True)
        combined.reset_index(drop=True, inplace=True)
        target = self.layout.dataset_path(dataset_name, layer=layer)
        self._overwrite_dataset(
            target,
            combined,
            date_column=date_column,
            partition_columns=partition_columns or self.DEFAULT_PARTITION_COLUMNS,
        )
        return target

    def compact_dataset(
        self,
        dataset_name,
        dedupe_keys,
        sort_by=None,
        layer="clean",
        partition_columns=None,
    ):
        """使用 DuckDB 对整个数据集去重压实。"""
        if not self.dataset_exists(dataset_name, layer=layer):
            return self.layout.dataset_path(dataset_name, layer=layer)

        dataset_path = self.layout.dataset_path(dataset_name, layer=layer)
        temp_dir = dataset_path.parent / f".{dataset_path.name}_compact_{uuid.uuid4().hex}"
        if temp_dir.exists():
            shutil.rmtree(temp_dir)

        dataset_glob = self.layout.dataset_glob(dataset_name, layer=layer)
        dataset_glob_sql = dataset_glob.replace("'", "''")
        temp_dir_sql = str(temp_dir).replace("'", "''")
        effective_partition_columns = partition_columns or self.DEFAULT_PARTITION_COLUMNS
        partition_sql = ", ".join(effective_partition_columns)
        partition_by_sql = ", ".join(dedupe_keys)
        order_sql = ", ".join(
            [f"{column} DESC" for column in (sort_by or [])]
        ) or ", ".join([f"{column} DESC" for column in dedupe_keys])

        conn = duckdb.connect(database=":memory:")
        try:
            conn.execute(
                f"""
                COPY (
                    SELECT * EXCLUDE (rn)
                    FROM (
                        SELECT *,
                               ROW_NUMBER() OVER (
                                   PARTITION BY {partition_by_sql}
                                   ORDER BY {order_sql}
                               ) AS rn
                        FROM read_parquet('{dataset_glob_sql}', hive_partitioning = true)
                    )
                    WHERE rn = 1
                ) TO '{temp_dir_sql}' (
                    FORMAT PARQUET,
                    PARTITION_BY ({partition_sql})
                )
                """
            )
        finally:
            conn.close()

        backup_dir = dataset_path.parent / f".{dataset_path.name}_backup_{uuid.uuid4().hex}"
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        if dataset_path.exists():
            dataset_path.rename(backup_dir)
        temp_dir.rename(dataset_path)
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        return dataset_path

    def _build_query(self, dataset_name, layer, select_sql, filters=None, order_by=None, range_filters=None):
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

        for column, bounds in (range_filters or {}).items():
            if not bounds:
                continue
            lower = bounds.get("gte")
            if lower is not None:
                clauses.append(f"{column} >= ?")
                params.append(lower)
            upper = bounds.get("lte")
            if upper is not None:
                clauses.append(f"{column} <= ?")
                params.append(upper)

        query = f"SELECT {select_sql} FROM read_parquet(?, hive_partitioning = true)"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        if order_by:
            query += f" ORDER BY {order_by}"
        return query, params

    def _overwrite_dataset(self, dataset_dir, frame, date_column="trade_date", partition_columns=None):
        dataset_path = Path(dataset_dir)
        dataset_path.parent.mkdir(parents=True, exist_ok=True)

        if frame is None or frame.empty:
            if dataset_path.exists():
                shutil.rmtree(dataset_path)
            dataset_path.mkdir(parents=True, exist_ok=True)
            return

        prepared = frame.copy()
        effective_partition_columns = partition_columns or self.DEFAULT_PARTITION_COLUMNS
        prepared[date_column] = pd.to_datetime(prepared[date_column], errors="coerce")
        prepared.dropna(subset=[date_column], inplace=True)
        prepared["year"] = prepared[date_column].dt.year.astype("int32")

        temp_dir = dataset_path.parent / f".{dataset_path.name}_tmp"
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        if dataset_path.exists():
            shutil.rmtree(dataset_path)

        conn = duckdb.connect(database=":memory:")
        try:
            conn.register("frame_view", prepared)
            partition_sql = ", ".join(effective_partition_columns)
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

    def _append_dataset(self, dataset_dir, frame, date_column="trade_date", partition_columns=None):
        dataset_path = Path(dataset_dir)
        dataset_path.mkdir(parents=True, exist_ok=True)

        if frame is None or frame.empty:
            return

        prepared = frame.copy()
        effective_partition_columns = partition_columns or self.DEFAULT_PARTITION_COLUMNS
        prepared[date_column] = pd.to_datetime(prepared[date_column], errors="coerce")
        prepared.dropna(subset=[date_column], inplace=True)
        prepared["year"] = prepared[date_column].dt.year.astype("int32")
        if prepared.empty:
            return

        temp_dir = dataset_path.parent / f".{dataset_path.name}_append_{uuid.uuid4().hex}"
        conn = duckdb.connect(database=":memory:")
        try:
            conn.register("frame_view", prepared)
            partition_sql = ", ".join(effective_partition_columns)
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

        for parquet_file in temp_dir.rglob("*.parquet"):
            rel_path = parquet_file.relative_to(temp_dir)
            partition_dir = dataset_path / rel_path.parent
            partition_dir.mkdir(parents=True, exist_ok=True)
            target_file = partition_dir / f"part-{uuid.uuid4().hex}.parquet"
            parquet_file.rename(target_file)

        shutil.rmtree(temp_dir)
