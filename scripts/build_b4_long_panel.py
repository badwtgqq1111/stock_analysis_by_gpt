#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从 feature store 物化 B4 所需的 100 个准入特征（长历史、紧凑宽表）。

不重建 683 列的完整 clean_panel（约 2.6 小时/70GB 峰值），而是只取
当前生产模型 manifest 里的 100 个特征，按年 pivot + 横截面 rank。
树模型对单调变换不敏感，rank 也避免不同因子量纲/极端值影响。

输出：
  assets/data/derived/cn_b4_long_panel/year=YYYY/part.parquet
  assets/data/derived/cn_b4_long_panel.manifest.json
"""
from __future__ import annotations

import json
import sys
import argparse
from datetime import datetime
from pathlib import Path

import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "output/models/cn/lightgbm/alpha_zoo_hk/model_manifest.json"
STORE = ROOT / "assets/data/feature/features/market=CN"
OUT = ROOT / "assets/data/derived/cn_b4_long_panel"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="", help="逗号分隔；空=2016-2026")
    args = ap.parse_args()
    years_wanted = ([int(x) for x in args.years.split(",") if x.strip()]
                    if args.years else list(range(2016, 2027)))
    features = json.loads(MANIFEST.read_text(encoding="utf-8"))["feature_columns"]
    raw_names = [c.removesuffix("_clean") for c in features]
    mapping = dict(zip(raw_names, features))
    print(f"[b4-panel] {len(raw_names)} features", flush=True)
    rows_total = 0
    years = []
    for year in years_wanted:
        files = [str(p) for p in STORE.glob(
            f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/feature_set=alpha_zoo_hk/"
            f"feature_version=0.1.0/feature_config_hash=236efa35ecb499f6/year={year}/*.parquet")]
        if not files:
            continue
        dataset = ds.dataset(files, format="parquet")
        filt = ds.field("feature_name").isin(raw_names)
        table = dataset.to_table(
            columns=["trade_date", "stock_code", "feature_name", "feature_value"],
            filter=filt).to_pandas()
        if table.empty:
            continue
        table["trade_date"] = pd.to_datetime(table["trade_date"]).dt.normalize()
        table["feature_value"] = pd.to_numeric(table["feature_value"], errors="coerce")
        table = table.drop_duplicates(["trade_date", "stock_code", "feature_name"], keep="last")
        wide = table.pivot(
            index=["trade_date", "stock_code"], columns="feature_name",
            values="feature_value")
        wide = wide.reindex(columns=raw_names)
        # 横截面 rank：树模型对单调变换不敏感，同时压掉量纲/极端值
        wide = wide.groupby(level="trade_date", sort=False).rank(pct=True) - 0.5
        wide = wide.rename(columns=mapping).reset_index()
        path = OUT / f"year={year}" / "part.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        wide.to_parquet(path, index=False)
        rows_total += len(wide)
        years.append(year)
        print(f"[b4-panel] {year}: {len(wide):,} rows", flush=True)
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "rows": rows_total, "years": years, "features": len(features),
        "path": str(OUT.relative_to(ROOT)),
        "features_list": features,
        "notes": [
            "从 feature store 只取当前模型 100 个准入特征，按年 pivot",
            "按 trade_date 做横截面 rank(pct)-0.5；树模型对单调变换不敏感",
            "不是 683 列完整 clean_panel；完整 clean_panel 仍可用 run_cn_pipeline 重建",
        ],
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[b4-panel] done rows={rows_total:,} years={years}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
