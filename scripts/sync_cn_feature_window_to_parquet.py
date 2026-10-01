#!/usr/bin/env python
"""把 ClickHouse 中指定窗口/股票的 CN 特征补写进 Parquet feature 层（一次性对齐工具）。

用于 R1（CN 特征 Parquet 权威化）之前的差异回填：Parquet 与 ClickHouse 互为镜像时，
先用本脚本把“只在 ClickHouse 有”的行补进 Parquet，再启用权威标记。
默认 dry-run，只报告差异；--publish 才真正追加写入。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

from data.ingest.service import MarketDataService  # noqa: E402
from data.model import FEATURE_COLUMNS  # noqa: E402
from data.store.warehouse import MarketDataWarehouse  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", required=True, help="逗号分隔的股票代码")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--factor-set", default="alpha_zoo_hk")
    ap.add_argument("--publish", action="store_true")
    args = ap.parse_args()

    svc = MarketDataService(base_dir=str(ROOT / "assets/data"))
    ch = svc.warehouse.clickhouse_store
    assert ch is not None, "ClickHouse 不可用，无法作为回填源"
    pq = svc.warehouse.parquet_store
    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    out = {"codes": codes, "window": [args.start, args.end], "publish": bool(args.publish), "rows": {}}
    for code in codes:
        src = ch.read_frame("features", layer="feature",
                            filters={"market": "CN", "stock_code": [code], "feature_set": args.factor_set},
                            range_filters={"trade_date": {"gte": args.start, "lte": args.end}},
                            columns=list(FEATURE_COLUMNS))
        if src is None or src.empty:
            out["rows"][code] = 0
            continue
        exist = pq.read_frame("features", layer="feature",
                              filters={"market": "CN", "stock_code": [code], "feature_set": args.factor_set},
                              range_filters={"trade_date": {"gte": args.start, "lte": args.end}},
                              columns=["stock_code", "trade_date", "feature_name"])
        have = set(map(tuple, exist[["stock_code", "trade_date", "feature_name"]].itertuples(index=False, name=None))) if not exist.empty else set()
        src = src.copy()
        src["trade_date"] = pd.to_datetime(src["trade_date"]).dt.normalize()
        exist_dates = pd.to_datetime(exist["trade_date"]).dt.normalize().unique() if not exist.empty else []
        missing = src[~src["trade_date"].isin(exist_dates)] if len(exist_dates) else src
        out["rows"][code] = {"source_rows": int(len(src)), "parquet_dates": int(len(set(exist_dates))),
                             "to_write": int(len(missing))}
        print(f"{code}: ch_rows={len(src)} parquet_dates={len(set(exist_dates))} to_write={len(missing)}")
        if args.publish and not missing.empty:
            pq.append_frame("features", missing[list(FEATURE_COLUMNS)], layer="feature",
                            date_column="trade_date",
                            partition_columns=MarketDataWarehouse.FEATURES_PARTITION_COLUMNS)
    (ROOT / "output/verification/cn_base_data_repair_20260926" / ("feature_backfill.json" if args.publish else "feature_backfill.dryrun.json")).write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
