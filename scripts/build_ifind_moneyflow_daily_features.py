#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 iFinD 日频主力资金流向做成日频因子。

输入：
  assets/data/raw/ifind/market=CN/moneyflow/year=*/part.parquet
  assets/data/clean/ohlcv/...（日成交额，用于归一化）

输出：
  assets/data/derived/cn_ifind_moneyflow_daily_features.parquet
  assets/data/derived/cn_ifind_moneyflow_daily_features.manifest.json
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RAW_DIR = ROOT / "assets/data/raw/ifind/market=CN/moneyflow"
OHLCV = ROOT / "assets/data/clean/ohlcv/market=CN"
OUT = ROOT / "assets/data/derived/cn_ifind_moneyflow_daily_features.parquet"
MANIFEST = ROOT / "assets/data/derived/cn_ifind_moneyflow_daily_features.manifest.json"


def main() -> int:
    files = sorted(RAW_DIR.glob("year=*/part.parquet"))
    if not files:
        raise SystemExit(f"缺 {RAW_DIR}/year=*/part.parquet")
    raw = pd.concat([pd.read_parquet(p) for p in files], ignore_index=True)
    raw["trade_date"] = pd.to_datetime(raw["trade_date"]).dt.normalize()
    raw = raw.drop_duplicates(["trade_date", "stock_code"], keep="last")
    start, end = raw.trade_date.min(), raw.trade_date.max()
    print(f"[ifind-daily] raw {len(raw):,} 行 {start.date()} ~ {end.date()}", flush=True)

    bar_files = [str(p) for year in range(start.year, end.year + 1) for p in OHLCV.glob(
        f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={year}/*.parquet")]
    bars = ds.dataset(bar_files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "amount"]).to_pandas()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"], errors="coerce").dt.normalize()
    bars["amount"] = pd.to_numeric(bars["amount"], errors="coerce")
    bars = bars.dropna(subset=["trade_date", "stock_code", "amount"])
    bars = bars.drop_duplicates(["trade_date", "stock_code"], keep="last")
    frame = raw.merge(bars, on=["trade_date", "stock_code"], how="left")
    frame["mf_ifind_daily_ratio"] = frame["mainforce_net_inflow_yuan"] / frame["amount"].replace(0, np.nan)

    def _z(values: pd.Series) -> pd.Series:
        s = pd.to_numeric(values, errors="coerce")
        std = s.std(ddof=0)
        return (s - s.mean()) / std if std and std > 0 else pd.Series(0.0, index=s.index)

    frame["mf_ifind_daily_z"] = frame.groupby("trade_date", sort=False)["mf_ifind_daily_ratio"].transform(_z)
    frame["mf_ifind_daily_rank"] = frame.groupby("trade_date", sort=False)["mf_ifind_daily_ratio"].rank(pct=True)
    frame = frame.sort_values(["stock_code", "trade_date"])
    # 2016-2024 回补后，2025 可能是空的、2026 再接上；rolling 不能跨年缺口，
    # 否则 2026 的 5/20 日窗口会偷偷用 2024 的行。按 gap>5 天分段滚动。
    gap = frame.groupby("stock_code", sort=False)["trade_date"].diff().dt.days.fillna(0) > 5
    frame["_segment"] = gap.groupby(frame["stock_code"], sort=False).cumsum()
    by_stock = frame.groupby(["stock_code", "_segment"], sort=False)
    frame["mf_ifind_5d_sum"] = by_stock["mainforce_net_inflow_yuan"].transform(
        lambda s: s.rolling(5, min_periods=3).sum())
    frame["amount_5d"] = by_stock["amount"].transform(lambda s: s.rolling(5, min_periods=3).sum())
    frame["mf_ifind_5d_ratio"] = frame["mf_ifind_5d_sum"] / frame["amount_5d"].replace(0, np.nan)
    frame["mf_ifind_20d_sum"] = by_stock["mainforce_net_inflow_yuan"].transform(
        lambda s: s.rolling(20, min_periods=10).sum())
    frame["amount_20d"] = by_stock["amount"].transform(lambda s: s.rolling(20, min_periods=10).sum())
    frame["mf_ifind_20d_ratio"] = frame["mf_ifind_20d_sum"] / frame["amount_20d"].replace(0, np.nan)
    frame = frame.drop(columns=["amount_5d", "amount_20d"])

    keep = ["trade_date", "stock_code", "mainforce_net_inflow_yuan", "amount",
            "mf_ifind_daily_ratio", "mf_ifind_daily_z", "mf_ifind_daily_rank",
            "mf_ifind_5d_ratio", "mf_ifind_20d_ratio"]
    out = frame[keep].copy()
    for col in keep[2:]:
        out[col] = pd.to_numeric(out[col], errors="coerce").astype("float32")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(OUT, index=False)
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "rows": int(len(out)),
        "dates": int(out["trade_date"].nunique()),
        "stocks": int(out["stock_code"].nunique()),
        "start": str(out["trade_date"].min().date()),
        "end": str(out["trade_date"].max().date()),
        "columns": list(out.columns),
        "path": str(OUT.relative_to(ROOT)),
        "notes": [
            "iFinD 日频主力资金流向，按日成交额归一",
            "当前 raw 覆盖 2024-01-02 ~ 2024-08-22；可继续用 fetch_ifind_moneyflow_history.py 扩年",
            "与本地多源 moneyflow 口径不同，字段名单独加 mf_ifind_* 前缀",
        ],
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in ("rows", "dates", "stocks", "start", "end")},
                     ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
