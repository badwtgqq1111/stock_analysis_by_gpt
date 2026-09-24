#!/usr/bin/env python3
"""Remove only Parquet files listed by a verified CN daily cutover manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.migrate_cn_daily_clickhouse_to_parquet import MARKER_NAME


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rollback_cutover(*, manifest_path: Path, data_root: Path) -> dict:
    manifest_path = Path(manifest_path).resolve()
    data_root = Path(data_root).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not manifest.get("production_mutated"):
        raise ValueError("manifest records no production mutation")
    allowed_root = data_root / "clean" / "ohlcv"
    marker = data_root / "meta" / MARKER_NAME
    if marker.is_file():
        marker_payload = json.loads(marker.read_text(encoding="utf-8"))
        if Path(marker_payload.get("manifest", "")).resolve() != manifest_path:
            raise ValueError("cutover marker belongs to another manifest")
    checked = []
    for record in manifest.get("new_parquet_files", []):
        path = Path(record["path"]).resolve()
        if allowed_root not in path.parents or path.suffix != ".parquet":
            raise ValueError(f"refusing path outside CN OHLCV parquet root: {path}")
        if not path.is_file() or _sha256(path) != record["sha256"]:
            raise ValueError(f"refusing changed or missing parquet file: {path}")
        checked.append(path)
    removed = []
    for path in checked:
        path.unlink()
        removed.append(str(path))
    if marker.is_file():
        marker.unlink()
    return {"removed_files": removed, "marker_removed": not marker.exists()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--data-root", default="assets/data")
    args = parser.parse_args()
    result = rollback_cutover(manifest_path=Path(args.manifest), data_root=Path(args.data_root))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
