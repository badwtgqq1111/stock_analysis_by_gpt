#!/usr/bin/env python3
"""One-time, verified cutover of missing CN daily bars into Parquet.

Daily CN OHLCV becomes Parquet-authoritative only after ``--publish`` has
written and verified all selected ClickHouse-only keys.  This script does not
delete ClickHouse; its output records exactly which new Parquet files a
rollback must remove.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.ingest.service import MarketDataService  # noqa: E402


KEYS = ["market", "stock_code", "trade_date", "frequency", "adjust"]
COMPARE_COLUMNS = [
    "market", "exchange", "asset_type", "frequency", "adjust", "stock_code", "trade_date",
    "open", "high", "low", "close", "volume", "amount", "turnover", "vwap", "source",
]
MARKER_NAME = "cn_daily_parquet_authoritative.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalise(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    if result.empty and "trade_date" not in result:
        return result
    result["trade_date"] = pd.to_datetime(result["trade_date"], errors="coerce").dt.normalize()
    if "ingest_time" in result:
        result["ingest_time"] = pd.to_datetime(result["ingest_time"], errors="coerce")
    return result


def select_missing_cn_daily(source: pd.DataFrame, existing: pd.DataFrame) -> pd.DataFrame:
    """Keep the newest source version for keys absent from Parquet only."""
    source = _normalise(source)
    existing = _normalise(existing)
    if source.empty:
        raise ValueError("ClickHouse has no selected CN daily bars")
    if source[KEYS].isna().any().any() or source["trade_date"].isna().any():
        raise ValueError("ClickHouse source has invalid natural keys")
    order = KEYS + (["ingest_time"] if "ingest_time" in source else [])
    source = source.sort_values(order, kind="stable").drop_duplicates(KEYS, keep="last")
    if "source" not in source or source["source"].astype(str).str.lower().eq("unknown").any():
        raise ValueError("ClickHouse source contains unknown synthetic bars")
    existing_keys = existing[KEYS].drop_duplicates() if not existing.empty else pd.DataFrame(columns=KEYS)
    selected = source.merge(existing_keys, on=KEYS, how="left", indicator=True)
    selected = selected.loc[selected["_merge"].eq("left_only")].drop(columns=["_merge"])
    if selected.empty:
        raise ValueError("no ClickHouse-only CN daily keys to migrate")
    return selected.sort_values(KEYS, kind="stable").reset_index(drop=True)


def _new_parquet_files(root: Path, before: set[Path]) -> list[Path]:
    return sorted(path.resolve() for path in root.rglob("*.parquet") if path.resolve() not in before)


def migrate_cn_daily(*, clickhouse_store, parquet_store, dataset_name: str, data_root: Path,
                     start_date: str, end_date: str, output_dir: Path, publish: bool) -> dict:
    """Snapshot, optionally publish, then verify the selected cutover frame."""
    output_dir = Path(output_dir).resolve()
    if output_dir.exists():
        raise FileExistsError(f"output already exists: {output_dir}")
    filters = {"market": "CN", "asset_type": "equity", "frequency": "daily", "adjust": "qfq"}
    interval = {"trade_date": {"gte": start_date, "lte": end_date}}
    source = clickhouse_store.read_frame(dataset_name=dataset_name, layer="clean", filters=filters,
                                         range_filters=interval, order_by="stock_code, trade_date")
    existing = parquet_store.read_frame(dataset_name=dataset_name, layer="clean",
                                        filters={**filters, "year": list(range(pd.Timestamp(start_date).year, pd.Timestamp(end_date).year + 1))},
                                        range_filters=interval, order_by="stock_code, trade_date")
    selected = select_missing_cn_daily(source, existing)
    output_dir.mkdir(parents=True)
    snapshot_path = output_dir / "SOURCE_SNAPSHOT.parquet"
    pq.write_table(pa.Table.from_pandas(selected, preserve_index=False), snapshot_path, compression="snappy")
    result = {
        "start_date": start_date, "end_date": end_date, "selected_rows": int(len(selected)),
        "selected_stocks": int(selected["stock_code"].nunique()), "publish": bool(publish),
        "source_snapshot": str(snapshot_path), "source_snapshot_sha256": _sha256(snapshot_path),
        "production_mutated": False, "new_parquet_files": [],
    }
    if not publish:
        (output_dir / "manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    dataset_root = Path(data_root).resolve() / "clean" / dataset_name
    before = {path.resolve() for path in dataset_root.rglob("*.parquet")}
    parquet_store.append_frame(dataset_name=dataset_name, frame=selected, layer="clean")
    new_files = _new_parquet_files(dataset_root, before)
    if not new_files:
        raise AssertionError("Parquet publish created no files")
    actual = parquet_store.read_frame(dataset_name=dataset_name, layer="clean", filters=filters,
                                      range_filters=interval, order_by="stock_code, trade_date")
    actual = _normalise(actual).merge(selected[KEYS], on=KEYS, how="inner")
    expected = _normalise(selected)
    if len(actual) != len(expected) or actual.duplicated(KEYS).any():
        raise AssertionError("published Parquet keys do not match selected ClickHouse keys")
    for column in [name for name in COMPARE_COLUMNS if name in expected and name in actual]:
        left = expected.sort_values(KEYS)[column].reset_index(drop=True)
        right = actual.sort_values(KEYS)[column].reset_index(drop=True)
        if pd.api.types.is_numeric_dtype(left) or pd.api.types.is_numeric_dtype(right):
            if not pd.Series(left).equals(pd.Series(right)):
                raise AssertionError(f"published Parquet differs in {column}")
        elif not left.astype(str).equals(right.astype(str)):
            raise AssertionError(f"published Parquet differs in {column}")
    result["production_mutated"] = True
    result["new_parquet_files"] = [{"path": str(path), "sha256": _sha256(path)} for path in new_files]
    marker = Path(data_root).resolve() / "meta" / MARKER_NAME
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"market": "CN", "frequency": "daily", "authoritative_store": "parquet",
                                  "cutover_start": start_date, "cutover_end": end_date,
                                  "manifest": str((output_dir / "manifest.json").resolve())},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    result["cutover_marker"] = str(marker)
    (output_dir / "manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", default="2026-09-23")
    parser.add_argument("--end-date", default="2026-09-23")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--publish", action="store_true", help="append verified missing keys and enable Parquet authority")
    args = parser.parse_args()
    service = MarketDataService()
    if service.warehouse.clickhouse_store is None:
        raise RuntimeError("ClickHouse backend is unavailable")
    result = migrate_cn_daily(clickhouse_store=service.warehouse.clickhouse_store,
                              parquet_store=service.warehouse.parquet_store,
                              dataset_name=service.warehouse.OHLCV_DATASET,
                              data_root=service.warehouse.layout.base_path,
                              start_date=args.start_date, end_date=args.end_date,
                              output_dir=Path(args.output_dir), publish=args.publish)
    print(json.dumps({key: result[key] for key in ("selected_rows", "selected_stocks", "publish", "production_mutated")},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
