#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 iFinD 月度主力资金流向做成可用的月度因子。

输入：
  assets/data/raw/ifind/market=CN/moneyflow_monthly/part.parquet
  assets/data/clean/ohlcv/...（月度成交额，用于归一化）

输出：
  assets/data/derived/cn_ifind_moneyflow_monthly_features.parquet
  assets/data/derived/cn_ifind_moneyflow_monthly_features.manifest.json

因子：
  mf_monthly_ratio   = 月度主力净流入 / 月度成交额
  mf_monthly_z        = 当月横截面 z-score
  mf_monthly_rank     = 当月横截面 pct rank
  mf_3m_sum           = 近 3 个月主力净流入合计（元）
  mf_3m_ratio         = 近 3 个月净流入 / 近 3 个月成交额
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

RAW = ROOT / "assets/data/raw/ifind/market=CN/moneyflow_monthly/part.parquet"
OHLCV = ROOT / "assets/data/clean/ohlcv/market=CN"
OUT = ROOT / "assets/data/derived/cn_ifind_moneyflow_monthly_features.parquet"
MANIFEST = ROOT / "assets/data/derived/cn_ifind_moneyflow_monthly_features.manifest.json"


def main() -> int:
    if not RAW.exists():
        raise SystemExit(f"缺 {RAW}；先跑 scripts/fetch_ifind_moneyflow_monthly.py")
    raw = pd.read_parquet(RAW)
    raw["period"] = raw["period"].astype(str)
    raw["period_end"] = pd.to_datetime(raw["period_end"]).dt.normalize()

    files = [str(p) for year in range(2016, 2027) for p in OHLCV.glob(
        f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={year}/*.parquet")]
    bars = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "amount"]).to_pandas()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"], errors="coerce").dt.normalize()
    bars["amount"] = pd.to_numeric(bars["amount"], errors="coerce")
    bars = bars.dropna(subset=["trade_date", "stock_code", "amount"])
    bars["period"] = bars["trade_date"].dt.to_period("M").astype(str)
    month_amount = bars.groupby(["period", "stock_code"], as_index=False)["amount"].sum().rename(
        columns={"amount": "month_amount"})
    frame = raw.merge(month_amount, on=["period", "stock_code"], how="left")
    frame["mf_monthly_ratio"] = frame["mainforce_net_inflow_yuan"] / frame["month_amount"].replace(0, np.nan)

    def _z(values: pd.Series) -> pd.Series:
        s = pd.to_numeric(values, errors="coerce")
        std = s.std(ddof=0)
        return (s - s.mean()) / std if std and std > 0 else pd.Series(0.0, index=s.index)

    frame["mf_monthly_z"] = frame.groupby("period", sort=False)["mf_monthly_ratio"].transform(_z)
    frame["mf_monthly_rank"] = frame.groupby("period", sort=False)["mf_monthly_ratio"].rank(pct=True)
    frame = frame.sort_values(["stock_code", "period"])
    by_stock = frame.groupby("stock_code", sort=False)
    frame["mf_3m_sum"] = by_stock["mainforce_net_inflow_yuan"].transform(
        lambda s: s.rolling(3, min_periods=2).sum())
    frame["amount_3m"] = by_stock["month_amount"].transform(
        lambda s: s.rolling(3, min_periods=2).sum())
    frame["mf_3m_ratio"] = frame["mf_3m_sum"] / frame["amount_3m"].replace(0, np.nan)
    frame = frame.drop(columns=["amount_3m"])

    keep = ["period", "period_start", "period_end", "stock_code", "name",
            "mainforce_net_inflow_yuan", "month_amount", "mf_monthly_ratio",
            "mf_monthly_z", "mf_monthly_rank", "mf_3m_sum", "mf_3m_ratio", "source"]
    out = frame[keep].copy()
    for col in ("mainforce_net_inflow_yuan", "month_amount", "mf_monthly_ratio",
                "mf_monthly_z", "mf_monthly_rank", "mf_3m_sum", "mf_3m_ratio"):
        out[col] = pd.to_numeric(out[col], errors="coerce").astype("float32")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(OUT, index=False)
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "rows": int(len(out)),
        "periods": int(out["period"].nunique()),
        "stocks": int(out["stock_code"].nunique()),
        "start": str(out["period"].min()), "end": str(out["period"].max()),
        "columns": list(out.columns),
        "path": str(OUT.relative_to(ROOT)),
        "notes": [
            "月度主力净流入来自 iFinD smart_stock_picking 区间查询",
            "mf_monthly_ratio 用月度成交额归一，避免大票金额天然更大",
            "月度数据不能替代日频资金流；日频仍以 2024-08 起的本地多源表为准",
        ],
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in ("rows", "periods", "stocks", "start", "end")},
                     ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
