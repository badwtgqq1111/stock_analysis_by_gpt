#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""从原始快照重建全历史 ``cn_moneyflow_features.parquet``。

``--stage moneyflow`` 只为“本次抓取到的日期”构建滚动特征，所以每日增量运行会把
``assets/data/derived/cn_moneyflow_features.parquet`` 覆盖成 1—2 天的数据；
clean panel 随即只剩 1—2 天覆盖，训练时的 5% 覆盖门槛再把它们全部丢掉
（2026-09-19 的 LightGBM 1,146 个特征里 ``moneyflow_*`` 为 0 个）。

本脚本用生产同款函数 ``build_moneyflow_features`` 对三个来源
（``moneyflow`` / ``moneyflow_dc`` / ``moneyflow_ths``）逐股票重算全窗口特征，
再与已有文件合并（新值优先），从而恢复完整历史。

用法::

    uv run python scripts/rebuild_cn_moneyflow_features.py \
        --start 2024-08-23 --end 2026-09-18
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.ingest.providers.cn_moneyflow import build_moneyflow_features  # noqa: E402

RAW = ROOT / "assets/data/raw/moneyflow_snapshots"
DEFAULT_OUTPUT = ROOT / "assets/data/derived/cn_moneyflow_features.parquet"

SOURCE_FILES = {
    "moneyflow": "moneyflow_2*.parquet",
    "moneyflow_dc": "moneyflow_dc_2*.parquet",
    "moneyflow_ths": "moneyflow_ths_2*.parquet",
}


def _read_source(source: str, start: str, end: str) -> pd.DataFrame:
    pattern = RAW / SOURCE_FILES[source]
    files = sorted(str(path) for path in pattern.parent.glob(pattern.name))
    if not files:
        raise FileNotFoundError(f"no raw snapshots for {source}")
    dataset = ds.dataset(files, format="parquet")
    columns = [name for name in dataset.schema.names if name not in {"name", "pct_change", "latest", "retrieved_at"}]
    if "retrieved_at" in dataset.schema.names:
        columns.append("retrieved_at")
    frame = dataset.to_table(
        columns=sorted(set(columns)),
        filter=(ds.field("trade_date") >= pd.Timestamp(start)) & (ds.field("trade_date") <= pd.Timestamp(end)),
    ).to_pandas()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame["stock_code"] = frame["stock_code"].astype(str)
    if "retrieved_at" in frame.columns:
        frame = frame.sort_values(["stock_code", "trade_date", "retrieved_at"])
        frame = frame.drop_duplicates(["stock_code", "trade_date"], keep="last")
        frame = frame.drop(columns=["retrieved_at"])
    return frame.sort_values(["stock_code", "trade_date"]).reset_index(drop=True)


def build_source_features(source: str, start: str, end: str) -> pd.DataFrame:
    frame = _read_source(source, start, end)
    parts = []
    for code, group in frame.groupby("stock_code", sort=False):
        feature = build_moneyflow_features(group, source=source)
        if feature is None or feature.empty:
            continue
        feature = feature.copy()
        feature["stock_code"] = code
        feature["market"] = "CN"
        feature["source"] = source
        parts.append(feature)
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild full-history moneyflow features")
    parser.add_argument("--start", default="2024-08-23")
    parser.add_argument("--end", default="2026-09-18")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--no-merge-existing", action="store_true")
    args = parser.parse_args()

    started = time.time()
    output = Path(args.output)
    parts = []
    summary: dict = {"sources": {}, "window": {"start": args.start, "end": args.end}}
    for source in SOURCE_FILES:
        source_started = time.time()
        features = build_source_features(source, args.start, args.end)
        if features.empty:
            summary["sources"][source] = {"rows": 0}
            continue
        summary["sources"][source] = {
            "rows": int(len(features)),
            "stocks": int(features["stock_code"].nunique()),
            "start": str(features["trade_date"].min().date()),
            "end": str(features["trade_date"].max().date()),
            "seconds": round(time.time() - source_started, 1),
        }
        parts.append(features)
        print(f"[rebuild] {source}: rows={len(features):,} stocks={features['stock_code'].nunique():,} "
              f"in {time.time() - source_started:.1f}s", flush=True)

    if not parts:
        raise SystemExit("no features were built")
    rebuilt = pd.concat(parts, ignore_index=True)

    existing = pd.DataFrame()
    if output.is_file() and not args.no_merge_existing:
        existing = pd.read_parquet(output)
        existing["trade_date"] = pd.to_datetime(existing["trade_date"], errors="coerce")
        existing["stock_code"] = existing["stock_code"].astype(str)
        summary["existing_rows"] = int(len(existing))

    combined = pd.concat([existing, rebuilt], ignore_index=True)
    keys = [column for column in ("stock_code", "trade_date", "source") if column in combined.columns]
    combined = combined.drop_duplicates(keys, keep="last").sort_values(["stock_code", "trade_date"]).reset_index(drop=True)

    backup = output.with_suffix(f".bak_{pd.Timestamp.now():%Y%m%d_%H%M%S}.parquet")
    if output.is_file():
        shutil.copy2(output, backup)
        summary["backup"] = str(backup.relative_to(ROOT))
    output.parent.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(output, index=False)

    summary.update(
        {
            "output": str(output.relative_to(ROOT)),
            "rows": int(len(combined)),
            "stocks": int(combined["stock_code"].nunique()),
            "dates": int(combined["trade_date"].nunique()),
            "start": str(combined["trade_date"].min().date()),
            "end": str(combined["trade_date"].max().date()),
            "feature_columns": int(len([c for c in combined.columns if c not in {"stock_code", "trade_date", "market", "source"}])),
            "seconds": round(time.time() - started, 1),
        }
    )
    report_path = ROOT / "output/research/moneyflow_features_rebuild_20260919.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    print(f"[rebuild] wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
