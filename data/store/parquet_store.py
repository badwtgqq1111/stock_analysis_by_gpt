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

    def distinct_counts_by_group(
        self,
        dataset_name,
        group_column,
        value_column,
        layer="clean",
        filters=None,
        range_filters=None,
    ):
        """Count distinct values per group without materializing the full dataset."""
        if not self.dataset_exists(dataset_name, layer=layer):
            return {}

        dataset_path = self.layout.dataset_path(dataset_name, layer=layer)
        dataset = ds.dataset(dataset_path, format="parquet", partitioning="hive")
        available_columns = set(dataset.schema.names)
        if group_column not in available_columns or value_column not in available_columns:
            return {}

        import pyarrow.compute as pc

        expression = None
        for column, value in (filters or {}).items():
            if value is None or column not in available_columns:
                continue
            if isinstance(value, (list, tuple, set)):
                values = [item for item in value if item is not None]
                if not values:
                    continue
                condition = pc.field(column).isin(values)
            else:
                condition = pc.field(column) == value
            expression = condition if expression is None else expression & condition

        for column, bounds in (range_filters or {}).items():
            if not bounds or column not in available_columns:
                continue
            lower = bounds.get("gte")
            upper = bounds.get("lte")
            if lower is not None:
                condition = pc.field(column) >= pc.scalar(pd.to_datetime(lower))
                expression = condition if expression is None else expression & condition
            if upper is not None:
                condition = pc.field(column) <= pc.scalar(pd.to_datetime(upper))
                expression = condition if expression is None else expression & condition

        group_ids_by_name = {}
        value_ids_by_name = {}
        values_by_group = {}
        scanner = dataset.scanner(
            columns=[group_column, value_column],
            filter=expression,
            batch_size=131_072,
        )
        for batch in scanner.to_batches():
            # Dictionary encoding keeps the row loop numeric.  Converting every
            # long-format factor name to a Python string here retained gigabytes
            # of temporary objects on a multi-million-row factor dataset.
            encoded_groups = pc.dictionary_encode(batch.column(0))
            encoded_values = pc.dictionary_encode(batch.column(1))
            local_groups = [
                group_ids_by_name.setdefault(str(value), len(group_ids_by_name))
                for value in encoded_groups.dictionary.to_pylist()
            ]
            local_values = [
                value_ids_by_name.setdefault(str(value), len(value_ids_by_name))
                for value in encoded_values.dictionary.to_pylist()
            ]
            group_indices = encoded_groups.indices.to_numpy(zero_copy_only=False)
            value_indices = encoded_values.indices.to_numpy(zero_copy_only=False)
            for group_index, value_index in zip(group_indices, value_indices):
                values_by_group.setdefault(local_groups[int(group_index)], set()).add(local_values[int(value_index)])
        group_names = {group_id: name for name, group_id in group_ids_by_name.items()}
        return {group_names[group_id]: len(values) for group_id, values in values_by_group.items()}

    def distinct_values_from_statistics(
        self,
        dataset_name,
        column,
        *,
        layer="clean",
        filters=None,
    ):
        """Return distinct values using Parquet row-group statistics first.

        Feature files are normally written in single-stock row groups, so
        ``min == max`` yields the exact instrument without reading factor
        values. Mixed or unstatisted row groups fall back to scanning only the
        requested column from that row group.
        """
        if not self.dataset_exists(dataset_name, layer=layer):
            return set()
        dataset_path = self.layout.dataset_path(dataset_name, layer=layer)
        dataset = ds.dataset(dataset_path, format="parquet", partitioning="hive")
        if column not in dataset.schema.names:
            return set()

        import pyarrow.compute as pc

        expression = None
        for filter_column, value in (filters or {}).items():
            if value is None or filter_column not in dataset.schema.names:
                continue
            if isinstance(value, (list, tuple, set)):
                values = [item for item in value if item is not None]
                if not values:
                    continue
                condition = pc.field(filter_column).isin(values)
            else:
                condition = pc.field(filter_column) == value
            expression = condition if expression is None else expression & condition

        distinct = set()
        for fragment in dataset.get_fragments(filter=expression):
            # Partition-only fields (for example ``market``) are not physical
            # parquet columns. They have already selected the fragment and
            # must not be rebound against the row-group schema here.
            for row_group_fragment in fragment.split_by_row_group():
                metadata = row_group_fragment.metadata
                names = metadata.schema.names if metadata is not None else []
                column_index = names.index(column) if column in names else None
                statistics = None
                if column_index is not None and metadata.num_row_groups == 1:
                    statistics = metadata.row_group(0).column(column_index).statistics
                if statistics is not None and statistics.has_min_max and statistics.min == statistics.max:
                    value = statistics.min
                    distinct.add(value.decode() if isinstance(value, bytes) else value)
                    continue
                table = row_group_fragment.to_table(columns=[column])
                distinct.update(value for value in pc.unique(table[column]).to_pylist() if value is not None)
        return distinct

    def group_count_and_max(
        self,
        dataset_name,
        group_column,
        count_column,
        max_column,
        layer="clean",
        filters=None,
        range_filters=None,
    ):
        """Return row counts and a max value per group using bounded batches."""
        if not self.dataset_exists(dataset_name, layer=layer):
            return {}, {}

        dataset_path = self.layout.dataset_path(dataset_name, layer=layer)
        dataset = ds.dataset(dataset_path, format="parquet", partitioning="hive")
        available_columns = set(dataset.schema.names)
        if not {group_column, count_column, max_column}.issubset(available_columns):
            return {}, {}
        import pyarrow.compute as pc

        expression = None
        for column, value in (filters or {}).items():
            if value is None or column not in available_columns:
                continue
            condition = pc.field(column).isin([item for item in value if item is not None]) if isinstance(value, (list, tuple, set)) else pc.field(column) == value
            expression = condition if expression is None else expression & condition
        for column, bounds in (range_filters or {}).items():
            if not bounds or column not in available_columns:
                continue
            if bounds.get("gte") is not None:
                condition = pc.field(column) >= pc.scalar(pd.to_datetime(bounds["gte"]))
                expression = condition if expression is None else expression & condition
            if bounds.get("lte") is not None:
                condition = pc.field(column) <= pc.scalar(pd.to_datetime(bounds["lte"]))
                expression = condition if expression is None else expression & condition

        counts = {}
        latest = {}
        scan_columns = list(dict.fromkeys([group_column, count_column, max_column]))
        scanner = dataset.scanner(columns=scan_columns, filter=expression, batch_size=131_072)
        for batch in scanner.to_batches():
            batch_table = pa.Table.from_batches([batch])
            grouped = batch_table.group_by(group_column).aggregate(
                [(count_column, "count"), (max_column, "max")]
            )
            group_values = grouped[group_column].to_pylist()
            batch_counts = grouped[f"{count_column}_count"].to_pylist()
            batch_latest = grouped[f"{max_column}_max"].to_pylist()
            for group, count, value in zip(group_values, batch_counts, batch_latest):
                if group is None:
                    continue
                key = str(group)
                counts[key] = counts.get(key, 0) + int(count or 0)
                if value is not None and (key not in latest or value > latest[key]):
                    latest[key] = value
        return counts, latest

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
