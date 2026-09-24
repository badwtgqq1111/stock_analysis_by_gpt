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


RPS_ALGORITHM_SOURCE = "rps.v2"
RPS_SCOPE_COLUMNS = (
    "market", "asset_type", "frequency", "adjust",
    "feature_set", "feature_version", "feature_config_hash", "trade_date",
)


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

    def iter_frames_by_group_batches(
        self,
        dataset_name,
        *,
        group_column,
        group_values,
        batch_size,
        layer="clean",
        filters=None,
        range_filters=None,
        columns=None,
    ):
        """Yield filtered frames using a one-time Parquet row-group index.

        Feature data is partitioned by market/materialization rather than stock.
        Recreating a dataset scanner for each small stock batch makes PyArrow
        visit every fragment repeatedly.  Factor files are normally written in
        single-stock row groups, so their min/max statistics provide a compact
        and exact index for the common case.  Row groups without usable
        statistics remain in a fallback list and are scanned normally, which
        preserves correctness for externally written datasets.
        """
        requested_values = list(dict.fromkeys(str(value) for value in group_values if value is not None))
        if not requested_values or not self.dataset_exists(dataset_name, layer=layer):
            return

        dataset_path = self.layout.dataset_path(dataset_name, layer=layer)
        dataset = ds.dataset(dataset_path, format="parquet", partitioning="hive")
        available_columns = set(dataset.schema.names)
        if group_column not in available_columns:
            for start in range(0, len(requested_values), max(1, int(batch_size))):
                values = requested_values[start:start + max(1, int(batch_size))]
                yield values, pd.DataFrame(columns=columns or None)
            return

        import pyarrow.compute as pc

        requested_set = set(requested_values)
        requested_columns = list(columns or [])
        read_columns = [column for column in requested_columns if column in available_columns] or None
        normalized_range_filters = {
            column: {
                "gte": pd.to_datetime(bounds["gte"]).to_pydatetime() if bounds.get("gte") is not None else None,
                "lte": pd.to_datetime(bounds["lte"]).to_pydatetime() if bounds.get("lte") is not None else None,
            }
            for column, bounds in (range_filters or {}).items()
            if bounds
        }

        def _range_overlaps(statistics, bounds):
            if not bounds:
                return True
            if statistics is None or not statistics.has_min_max:
                return True
            lower = bounds.get("gte")
            upper = bounds.get("lte")
            try:
                if lower is not None and statistics.max < lower:
                    return False
                if upper is not None and statistics.min > upper:
                    return False
            except TypeError:
                return True
            return True

        def _path_partitions(path):
            return {
                key: value
                for part in path.parts
                if "=" in part
                for key, value in [part.split("=", 1)]
            }

        def _matches_partition(path):
            partitions = _path_partitions(path)
            for column, value in (filters or {}).items():
                if column == group_column or value is None or column not in partitions:
                    continue
                values = value if isinstance(value, (list, tuple, set)) else [value]
                if str(partitions[column]) not in {str(item) for item in values if item is not None}:
                    return False
            return True

        # ``Dataset.get_fragments().split_by_row_group()`` constructs a Python
        # fragment object for every row group. On the CN feature store that is
        # over 200k objects and made the supposed optimization slower than a
        # scan. Read Parquet footer metadata directly, then open only selected
        # row groups during each batch.
        indexed_candidates = {value: [] for value in requested_values}
        latest_ingest_by_value = {}
        fallback_row_groups = {}
        for path in dataset_path.rglob("*.parquet"):
            if not _matches_partition(path):
                continue
            metadata = pq.ParquetFile(path).metadata
            names = metadata.schema.names
            if group_column not in names:
                continue
            group_index = names.index(group_column)
            ingest_index = names.index("ingest_time") if "ingest_time" in names else None
            range_indexes = {
                column: names.index(column)
                for column in normalized_range_filters
                if column in names
            }
            for row_group_index in range(metadata.num_row_groups):
                row_group = metadata.row_group(row_group_index)
                if any(
                    not _range_overlaps(row_group.column(column_index).statistics, bounds)
                    for column, bounds in normalized_range_filters.items()
                    if column in range_indexes
                    for column_index in [range_indexes[column]]
                ):
                    continue
                statistics = row_group.column(group_index).statistics
                if statistics is not None and statistics.has_min_max and statistics.min == statistics.max:
                    value = statistics.min.decode() if isinstance(statistics.min, bytes) else str(statistics.min)
                    if value in requested_set:
                        ingest_statistics = row_group.column(ingest_index).statistics if ingest_index is not None else None
                        ingest_value = ingest_statistics.max if ingest_statistics and ingest_statistics.has_min_max else None
                        # Feature writers use UTC. Compare the normalized
                        # footer representation directly: constructing a
                        # pandas Timestamp for every row group is costly on a
                        # 200k+ row-group store.
                        ingest_key = ingest_value.replace(tzinfo=None).isoformat() if ingest_value is not None else None
                        indexed_candidates[value].append((path, row_group_index, ingest_key))
                        if ingest_key is not None:
                            latest_ingest_by_value[value] = max(latest_ingest_by_value.get(value, ingest_key), ingest_key)
                else:
                    fallback_row_groups.setdefault(path, []).append(row_group_index)

        indexed_row_groups = {value: {} for value in requested_values}
        for value, candidates in indexed_candidates.items():
            latest_ingest = latest_ingest_by_value.get(value)
            for path, row_group_index, ingest_key in candidates:
                # Feature materialization is append-only. A rerun writes a
                # complete replacement for the same stock, so older row
                # groups are superseded by the latest ingest snapshot.
                if latest_ingest is not None and ingest_key != latest_ingest:
                    continue
                indexed_row_groups[value].setdefault(path, []).append(row_group_index)

        normalized_batch_size = max(1, int(batch_size))
        for start in range(0, len(requested_values), normalized_batch_size):
            values = requested_values[start:start + normalized_batch_size]
            # A fallback row group has no exact stock statistic. It may only
            # contribute data for an instrument absent from all indexed parts;
            # otherwise it is an older append-only materialization superseded
            # by the newest indexed ingest above. Reading it for every batch
            # repeatedly loaded tens of millions of stale feature rows.
            missing_index_values = [value for value in values if not indexed_row_groups[value]]
            row_groups_by_path = (
                {path: list(indices) for path, indices in fallback_row_groups.items()}
                if missing_index_values else {}
            )
            for value in values:
                for path, indices in indexed_row_groups[value].items():
                    row_groups_by_path.setdefault(path, []).extend(indices)
            if row_groups_by_path:
                # Older feature parts were written with a naive ingest_time,
                # while newer parts use UTC-aware timestamps.  Normalize at
                # the pandas boundary so mixed historical schemas can be
                # concatenated without Arrow rejecting the batch.
                frames = []
                for path, row_group_ids in row_groups_by_path.items():
                    table = pq.ParquetFile(path).read_row_groups(row_group_ids, columns=read_columns)
                    part = table.to_pandas()
                    part = part[part[group_column].astype(str).isin(values)]
                    for column, bounds in (range_filters or {}).items():
                        if column in part.columns and bounds:
                            if bounds.get("gte") is not None:
                                part = part[part[column] >= pd.to_datetime(bounds["gte"])]
                            if bounds.get("lte") is not None:
                                part = part[part[column] <= pd.to_datetime(bounds["lte"])]
                    if "ingest_time" in part.columns:
                        timestamps = pd.to_datetime(part["ingest_time"], errors="coerce", utc=True)
                        part["ingest_time"] = timestamps.dt.tz_localize(None)
                    if not part.empty:
                        frames.append(part)
                frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=read_columns or None)
            else:
                frame = pd.DataFrame(columns=read_columns or None)
            for column in requested_columns:
                if column not in frame.columns:
                    frame[column] = None
            if requested_columns:
                frame = frame[requested_columns]
            yield values, frame

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
            metadata = fragment.metadata
            names = metadata.schema.names if metadata is not None else []
            column_index = names.index(column) if column in names else None
            for row_group_info in fragment.row_groups:
                statistics = (
                    metadata.row_group(row_group_info.id).column(column_index).statistics
                    if column_index is not None else None
                )
                if statistics is not None and statistics.has_min_max and statistics.min == statistics.max:
                    value = statistics.min
                    distinct.add(value.decode() if isinstance(value, bytes) else value)
                    continue
                table = fragment.subset(row_group_ids=[row_group_info.id]).to_table(columns=[column])
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
        # Harmonise datetime resolution before concatenating: partitions written
        # by an older pandas/pyarrow carry second-resolution timestamps while new
        # frames carry microseconds, and pandas then refuses to merge the two
        # ("incompatible merge keys [0] dtype('<M8[us]') and dtype('<M8[s]')").
        for column in (date_column, "ingest_time"):
            for side in (existing, frame):
                if side is not None and not side.empty and column in side.columns:
                    values = pd.to_datetime(side[column], errors="coerce", utc=True)
                    side[column] = values.dt.tz_convert(None).astype("datetime64[us]")
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
        """计算按完整数据口径隔离、可增量修订的横截面 RPS。

        ``ROC{w} = close[t-w] / close[t]``，数值越低代表区间收益越高，
        所以最低 ROC 的 RPS 为 100。每个市场、资产、频率、复权、特征版本和
        配置哈希各自独立排名；当前横截面会与最新 ``rps.v2`` 输出逐股比较，
        有差异时重算并追加整个横截面。
        """
        def _progress(message):
            if progress_callback is not None:
                progress_callback(message)

        roc_names = [f"ROC{w}" for w in windows]
        rps_names = [f"RPS_{w}" for w in windows]
        # ``feature_name`` is deliberately absent: the source is ``ROC{w}``,
        # while the materialized value is named ``RPS_{w}``.
        observation_key_columns = [*RPS_SCOPE_COLUMNS[:-1], "stock_code", "trade_date"]
        columns_to_read = [
            "trade_date", "stock_code", "market", "exchange", "asset_type",
            "frequency", "adjust", "feature_set", "feature_version",
            "feature_config_hash", "feature_name", "feature_value", "ingest_time",
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
            columns=[*observation_key_columns, "feature_name", "feature_value", "source", "ingest_time"],
        )
        _progress(f"rps existing rows={len(existing_rps)}")
        existing_by_name = {}
        if not existing_rps.empty:
            existing_rps["trade_date"] = pd.to_datetime(existing_rps["trade_date"], errors="coerce")
            existing_rps["ingest_time"] = pd.to_datetime(existing_rps["ingest_time"], errors="coerce")
            existing_rps = existing_rps[existing_rps.get("source", "").astype(str) == RPS_ALGORITHM_SOURCE]
            for feature_name, group in existing_rps.groupby("feature_name", sort=False):
                # The feature store is append-only.  Only the latest materialized
                # RPS observation per stock is relevant for the current state.
                existing_by_name[feature_name] = group.sort_values("ingest_time").drop_duplicates(
                    subset=observation_key_columns, keep="last",
                )

        rows = []
        for feature_name, group in roc_long.groupby("feature_name", sort=False):
            w = int(feature_name.replace("ROC", ""))
            rps_name = f"RPS_{w}"
            group = group.dropna(subset=["feature_value"]).copy()
            if group.empty:
                continue
            group["trade_date"] = pd.to_datetime(group["trade_date"], errors="coerce")
            group["ingest_time"] = pd.to_datetime(group["ingest_time"], errors="coerce")
            group = group.dropna(subset=["trade_date"]).sort_values("ingest_time")
            group = group.drop_duplicates(subset=observation_key_columns, keep="last")
            if group.empty:
                continue
            source_rows = len(group)
            scope_columns = list(RPS_SCOPE_COLUMNS)
            rank = group.groupby(scope_columns, dropna=False)["feature_value"].rank(
                ascending=True, method="min",
            )
            count = group.groupby(scope_columns, dropna=False)["feature_value"].transform("count")
            group["rps_value"] = (count - rank + 1.0) / count * 100.0

            existing = existing_by_name.get(rps_name)
            if existing is None:
                dirty_scopes = group.set_index(scope_columns).index.unique()
            else:
                comparison = group[[*observation_key_columns, "rps_value"]].merge(
                    existing[[*observation_key_columns, "feature_value"]].rename(
                        columns={"feature_value": "existing_rps_value"},
                    ),
                    on=observation_key_columns,
                    how="left",
                )
                comparison["matches"] = (
                    comparison["existing_rps_value"].notna()
                    & comparison["rps_value"].sub(comparison["existing_rps_value"]).abs().le(1e-12)
                )
                scope_matches = comparison.groupby(scope_columns, dropna=False)["matches"].all()
                dirty_scopes = scope_matches.index[
                    ~scope_matches
                ]
            if len(dirty_scopes) == 0:
                _progress(f"rps window={w} source_rows={source_rows} dirty_scopes=0")
                continue
            dirty = pd.DataFrame(list(dirty_scopes), columns=scope_columns)
            group = group.merge(dirty, on=scope_columns, how="inner")
            group["feature_value"] = group["rps_value"]
            group["feature_name"] = rps_name
            group["source"] = RPS_ALGORITHM_SOURCE
            group["ingest_time"] = pd.Timestamp.now("UTC").tz_localize(None)
            _progress(
                f"rps window={w} source_rows={source_rows} "
                f"dirty_scopes={len(dirty_scopes)} recomputed_rows={len(group)}"
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
