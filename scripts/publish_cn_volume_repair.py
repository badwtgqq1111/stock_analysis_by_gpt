#!/usr/bin/env python3
"""Verify and publish Tencent CN daily lot-to-share repairs in Parquet.

Only rows classified unambiguously as 100-share lots are changed.  The live
files are replaced from verified copies; byte-identical originals remain in
the output directory.  A hash-guarded rollback works on live files or a test
copy and will not overwrite a file changed by a later pipeline run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import sys
import uuid
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.repair_cn_volume_units_copy import repair_parquet_copy  # noqa: E402


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read(path: Path) -> pd.DataFrame:
    return pq.ParquetFile(path).read().to_pandas()


def _replace_from_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def rollback(manifest_path: Path, *, target_root: Path | None = None) -> list[dict]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = Path(target_root or manifest["data_root"]).resolve()
    records = [row for row in manifest["files"] if row["lots_repaired"]]
    # Preflight every file before touching any: a later write must not be lost.
    for row in records:
        target = root / row["relative_path"]
        backup = manifest_path.parent / row["backup_path"]
        if not target.is_file() or sha256(target) != row["repaired_sha256"]:
            raise ValueError(f"modified hash mismatch: {target}")
        if not backup.is_file() or sha256(backup) != row["original_sha256"]:
            raise ValueError(f"backup hash mismatch: {backup}")
    for row in records:
        target = root / row["relative_path"]
        backup = manifest_path.parent / row["backup_path"]
        _replace_from_copy(backup, target)
        if sha256(target) != row["original_sha256"]:
            raise AssertionError(f"rollback verification failed: {target}")
    return records


def prepare_repair(*, data_root: Path, output_dir: Path, start_date: str,
                   end_date: str, publish: bool = False,
                   sources: list[Path] | None = None) -> dict:
    data_root = data_root.resolve()
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError(f"output already exists: {output_dir}")
    candidates = sources or sorted(data_root.glob(
        "market=CN/exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year=2026/*.parquet"
    ))
    if not candidates:
        raise ValueError("no CN qfq daily Parquet partition files")
    output_dir.mkdir(parents=True)
    rows = []
    differences = []
    for source in candidates:
        source = source.resolve()
        relative = source.relative_to(data_root)
        backup = output_dir / "ORIGINALS" / relative
        repaired = output_dir / "REPAIRED" / relative
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, backup)
        original_hash = sha256(source)
        if sha256(backup) != original_hash:
            raise AssertionError(f"backup hash mismatch: {source}")
        result = repair_parquet_copy(backup, repaired, start_date=start_date, end_date=end_date)
        before, after = _read(backup), _read(repaired)
        changed = pd.to_numeric(before["volume"]) != pd.to_numeric(after["volume"])
        if int(changed.sum()) != result["lots_repaired"]:
            raise AssertionError(f"row count mismatch: {source}")
        if not before.loc[~changed].equals(after.loc[~changed]):
            raise AssertionError(f"unselected row changed: {source}")
        if not before.drop(columns=["volume", "vwap"]).equals(after.drop(columns=["volume", "vwap"])):
            raise AssertionError(f"non-volume field changed: {source}")
        for index in before.index[changed]:
            differences.append({
                "file": str(relative), "stock_code": before.at[index, "stock_code"],
                "trade_date": str(before.at[index, "trade_date"])[:10],
                "volume_before": before.at[index, "volume"],
                "volume_after": after.at[index, "volume"],
                "vwap_before": before.at[index, "vwap"],
                "vwap_after": after.at[index, "vwap"],
            })
        rows.append({
            "relative_path": str(relative), "backup_path": str(backup.relative_to(output_dir)),
            "repaired_path": str(repaired.relative_to(output_dir)),
            "original_sha256": original_hash, "repaired_sha256": sha256(repaired),
            "rows": result["rows"], "lots_repaired": result["lots_repaired"],
            "ambiguous_untouched": result["ambiguous_untouched"],
        })
        if sha256(source) != original_hash:
            raise AssertionError(f"source changed during staging: {source}")

    manifest = {
        "changed_field": "CN qfq daily Tencent volume: 100-share lots to shares; vwap recomputed",
        "data_root": str(data_root), "start_date": start_date, "end_date": end_date,
        "files": rows, "lots_repaired": sum(r["lots_repaired"] for r in rows),
        "ambiguous_untouched": sum(r["ambiguous_untouched"] for r in rows),
        "published": False,
    }
    diff_path = output_dir / "DIFF_FILE.csv"
    pd.DataFrame(differences, columns=["file", "stock_code", "trade_date", "volume_before",
                                       "volume_after", "vwap_before", "vwap_after"]).to_csv(diff_path, index=False)
    manifest_path = output_dir / "MODIFIED_FILE.json"
    rollback_path = output_dir / "ROLLBACK.sh"
    rollback_path.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        f"if [[ $# -gt 0 ]]; then exec {shlex.quote(sys.executable)} "
        f"{shlex.quote(str(Path(__file__).resolve()))} "
        f"--rollback-manifest {shlex.quote(str(manifest_path))} --target-root \"$1\"; fi\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} "
        f"--rollback-manifest {shlex.quote(str(manifest_path))}\n",
        encoding="utf-8",
    )
    rollback_path.chmod(0o755)

    if publish:
        changed_rows = [row for row in rows if row["lots_repaired"]]
        for row in changed_rows:
            live = data_root / row["relative_path"]
            if sha256(live) != row["original_sha256"]:
                raise ValueError(f"live hash changed since staging: {live}")
        installed = []
        try:
            for row in changed_rows:
                live = data_root / row["relative_path"]
                _replace_from_copy(output_dir / row["repaired_path"], live)
                installed.append(row)
                if sha256(live) != row["repaired_sha256"]:
                    raise AssertionError(f"publish hash mismatch: {live}")
            manifest["published"] = True
        except BaseException:
            for row in reversed(installed):
                _replace_from_copy(output_dir / row["backup_path"], data_root / row["relative_path"])
            raise
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default="assets/data/clean/ohlcv")
    parser.add_argument("--output-dir")
    parser.add_argument("--start-date", default="2026-08-28")
    parser.add_argument("--end-date", default="2026-09-23")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--rollback-manifest")
    parser.add_argument("--target-root")
    args = parser.parse_args()
    if args.rollback_manifest:
        restored = rollback(Path(args.rollback_manifest), target_root=Path(args.target_root) if args.target_root else None)
        print(json.dumps({"restored_files": len(restored), "restored_rows": sum(r["lots_repaired"] for r in restored)}, sort_keys=True))
        return 0
    if not args.output_dir:
        parser.error("--output-dir is required for a repair")
    result = prepare_repair(data_root=Path(args.data_root), output_dir=Path(args.output_dir),
                            start_date=args.start_date, end_date=args.end_date, publish=args.publish)
    print(json.dumps({key: result[key] for key in ("lots_repaired", "ambiguous_untouched", "published")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
