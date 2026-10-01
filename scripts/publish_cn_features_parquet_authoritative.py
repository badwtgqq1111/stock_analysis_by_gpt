#!/usr/bin/env python
"""启用 CN 特征 “Parquet 权威” 标记（R1）。

前置校验（默认抽样 20 只，跨交易所）：Parquet feature 层必须是 ClickHouse 的超集
（不存在“只有 ClickHouse 有”的 (股票, 交易日)）。不满足即中止，避免切换后丢历史。
默认 dry-run，--publish 写标记并输出清单。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

from data.ingest.service import MarketDataService  # noqa: E402

MARKER = ROOT / "assets/data/meta/cn_features_parquet_authoritative.json"
OUT = ROOT / "output/verification/cn_base_data_repair_20260926"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=20)
    ap.add_argument("--publish", action="store_true")
    args = ap.parse_args()

    svc = MarketDataService(base_dir=str(ROOT / "assets/data"))
    ch = svc.warehouse.clickhouse_store
    if ch is None:
        print("ClickHouse 不可用：无法做切换前校验")
        return 2

    codes = [str(c) for c in svc.warehouse.get_all_stock_codes(market="CN")]
    step = max(1, len(codes) // args.sample)
    sample = codes[::step][: args.sample]
    d = ds.dataset(str(ROOT / "assets/data/feature/features/market=CN"), format="parquet", partitioning="hive")

    report, offenders = [], []
    for code in sample:
        pq_dates = set(pd.to_datetime(d.to_table(columns=["trade_date"], filter=(ds.field("stock_code") == code)).to_pandas().trade_date).dt.normalize())
        ch_frame = ch.read_frame("features", layer="feature", filters={"market": "CN", "stock_code": [code]},
                                 columns=["trade_date"])
        ch_dates = set(pd.to_datetime(ch_frame["trade_date"]).dt.normalize()) if "trade_date" in getattr(ch_frame, "columns", []) else set()
        only_ch = sorted(ch_dates - pq_dates)
        report.append({"stock": code, "parquet_dates": len(pq_dates), "ch_dates": len(ch_dates),
                       "only_ch": len(only_ch), "only_ch_span": (f"{only_ch[0].date()}..{only_ch[-1].date()}" if only_ch else "")})
        if only_ch:
            offenders.append(code)

    print(pd.DataFrame(report).to_string(index=False))
    ok = not offenders
    print(f"\n抽样 {len(sample)} 只：Parquet 超集校验 {'通过' if ok else '不通过 -> ' + ','.join(offenders)}")
    payload = {"marker": "cn_features_parquet_authoritative",
               "purpose": "CN feature layer reads/writes use Parquet only (feature generation and clean-panel builder must agree)",
               "verified_at": pd.Timestamp.utcnow().tz_localize(None).isoformat(),
               "sample_size": len(sample), "sample": report, "offenders": offenders,
               "rollback": "delete assets/data/meta/cn_features_parquet_authoritative.json (list the file in the publish manifest)"}
    if args.publish and ok:
        MARKER.parent.mkdir(parents=True, exist_ok=True)
        MARKER.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        (OUT / "features_parquet_authoritative_manifest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print("published marker:", MARKER)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
