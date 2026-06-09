#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Parquet 数据存储抽象。"""

import shutil
from pathlib import Path
import uuid

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq


class ParquetDataStore:
    """基于 pyarrow/pandas 的分区 Parquet 数据集存储。"""

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

        frame = self._load_dataset_frame(
            dataset_name=dataset_name,
            layer=layer,
            filters=filters,
            range_filters=range_filters,
            columns=columns,
        )
        if "year" in frame.columns:
            frame.drop(columns=["year"], inplace=True)
        if order_by and not frame.empty:
            frame = self._sort_frame(frame, order_by)
        return frame

    def scalar_query(self, dataset_name, expression, layer="clean", filters=None, range_filters=None):
        """执行单值聚合查询。"""
        if not self.dataset_exists(dataset_name, layer=layer):
            return None

        frame = self._load_dataset_frame(dataset_name, layer=layer, filters=filters, range_filters=range_filters)
        return self._evaluate_scalar_expression(frame, expression)

    def values_query(self, dataset_name, column, layer="clean", filters=None, distinct=False, order_by=None, range_filters=None):
        """执行单列值查询。"""
        if not self.dataset_exists(dataset_name, layer=layer):
            return []

        frame = self._load_dataset_frame(
            dataset_name=dataset_name,
            layer=layer,
            filters=filters,
            range_filters=range_filters,
            columns=[column],
        )
        if frame.empty or column not in frame.columns:
            return []
        values = frame[column].dropna()
        if distinct:
            values = values.drop_duplicates()
        out = pd.DataFrame({"value": values})
        if order_by:
            out = self._sort_frame(out, order_by)
        return out["value"].tolist()

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
        """对整个数据集去重压实。"""
        if not self.dataset_exists(dataset_name, layer=layer):
            return self.layout.dataset_path(dataset_name, layer=layer)

        dataset_path = self.layout.dataset_path(dataset_name, layer=layer)
        temp_dir = dataset_path.parent / f".{dataset_path.name}_compact_{uuid.uuid4().hex}"
        if temp_dir.exists():
            shutil.rmtree(temp_dir)

        combined = self.read_frame(dataset_name, layer=layer)
        if sort_by:
            combined.sort_values(sort_by, inplace=True)
        combined.drop_duplicates(subset=dedupe_keys, keep="last", inplace=True)
        self._overwrite_dataset(
            temp_dir,
            combined,
            date_column=self._infer_date_column(combined),
            partition_columns=partition_columns or self.DEFAULT_PARTITION_COLUMNS,
        )

        backup_dir = dataset_path.parent / f".{dataset_path.name}_backup_{uuid.uuid4().hex}"
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        if dataset_path.exists():
            dataset_path.rename(backup_dir)
        temp_dir.rename(dataset_path)
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        return dataset_path

    def _load_dataset_frame(self, dataset_name, layer, filters=None, range_filters=None, columns=None):
        dataset_path = self.layout.dataset_path(dataset_name, layer=layer)
        if not self.dataset_exists(dataset_name, layer=layer):
            return pd.DataFrame(columns=columns or None)
        dataset = ds.dataset(dataset_path, format="parquet", partitioning="hive")
        requested_columns = list(columns or [])
        available_columns = set(dataset.schema.names)
        read_columns = [c for c in requested_columns if c in available_columns] or None

        # ---- PyArrow predicate pushdown ----
        # Build a pyarrow Expression so that only matching row groups
        # are read from disk.  This avoids loading the entire dataset
        # into memory and then filtering in pandas.
        import pyarrow.compute as pc

        pyarrow_expr = None
        for column, value in (filters or {}).items():
            if value is None or column not in available_columns:
                continue
            if isinstance(value, (list, tuple, set)):
                vals = [v for v in value if v is not None]
                if not vals:
                    continue
                cond = pc.field(column).isin(vals)
                pyarrow_expr = cond if pyarrow_expr is None else pyarrow_expr & cond
            else:
                cond = pc.field(column) == value
                pyarrow_expr = cond if pyarrow_expr is None else pyarrow_expr & cond

        # Range filters (e.g. trade_date between start and end).
        # Use pd.to_datetime for date strings so comparisons work across
        # timestamp columns without type errors.
        for column, rng in (range_filters or {}).items():
            if rng is None or column not in available_columns:
                continue
            gte = rng.get("gte")
            lte = rng.get("lte")
            if gte is not None:
                gte_ts = pd.to_datetime(gte)
                cond = pc.field(column) >= pc.scalar(gte_ts)
                pyarrow_expr = cond if pyarrow_expr is None else pyarrow_expr & cond
            if lte is not None:
                lte_ts = pd.to_datetime(lte)
                cond = pc.field(column) <= pc.scalar(lte_ts)
                pyarrow_expr = cond if pyarrow_expr is None else pyarrow_expr & cond

        if pyarrow_expr is not None:
            table = dataset.to_table(columns=read_columns, filter=pyarrow_expr)
        else:
            table = dataset.to_table(columns=read_columns)

        frame = table.to_pandas()
        for column in requested_columns:
            if column not in frame.columns:
                frame[column] = None
        if requested_columns:
            frame = frame[requested_columns]
        return frame

    @staticmethod
    def _apply_filters(frame, filters=None, range_filters=None):
        if frame is None or frame.empty:
            return pd.DataFrame() if frame is None else frame
        working = frame.copy()
        mask = pd.Series(True, index=working.index)
        for column, value in (filters or {}).items():
            if value is None or column not in working.columns:
                continue
            if isinstance(value, (list, tuple, set)):
                values = list(value)
                if not values:
                    continue
                mask &= working[column].isin(values)
            else:
                mask &= working[column] == value

        for column, bounds in (range_filters or {}).items():
            if not bounds or column not in working.columns:
                continue
            series = working[column]
            if pd.api.types.is_datetime64_any_dtype(series):
                lower = pd.to_datetime(bounds.get("gte")) if bounds.get("gte") is not None else None
                upper = pd.to_datetime(bounds.get("lte")) if bounds.get("lte") is not None else None
            else:
                lower = bounds.get("gte")
                upper = bounds.get("lte")
            if lower is not None:
                mask &= series >= lower
            if upper is not None:
                mask &= series <= upper
        return working.loc[mask].reset_index(drop=True)

    @staticmethod
    def _sort_frame(frame, order_by):
        if frame is None or frame.empty or not order_by:
            return frame
        columns = []
        ascending = []
        for part in str(order_by).split(","):
            bits = part.strip().split()
            if not bits:
                continue
            column = bits[0]
            if column not in frame.columns:
                continue
            columns.append(column)
            ascending.append(not (len(bits) > 1 and bits[1].upper() == "DESC"))
        if not columns:
            return frame
        return frame.sort_values(columns, ascending=ascending).reset_index(drop=True)

    @staticmethod
    def _evaluate_scalar_expression(frame, expression):
        if frame is None or frame.empty:
            return None
        expr = str(expression or "").strip()
        upper = expr.upper()
        if upper == "COUNT(*)":
            return len(frame)
        for func in ("MAX", "MIN", "COUNT"):
            prefix = f"{func}("
            if upper.startswith(prefix) and expr.endswith(")"):
                column = expr[len(prefix):-1].strip()
                if column not in frame.columns:
                    return None
                if func == "MAX":
                    return frame[column].max()
                if func == "MIN":
                    return frame[column].min()
                return int(frame[column].count())
        raise ValueError(f"Unsupported parquet scalar expression: {expression}")

    @staticmethod
    def _infer_date_column(frame):
        for column in ("trade_date", "event_date", "date"):
            if column in frame.columns:
                return column
        return "trade_date"

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

        self._write_partitioned_frame(prepared, temp_dir, effective_partition_columns)

        temp_dir.rename(dataset_path)

    def compute_rps_features(self, factor_set="qlib_alpha158",
                             windows=(5, 10, 20, 30, 60),
                             layer="feature",
                             progress_callback=None):
        """基于已有 ROC 因子计算横截面 RPS 排名（pandas 实现）。

        ROC{w} = close_past / close_today，值越低收益越高。
        """
        def _progress(message):
            if progress_callback is not None:
                progress_callback(message)

        roc_names = [f"ROC{w}" for w in windows]
        rps_names = [f"RPS_{w}" for w in windows]
        key_columns = [
            "market", "stock_code", "trade_date", "frequency", "adjust",
            "feature_set", "feature_version", "feature_config_hash", "feature_name",
        ]
        columns_to_read = [
            "trade_date", "stock_code", "market", "exchange", "asset_type",
            "frequency", "adjust", "feature_set", "feature_version",
            "feature_config_hash", "feature_name", "feature_value",
        ]
        _progress(f"rps reading source ROC rows windows={','.join(map(str, windows))}")
        roc_long = self.read_frame(
            "features", layer=layer,
            filters={"feature_set": factor_set, "feature_name": roc_names},
            columns=columns_to_read,
        )
        _progress(f"rps source rows={len(roc_long)}")
        if roc_long.empty:
            return 0

        _progress("rps reading existing RPS rows")
        existing_rps = self.read_frame(
            "features", layer=layer,
            filters={"feature_set": factor_set, "feature_name": rps_names},
            columns=key_columns,
        )
        _progress(f"rps existing rows={len(existing_rps)}")
        existing_keys_by_name = {}
        if not existing_rps.empty:
            existing_rps["trade_date"] = pd.to_datetime(existing_rps["trade_date"], errors="coerce")
            for feature_name, group in existing_rps.groupby("feature_name", sort=False):
                existing_keys_by_name[feature_name] = set(
                    group[key_columns].itertuples(index=False, name=None)
                )

        rows = []
        for feature_name, group in roc_long.groupby("feature_name", sort=False):
            w = int(feature_name.replace("ROC", ""))
            rps_name = f"RPS_{w}"
            group = group.dropna(subset=["feature_value"]).copy()
            if group.empty:
                continue
            source_rows = len(group)
            group["feature_value"] = (
                group.groupby("trade_date")["feature_value"]
                .rank(ascending=True, pct=True) * 100
            )
            group["feature_name"] = rps_name
            group["source"] = "rps"
            group["ingest_time"] = pd.Timestamp.utcnow()
            existing_keys = existing_keys_by_name.get(rps_name)
            if existing_keys:
                missing_mask = [
                    key not in existing_keys
                    for key in group[key_columns].itertuples(index=False, name=None)
                ]
                group = group.loc[missing_mask].copy()
            _progress(
                f"rps window={w} source_rows={source_rows} "
                f"missing_rows={len(group)} existing_rows={source_rows - len(group)}"
            )
            if group.empty:
                continue
            rows.append(group)

        if not rows:
            return 0

        result = pd.concat(rows, ignore_index=True)
        columns_to_write = [
            "trade_date", "stock_code", "market", "exchange", "asset_type",
            "frequency", "adjust", "feature_set", "feature_version",
            "feature_config_hash", "feature_name", "feature_value",
            "source", "ingest_time",
        ]
        result = result[[c for c in columns_to_write if c in result.columns]]
        self.append_frame(
            "features", result,
            layer=layer,
            date_column="trade_date",
            partition_columns=(
                "market", "exchange", "asset_type", "frequency", "adjust",
                "feature_set", "feature_version", "feature_config_hash", "year",
            ),
        )
        _progress(f"rps appended rows={len(result)}")
        return len(result)

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
        self._write_partitioned_frame(prepared, temp_dir, effective_partition_columns)

        for parquet_file in temp_dir.rglob("*.parquet"):
            rel_path = parquet_file.relative_to(temp_dir)
            partition_dir = dataset_path / rel_path.parent
            partition_dir.mkdir(parents=True, exist_ok=True)
            target_file = partition_dir / f"part-{uuid.uuid4().hex}.parquet"
            parquet_file.rename(target_file)

        shutil.rmtree(temp_dir)

    @staticmethod
    def _write_partitioned_frame(frame, target_dir, partition_columns):
        target_dir = Path(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        partition_cols = [column for column in (partition_columns or []) if column in frame.columns]
        table = pa.Table.from_pandas(frame, preserve_index=False)
        pq.write_to_dataset(
            table,
            root_path=str(target_dir),
            partition_cols=partition_cols,
            basename_template=f"part-{uuid.uuid4().hex}-{{i}}.parquet",
        )
