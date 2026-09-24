#!/usr/bin/env python3
"""Repair Tencent lot-unit CN daily OHLCV in *copies* of Parquet partitions.

This does not publish into the warehouse or rebuild downstream feature/model
artifacts.  Ambiguous rows are left unchanged and counted for manual review.
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

from data.ingest.service import is_cn_equity_code  # noqa: E402
from scripts.audit_cn_volume_units import classify_volume_units  # noqa: E402


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def repair_parquet_copy(source: Path, target: Path, *, start_date: str, end_date: str) -> dict:
    if source.resolve() == target.resolve():
        raise ValueError("source and target must differ")
    # Read the file itself, not the surrounding Hive partition: the file
    # already materializes `year`, which conflicts with Hive's dictionary
    # partition type when pq.read_table infers the parent directory.
    original = pq.ParquetFile(source).read()
    frame = original.to_pandas()
    # Hive partition keys live in the directory, not necessarily the file.
    partition = {
        part.split("=", 1)[0]: part.split("=", 1)[1]
        for part in source.parts if "=" in part
    }
    audit_frame = frame.copy()
    for name in ("market", "asset_type", "frequency", "adjust"):
        if name not in audit_frame.columns and name in partition:
            audit_frame[name] = partition[name]
    required = {"market", "asset_type", "frequency", "adjust", "source", "stock_code", "trade_date",
                "close", "volume", "amount", "vwap"}
    missing = required - set(audit_frame.columns)
    if missing:
        raise ValueError(f"missing OHLCV columns: {sorted(missing)}")
    dates = pd.to_datetime(audit_frame["trade_date"], errors="coerce")
    eligible = (
        audit_frame["market"].eq("CN") & audit_frame["asset_type"].eq("equity")
        & audit_frame["frequency"].eq("daily") & audit_frame["adjust"].eq("qfq")
        & audit_frame["source"].eq("tencent")
        & audit_frame["stock_code"].astype(str).map(is_cn_equity_code)
        & dates.between(pd.Timestamp(start_date), pd.Timestamp(end_date))
    )
    audit = classify_volume_units(frame.loc[eligible, ["close", "volume", "amount"]])
    lots = audit.index[audit["unit_class"] == "lots_100"]
    ambiguous = audit.index[audit["unit_class"] == "ambiguous"]
    repaired = frame.copy()
    repaired.loc[lots, "volume"] = pd.to_numeric(repaired.loc[lots, "volume"]) * 100.0
    repaired.loc[lots, "vwap"] = (
        pd.to_numeric(repaired.loc[lots, "amount"])
        / pd.to_numeric(repaired.loc[lots, "volume"])
    )
    assert repaired.drop(columns=["volume", "vwap"]).equals(frame.drop(columns=["volume", "vwap"]))
    target.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(repaired, schema=original.schema, preserve_index=False)
    pq.write_table(table, target, compression="snappy")
    checked = pq.ParquetFile(target).read().to_pandas()
    assert checked.drop(columns=["volume", "vwap"]).equals(frame.drop(columns=["volume", "vwap"]))
    assert len(checked) == len(frame)
    return {
        "source": str(source.resolve()), "target": str(target.resolve()),
        "source_sha256": _sha256(source), "target_sha256": _sha256(target),
        "rows": int(len(frame)), "eligible_rows": int(eligible.sum()),
        "lots_repaired": int(len(lots)), "ambiguous_untouched": int(len(ambiguous)),
        "other_fields_unchanged": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", required=True, help="Parquet partition file; repeatable")
    parser.add_argument("--output-dir", required=True, help="new directory outside the live partition")
    parser.add_argument("--start-date", default="2026-08-28")
    parser.add_argument("--end-date", default="2026-09-23")
    args = parser.parse_args()
    output = Path(args.output_dir).resolve()
    records = []
    for raw in args.source:
        source = Path(raw).resolve()
        # The 2026 SSE/SZSE partition files share a basename; preserve their
        # exchange/frequency/adjust/year path to avoid accidental collisions.
        target = (output / source.parents[4].name / source.parents[2].name
                  / source.parents[1].name / source.parents[0].name / source.name)
        if target.exists():
            raise FileExistsError(f"target already exists: {target}")
        records.append(repair_parquet_copy(source, target, start_date=args.start_date, end_date=args.end_date))
    summary = {"files": records, "lots_repaired": sum(r["lots_repaired"] for r in records),
               "ambiguous_untouched": sum(r["ambiguous_untouched"] for r in records)}
    output.mkdir(parents=True, exist_ok=True)
    (output / "repair_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({"lots_repaired": summary["lots_repaired"],
                      "ambiguous_untouched": summary["ambiguous_untouched"],
                      "files": len(records)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
