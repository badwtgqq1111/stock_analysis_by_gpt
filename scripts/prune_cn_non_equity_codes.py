#!/usr/bin/env python3
"""Drop non-equity codes (SSE indices, B shares, BJ) from a CN metadata dataset.

P1.19 §1: 128 SSE index codes (000001.SH = 上证指数 …) sit in the local layers
as if they were stocks.  The read paths (universe, clean panel, training frame)
now filter on ``is_cn_equity_code``, so models never see them again; this script
cleans the *stored* metadata so tooling that reads it directly is clean too.

Usage::

    uv run python scripts/prune_cn_non_equity_codes.py            # dry run
    uv run python scripts/prune_cn_non_equity_codes.py --apply    # rewrite in place

Every rewritten file is copied to ``<file>.bak-<timestamp>`` first, so the change
is reversible without re-downloading.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.ingest.service import is_cn_equity_code  # noqa: E402

DATASETS = {
    "stock_info": "assets/data/meta/stock_info_registry",
    "valuation": "assets/data/meta/valuation_snapshot",
    "financials": "assets/data/meta/financial_statement_metrics",
}


def _iter_parquet(root: Path):
    if not root.exists():
        return
    for path in sorted(root.rglob("*.parquet")):
        if ".bak-" in path.name:
            continue
        yield path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", choices=sorted(DATASETS), default="stock_info")
    parser.add_argument("--root", default=None, help="override the dataset root")
    parser.add_argument("--apply", action="store_true", help="rewrite files (default: dry run)")
    parser.add_argument("--keep", default=5, type=int, help="how many dropped codes to print")
    args = parser.parse_args()

    root = Path(args.root) if args.root else ROOT / DATASETS[args.dataset]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    total_rows = 0
    total_dropped = 0
    changed_files = 0
    samples: list[str] = []

    for path in _iter_parquet(root):
        table = pq.read_table(path)
        if "stock_code" not in table.column_names:
            continue
        codes = table.column("stock_code").to_pylist()
        mask = [is_cn_equity_code(code) for code in codes]
        total_rows += len(codes)
        dropped = [str(code) for code, keep in zip(codes, mask) if not keep]
        if not dropped:
            continue
        total_dropped += len(dropped)
        changed_files += 1
        samples.extend(dropped[: max(0, args.keep - len(samples))])
        action = "prune" if args.apply else "would prune"
        print(f"[{action}] {path.relative_to(ROOT)}: {len(dropped)}/{len(codes)} rows")
        if args.apply:
            backup = path.with_name(path.name + f".bak-{stamp}")
            shutil.copy2(path, backup)
            pruned = table.filter(pa.array(mask, type=pa.bool_()))
            tmp = path.with_name(path.name + ".tmp")
            pq.write_table(pruned, tmp, compression="snappy")
            tmp.replace(path)
            print(f"    backup -> {backup.relative_to(ROOT)}")

    print(
        f"\ndataset={args.dataset} root={root.relative_to(ROOT)} "
        f"rows={total_rows} non_equity_rows={total_dropped} files_changed={changed_files} "
        f"mode={'APPLY' if args.apply else 'DRY-RUN'}"
    )
    if samples:
        print("sample dropped codes:", ", ".join(samples))
    if not args.apply and total_dropped:
        print("rerun with --apply to write the change (backups are kept)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
