#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""面板 v3：用**真市值**替换掉"行业内市值分位"，把 35.7% 覆盖的 size 变成 ~全样本覆盖。

数据来源与构造（都可复核）：
  1. `assets/data/raw/daily_basic_relay/month=YYYY-MM.parquet`（本轮已完成回补：105 个月末、
     `total_mv/circ_mv/total_share/float_share/free_share/turnover_rate` 非空率 100%）；
  2. 日线 qfq 收盘价 + 成交量（`assets/data/clean/ohlcv/...`）；
  3. 构造：**日度市值 = 日收盘价 × 最近一个月末的总股本**（股本变动不频繁，用 merge_asof 前向填充），
     再在每个月末用真实 `total_mv` 校准（相对误差 >20% 视为期间股本变动，直接用真实值覆盖）；
  4. 派生：`log_mktcap / mktcap_rank（全市场升序分位，越小越"小市值"）/ circ_mktcap / circ_ratio /
     turnover_daily（成交量 ÷ 总股本）/ turnover_rank`。

为什么这么做：原面板的 `size_ind_pct` 只有 35.7% 覆盖（2018 年 31.8%），导致小市值域每月只剩 ~445 只，
而策略的域本应是"市场上最小的一批票"。v3 把市值覆盖率拉到 ~95%+，域内只数显著变大。

用法:
  uv run python scripts/panel_extend_v3.py
输出:
  assets/data/meta/cn_sleeve_features_long_v3.parquet + .manifest.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
META = ROOT / "assets/data/meta"
V2 = META / "cn_sleeve_features_long_v2.parquet"
BARS = ROOT / "assets/data/clean/ohlcv/market=CN"
BASIC = ROOT / "assets/data/raw/daily_basic_relay"
OUT = META / "cn_sleeve_features_long_v3.parquet"


def load_bars(start: str) -> pd.DataFrame:
    import pyarrow.dataset as ds
    years = range(int(start[:4]), 2027)
    files = [str(p) for y in years
             for p in BARS.glob(f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={y}/*.parquet")]
    table = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "close", "volume"]).to_pandas()
    table["trade_date"] = pd.to_datetime(table["trade_date"])
    return table


def main() -> int:
    ap = argparse.ArgumentParser(description="面板 v3：真市值")
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    panel = pd.read_parquet(V2)
    panel["trade_date"] = pd.to_datetime(panel["trade_date"])
    panel = panel[panel["trade_date"] >= pd.Timestamp(args.start)].copy()
    print(f"[v3] v2 面板 {len(panel):,} 行 / {panel['stock_code'].nunique()} 只 / {panel.shape[1]} 列", flush=True)

    bars = load_bars(args.start).rename(columns={"close": "close_px", "volume": "volume_raw"})
    bars = bars[bars["trade_date"] >= pd.Timestamp(args.start)]
    print(f"[v3] 日线 {len(bars):,} 行 / {bars['stock_code'].nunique()} 只", flush=True)
    panel = panel.merge(bars, on=["trade_date", "stock_code"], how="left")

    files = sorted(BASIC.glob("month=*.parquet"))
    if not files:
        raise SystemExit("daily_basic_relay 为空：先跑 scripts/backfill_cn_daily_basic_relay.py")
    db = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    db["trade_date"] = pd.to_datetime(db["trade_date"])
    db = db.rename(columns={"ts_code": "stock_code"})
    for col in ("total_share", "float_share", "free_share", "total_mv", "circ_mv", "turnover_rate"):
        if col in db.columns:
            db[col] = pd.to_numeric(db[col], errors="coerce") * (10_000.0 if "share" in col or col.endswith("_mv") else 1.0)
    print(f"[v3] daily_basic {len(db):,} 行 / {db['trade_date'].nunique()} 个月末 / "
          f"total_mv 非空 {db['total_mv'].notna().mean():.1%}", flush=True)

    shares = (db[["trade_date", "stock_code", "total_share", "float_share", "total_mv", "circ_mv"]]
              .dropna(subset=["total_share"]).sort_values("trade_date")
              .drop_duplicates(["trade_date", "stock_code"], keep="last"))
    panel = panel.sort_values(["trade_date", "stock_code"])
    panel = pd.merge_asof(panel, shares, on="trade_date", by="stock_code", direction="backward",
                          suffixes=("", "_db"))

    panel["mktcap"] = panel["close_px"] * panel["total_share"]        # 日度重构市值
    # 月末用真实市值校准：相对误差 >20% 视为期间股本变动 → 用真实值
    drift = (panel["mktcap"] / panel["total_mv"] - 1).abs()
    use_real = panel["total_mv"].notna() & (drift > 0.20)
    panel.loc[use_real, "mktcap"] = panel.loc[use_real, "total_mv"]
    panel["circ_mktcap"] = np.where(panel["circ_mv"].notna() & (drift > 0.20), panel["circ_mv"],
                                    panel["close_px"] * panel["float_share"])
    panel["circ_ratio"] = panel["circ_mktcap"] / panel["mktcap"]
    panel["turnover_daily"] = panel["volume_raw"] / panel["total_share"]

    # 全市场（当日）分位：越小越"小市值"
    panel["mktcap_rank"] = panel.groupby("trade_date")["mktcap"].rank(pct=True)
    panel["log_mktcap"] = np.log(panel["mktcap"].clip(lower=1.0))
    panel["turnover_rank"] = panel.groupby("trade_date")["turnover_daily"].rank(pct=True)

    coverage = {}
    for col in ("mktcap", "log_mktcap", "mktcap_rank", "circ_mktcap", "circ_ratio",
                "turnover_daily", "turnover_rank"):
        coverage[col] = float(panel[col].notna().mean())
    # 校准质量：月末重构值 vs 真实 total_mv 的中位绝对误差
    both = panel[panel["total_mv"].notna() & panel["close_px"].notna()].copy()
    both["recon"] = both["close_px"] * both["total_share"]
    err = (both["recon"] / both["total_mv"] - 1).abs()
    err = err.replace([np.inf, -np.inf], np.nan).dropna()

    print(f"[v3] 覆盖：{ {k: round(v, 4) for k, v in coverage.items()} }", flush=True)
    print(f"[v3] 月末重构误差：中位 {err.median():.2%}，P90 {err.quantile(0.9):.2%}，"
          f"用真实值覆盖 {int(use_real.sum()):,} 行", flush=True)
    print(f"[v3] 小市值覆盖（mktcap 非空）下，rank≤0.4 的月均只数："
          f"{int(panel[panel['mktcap_rank'] <= 0.4].groupby('trade_date').size().mean())}", flush=True)

    out = Path(args.out)
    keep_drop = ["close_px", "volume_raw", "total_share", "float_share", "total_mv", "circ_mv"]
    panel.drop(columns=[c for c in keep_drop if c in panel.columns]).to_parquet(out, index=False)
    fields = [c for c in panel.columns if c not in ("trade_date", "stock_code") and c not in keep_drop]
    (out.with_suffix(".manifest.json")).write_text(json.dumps({
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rows": int(len(panel)), "stocks": int(panel["stock_code"].nunique()),
        "date_range": [str(panel["trade_date"].min().date()), str(panel["trade_date"].max().date())],
        "n_fields": len(fields), "fields_new": ["mktcap", "log_mktcap", "mktcap_rank", "circ_mktcap",
                                               "circ_ratio", "turnover_daily", "turnover_rank"],
        "coverage": {k: round(v, 4) for k, v in coverage.items()},
        "reconstruction": {"method": "日收盘 × 最近月末总股本，月末用真实 total_mv 校准（误差>20% 覆盖）",
                           "median_abs_err_vs_total_mv": round(float(err.median()), 4),
                           "p90_abs_err_vs_total_mv": round(float(err.quantile(0.9)), 4),
                           "rows_overridden": int(use_real.sum())},
        "sources": {"v2": str(V2), "bars": str(BARS), "daily_basic": str(BASIC)},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[v3] 写出 {out}：{len(panel):,} 行 / {len(panel.columns)} 列（新增 7 列市值与换手）", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
