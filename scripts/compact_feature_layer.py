#!/usr/bin/env python
"""特征层压实（R10）：同一 (market, stock_code, trade_date, frequency, adjust,
feature_set, feature_version, feature_config_hash, feature_name) 只保留一行——
`ingest_time` 最新的那行，与读取侧（`sort by ingest_time, keep last`）完全一致。

背景：每日常规任务、修复脚本、对齐重算都会 append 新文件，同一个键最多累积到 9 行；
CN 日线特征层因此涨到 34GB / 54.1 亿行（其中大部分是历史副本）。压实不改变任何“最新值”，
只把被覆盖的旧副本删掉，从而让读取结果与存储内容一一对应。

默认 dry-run：只统计每个分区/year 的行数、唯一键数与可删除行数；
`--publish` 才写入（写入方式：先在同级目录产出新文件，再把旧目录整体改名为
`.<name>.previous-<timestamp>` 作为回滚点，最后把新文件就位）。

用法：
  PYTHONPATH=. .venv/bin/python scripts/compact_feature_layer.py --market CN --layer feature
  PYTHONPATH=. .venv/bin/python scripts/compact_feature_layer.py --market CN --layer feature --publish
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
KEYS = ["market", "stock_code", "trade_date", "frequency", "adjust",
        "feature_set", "feature_version", "feature_config_hash", "feature_name"]


def _list_parts(year_dir: Path) -> list[Path]:
    return sorted(year_dir.glob("*.parquet"))


def _dedupe_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    if not frames:
        return pd.DataFrame()
    frame = pd.concat(frames, ignore_index=True)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame = frame.sort_values(["ingest_time"])
    return frame.drop_duplicates(subset=KEYS, keep="last").reset_index(drop=True)


def _read_one(path: Path) -> pd.DataFrame:
    """单文件读取。

    注意：不能用 ``pq.read_table(path)``——路径里带 ``market=CN/...`` 这类 hive 片段时，
    pyarrow 会尝试推断分区列并与文件 schema 合并，字符串类型不一致就抛
    ``ArrowTypeError: Unable to merge: Field market has incompatible types``。
    ``ParquetFile`` 不做分区推断，正好绕开。
    """
    return pq.ParquetFile(path).read().to_pandas()


def _read_batch(files: list[Path]) -> pd.DataFrame:
    return _dedupe_frames([_read_one(path) for path in files])


def compact_year(year_dir: Path, *, work_root: Path, batch_files: int, publish: bool, log) -> dict:
    parts = _list_parts(year_dir)
    if not parts:
        return {"year_dir": str(year_dir), "parts": 0, "rows_before": 0, "rows_after": 0, "dropped": 0}
    work = work_root / year_dir.parent.name / year_dir.name
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)

    rows_before = 0
    batch_paths: list[Path] = []
    for start in range(0, len(parts), batch_files):
        chunk = parts[start:start + batch_files]
        frame = _read_batch(chunk)
        rows_before += int(sum(pq.ParquetFile(p).metadata.num_rows for p in chunk))
        out = work / f"batch_{start // batch_files:05d}.parquet"
        pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), out)
        batch_paths.append(out)
        log(f"  batch {start // batch_files + 1}: files={len(chunk)} rows={len(frame):,}")

    # 多轮归并
    round_no = 0
    while len(batch_paths) > 1:
        round_no += 1
        merged: list[Path] = []
        for start in range(0, len(batch_paths), 12):
            group = batch_paths[start:start + 12]
            if len(group) == 1:
                merged.append(group[0])
                continue
            frame = _dedupe_frames([_read_one(p) for p in group])
            out = work / f"merge_r{round_no}_{start:05d}.parquet"
            pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), out)
            merged.append(out)
        for path in batch_paths:
            if path not in merged and path.exists():
                path.unlink()
        batch_paths = merged
        log(f"  归并第 {round_no} 轮 -> {len(batch_paths)} 个文件")

    final = _read_one(batch_paths[0]) if batch_paths else pd.DataFrame()
    rows_after = int(len(final))
    duplicated = int(len(final) - final.drop_duplicates(subset=KEYS).shape[0]) if rows_after else 0
    result = {"year_dir": str(year_dir), "parts": len(parts), "rows_before": rows_before,
              "rows_after": rows_after, "dropped": rows_before - rows_after, "duplicate_keys": duplicated}
    if not publish:
        log(f"  DRY-RUN rows {rows_before:,} -> {rows_after:,} (可删 {rows_before - rows_after:,})")
        return result

    stamp = time.strftime("%Y%m%d_%H%M%S")
    previous = year_dir.with_name(f".{year_dir.name}.previous-{stamp}")
    shutil.move(str(year_dir), str(previous))
    year_dir.mkdir(parents=True, exist_ok=True)
    target = year_dir / f"part-compacted-{stamp}.parquet"
    pq.write_table(pa.Table.from_pandas(final, preserve_index=False), target)
    result["previous"] = str(previous)
    result["compacted_file"] = str(target)
    log(f"  PUBLISHED rows {rows_before:,} -> {rows_after:,} (previous -> {previous.name})")
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="CN")
    ap.add_argument("--layer", default="feature")
    ap.add_argument("--dataset", default="features")
    ap.add_argument("--batch-files", type=int, default=400)
    ap.add_argument("--publish", action="store_true")
    ap.add_argument("--limit-years", type=int, default=0, help="只处理最近 N 个 year 分区（调试用）")
    args = ap.parse_args()

    base = ROOT / "assets/data" / args.layer / args.dataset / f"market={args.market}"
    if not base.exists():
        print("no dataset at", base)
        return 2
    out_dir = ROOT / "output/verification/cn_base_data_repair_20260926"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / ("compact_feature_layer.log" if args.publish else "compact_feature_layer.dryrun.log")
    def log(message: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {message}"
        print(line, flush=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    year_dirs = sorted(base.glob("*/*/*/*/*/*/year=*"))
    if args.limit_years:
        year_dirs = year_dirs[-args.limit_years:]
    log(f"compact dataset={args.dataset} layer={args.layer} market={args.market} publish={args.publish} year_dirs={len(year_dirs)}")
    results = []
    work_root = ROOT / "output/verification/cn_base_data_repair_20260926/.compact_work"
    for year_dir in year_dirs:
        log(f"{year_dir.relative_to(base)}")
        results.append(compact_year(year_dir, work_root=work_root, batch_files=args.batch_files,
                                    publish=args.publish, log=log))
    summary = {
        "dataset": args.dataset, "layer": args.layer, "market": args.market, "publish": bool(args.publish),
        "year_dirs": len(year_dirs),
        "rows_before": sum(r["rows_before"] for r in results),
        "rows_after": sum(r["rows_after"] for r in results),
        "rows_dropped": sum(r["dropped"] for r in results),
        "duplicate_keys_after": sum(r.get("duplicate_keys", 0) for r in results),
        "details": results,
        "rollback": "rm -rf <year_dir> && mv <year_dir 同级的 .<name>.previous-<ts> <year_dir>（备份是同级隐藏目录，压实时整体改名保留）",
    }
    name = "compact_feature_layer.json" if args.publish else "compact_feature_layer.dryrun.json"
    (out_dir / name).write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    log(f"totals rows {summary['rows_before']:,} -> {summary['rows_after']:,} (dropped {summary['rows_dropped']:,})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
