#!/usr/bin/env python3
"""Recompute the rolling money-flow features for the tail of the feature file.

The daily fetch path builds rolling features from the slice it just downloaded,
so appended dates carried a one-row window: on 2026-09-21 and 2026-09-22 every
``moneyflow_*_net_z_*d`` column was NaN (0% coverage vs 31% before) while
``moneyflow_net_Nd`` collapsed to the daily amount.  Downstream that disabled the
flow tilt, the second-tier gate and the flow confirmation of the volume-breakout
filter.

This script recomputes the trailing ``--sessions`` sessions per stock/source from
the intact per-day amounts and writes the file back (or to ``--output``).  A
``.bak-<timestamp>`` copy is written next to the target unless ``--no-backup``.

Usage:
  python scripts/repair_cn_moneyflow_features.py                 # dry run on production path
  python scripts/repair_cn_moneyflow_features.py --apply         # repair in place (with backup)
  python scripts/repair_cn_moneyflow_features.py --apply --output /tmp/repaired.parquet
"""
from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.ingest.service import rebuild_moneyflow_rolling_tail  # noqa: E402

DEFAULT_FEATURE_PATH = ROOT / "assets/data/derived/cn_moneyflow_features.parquet"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--feature-path", type=Path, default=DEFAULT_FEATURE_PATH)
    parser.add_argument("--output", type=Path, default=None, help="write here instead of in place")
    parser.add_argument("--sessions", type=int, default=90, help="trailing sessions recomputed per stock")
    parser.add_argument("--apply", action="store_true", help="write the repaired file (default: dry run)")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()

    source = args.feature_path.resolve()
    if not source.is_file():
        print(f"feature file not found: {source}")
        return 2
    frame = pd.read_parquet(source)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    sessions = sorted(frame["trade_date"].dropna().unique())[-int(args.sessions):]
    tail_dates = sessions[-3:]
    print(f"rows={len(frame):,} sessions={len(sessions)} tail={[str(pd.Timestamp(d).date()) for d in tail_dates]}")

    def coverage(data, dates):
        subset = data.loc[data["trade_date"].isin(dates)]
        values = pd.to_numeric(subset["moneyflow_net_z_5d"], errors="coerce")
        return float(values.notna().mean()) if len(subset) else float("nan")

    print("coverage before:", {str(pd.Timestamp(d).date()): round(coverage(frame, [d]), 4) for d in tail_dates})
    repaired = rebuild_moneyflow_rolling_tail(frame, dates=pd.to_datetime(list(tail_dates)), sessions=int(args.sessions))
    print("coverage after :", {str(pd.Timestamp(d).date()): round(coverage(repaired, [d]), 4) for d in tail_dates})
    if not args.apply:
        print("dry run: nothing written (pass --apply)")
        return 0
    target = (args.output or source).resolve()
    if target == source and not args.no_backup:
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = source.with_suffix(f".parquet.bak-{stamp}")
        shutil.copy2(source, backup)
        print(f"backup: {backup}")
    repaired.to_parquet(target, index=False)
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
