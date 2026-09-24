#!/usr/bin/env python3
"""Snapshot CN ClickHouse daily bars and repair Tencent lot units in a copy.

Neither the ClickHouse table nor the Parquet warehouse is written. An existing
output directory is rejected so that a previous audit cannot be overwritten.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.ingest.service import MarketDataService, is_cn_equity_code  # noqa: E402
from scripts.audit_cn_volume_units import classify_volume_units  # noqa: E402
from scripts.repair_cn_volume_units_copy import repair_parquet_copy  # noqa: E402


def _unit_counts(frame: pd.DataFrame) -> dict[str, int]:
    eligible = frame["source"].eq("tencent") & frame["stock_code"].astype(str).map(is_cn_equity_code)
    classes = classify_volume_units(frame.loc[eligible, ["close", "volume", "amount"]])["unit_class"]
    return {str(key): int(value) for key, value in classes.value_counts().items()}


def snapshot_and_repair_copy(store, dataset_name: str, output_dir: Path, *,
                             start_date: str, end_date: str) -> dict:
    """Read one backend explicitly; preserve every row and refuse duplicate keys."""
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError(f"output already exists: {output_dir}")
    frame = store.read_frame(
        dataset_name=dataset_name, layer="clean",
        filters={"market": "CN", "asset_type": "equity", "frequency": "daily", "adjust": "qfq"},
        range_filters={"trade_date": {"gte": start_date, "lte": end_date}},
        order_by="stock_code, trade_date",
    )
    if frame is None or frame.empty:
        raise ValueError("no ClickHouse CN daily bars in requested interval")
    required = {"market", "asset_type", "frequency", "adjust", "source", "stock_code",
                "trade_date", "close", "volume", "amount", "vwap"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"missing OHLCV columns: {sorted(missing)}")
    keys = ["market", "stock_code", "trade_date", "frequency", "adjust"]
    duplicate_count = int(frame.duplicated(keys).sum())
    if duplicate_count:
        raise ValueError(f"ClickHouse snapshot has {duplicate_count} duplicate natural keys")
    dates = pd.to_datetime(frame["trade_date"], errors="coerce")
    if dates.isna().any() or not dates.between(pd.Timestamp(start_date), pd.Timestamp(end_date)).all():
        raise ValueError("ClickHouse snapshot has out-of-range or invalid trade dates")

    output_dir.mkdir(parents=True)
    source = output_dir / "SOURCE_SNAPSHOT.parquet"
    target = output_dir / "REPAIRED_SNAPSHOT.parquet"
    pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), source, compression="snappy")
    repair = repair_parquet_copy(source, target, start_date=start_date, end_date=end_date)
    checked = pq.ParquetFile(target).read().to_pandas()
    before = _unit_counts(frame)
    after = _unit_counts(checked)
    if after.get("lots_100", 0):
        raise AssertionError("lot-unit rows remain in repaired snapshot")
    if repair["lots_repaired"] != before.get("lots_100", 0):
        raise AssertionError("repaired row count differs from snapshot audit")
    manifest = {
        "backend": "clickhouse", "start_date": start_date, "end_date": end_date,
        "rows": int(len(frame)), "unique_stocks": int(frame["stock_code"].nunique()),
        "unit_counts_before": before, "unit_counts_after": after,
        "repair": repair,
        "production_mutated": False,
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", default="2026-09-23")
    parser.add_argument("--end-date", default="2026-09-23")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    service = MarketDataService()
    store = service.warehouse.clickhouse_store
    if store is None:
        raise RuntimeError("ClickHouse backend is unavailable")
    result = snapshot_and_repair_copy(
        store, service.warehouse.OHLCV_DATASET, Path(args.output_dir),
        start_date=args.start_date, end_date=args.end_date,
    )
    print(json.dumps({"rows": result["rows"], "before": result["unit_counts_before"],
                      "after": result["unit_counts_after"], "repaired": result["repair"]["lots_repaired"]},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
