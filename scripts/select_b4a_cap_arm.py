#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""B4a-cap 选股：100 个准入特征 + 市值≥10亿 + 日额≥2000万 + 行业≤2。

模型：output/models/p2_81/b4a_cap/model.txt（LightGBM，100 特征）。
候选：最新 B4 panel 截面，按模型分排序，取 Top20，等权。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds
import lightgbm as lgb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PANEL = ROOT / "assets/data/derived/cn_b4_long_panel"
MODEL_DIR = ROOT / "output/models/p2_81/b4a_cap"
SIZE = ROOT / "assets/data/meta/cn_sleeve_features_long_v4.parquet"
SECTOR = ROOT / "assets/data/derived/cn_stock_sector_features.parquet"
BASIC = ROOT / "assets/data/meta/cn_stock_basic.parquet"
OHLCV = ROOT / "assets/data/clean/ohlcv/market=CN"
OUT = ROOT / "output/results_cn"
HISTORY = OUT / "b4a_cap_arm_history"


def latest_panel(date: str | None) -> tuple[pd.Timestamp, pd.DataFrame]:
    years = sorted(int(p.name.split("=")[1]) for p in PANEL.glob("year=*") if p.is_dir())
    if not years:
        raise SystemExit("缺 cn_b4_long_panel")
    year = pd.Timestamp(date).year if date else years[-1]
    files = sorted(str(p) for p in PANEL.glob(f"year={year}/*.parquet"))
    frame = ds.dataset(files, format="parquet").to_table().to_pandas()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.normalize()
    latest = pd.Timestamp(date) if date else frame["trade_date"].max()
    frame = frame[frame["trade_date"] == latest].drop_duplicates("stock_code", keep="last")
    return latest, frame


def size_latest(date: pd.Timestamp) -> pd.DataFrame:
    frame = pd.read_parquet(SIZE, columns=["trade_date", "stock_code", "mktcap"])
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.normalize()
    frame = frame[frame["trade_date"] <= date].sort_values("trade_date").groupby("stock_code").tail(1)
    return frame[["stock_code", "mktcap"]].drop_duplicates("stock_code")


def industry_latest(date: pd.Timestamp) -> pd.DataFrame:
    frame = pd.read_parquet(SECTOR, columns=["trade_date", "stock_code", "industry_l1"])
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.normalize()
    frame = frame[frame["trade_date"] <= date].sort_values("trade_date").groupby("stock_code").tail(1)
    return frame[["stock_code", "industry_l1"]].drop_duplicates("stock_code")


def amount_ma20(date: pd.Timestamp) -> pd.DataFrame:
    files = []
    for year in (date.year - 1, date.year):
        files.extend(str(p) for p in OHLCV.glob(
            f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={year}/*.parquet"))
    bars = ds.dataset(sorted(set(files)), format="parquet").to_table(
        columns=["trade_date", "stock_code", "amount"]).to_pandas()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"]).dt.normalize()
    bars["amount"] = pd.to_numeric(bars["amount"], errors="coerce")
    bars = bars.dropna(subset=["trade_date", "stock_code", "amount"]).sort_values(["stock_code", "trade_date"])
    bars["amount_ma20"] = bars.groupby("stock_code", sort=False)["amount"].transform(
        lambda s: s.rolling(20, min_periods=10).mean())
    return bars[bars["trade_date"] == date][["stock_code", "amount_ma20"]]


def main() -> int:
    ap = argparse.ArgumentParser(description="B4a-cap 选股")
    ap.add_argument("--as-of", default=None, help="YYYY-MM-DD；默认最新 B4 面板")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--min-mktcap", type=float, default=1_000_000_000)
    ap.add_argument("--min-amount", type=float, default=20_000_000)
    ap.add_argument("--max-per-industry", type=int, default=2)
    ap.add_argument("--print", action="store_true")
    args = ap.parse_args()

    manifest = json.loads((MODEL_DIR / "manifest.json").read_text(encoding="utf-8"))
    features = manifest["features"]
    model = lgb.Booster(model_file=str(MODEL_DIR / "model.txt"))
    date, panel = latest_panel(args.as_of)
    missing = [c for c in features if c not in panel.columns]
    if missing:
        raise SystemExit(f"B4 panel 缺特征：{missing[:10]}")
    panel["score"] = model.predict(panel[features])

    panel = panel.merge(size_latest(date), on="stock_code", how="left")
    panel = panel.merge(industry_latest(date), on="stock_code", how="left")
    panel = panel.merge(amount_ma20(date), on="stock_code", how="left")
    basic = pd.read_parquet(BASIC, columns=["ts_code", "name"]).drop_duplicates("ts_code")
    panel = panel.merge(basic, left_on="stock_code", right_on="ts_code", how="left").drop(columns=["ts_code"])

    panel = panel[
        panel["mktcap"].fillna(0).ge(args.min_mktcap)
        & panel["amount_ma20"].fillna(0).ge(args.min_amount)
        & panel["industry_l1"].notna()
        & ~panel["name"].fillna("").str.contains("ST|退", regex=True)
    ].copy()
    panel = panel.sort_values("score", ascending=False)
    picked, counts = [], {}
    for _, row in panel.iterrows():
        industry = str(row["industry_l1"])
        if counts.get(industry, 0) >= args.max_per_industry:
            continue
        picked.append(row)
        counts[industry] = counts.get(industry, 0) + 1
        if len(picked) >= args.top:
            break
    result = pd.DataFrame(picked)
    if result.empty:
        raise SystemExit("没有满足条件的股票")
    result["trade_date"] = str(date.date())
    result["board"] = "B4a-cap ML"
    result["rank"] = range(1, len(result) + 1)
    result["in_portfolio"] = True
    result["target_weight"] = 1.0 / len(result)
    result["lots_at_capital"] = 0
    result["planned_amount"] = 0
    cols = ["trade_date", "stock_code", "name", "board", "industry_l1", "score", "rank",
            "in_portfolio", "target_weight", "mktcap", "amount_ma20", "lots_at_capital", "planned_amount"]
    result = result[cols].rename(columns={"industry_l1": "industry"})
    OUT.mkdir(parents=True, exist_ok=True)
    HISTORY.mkdir(parents=True, exist_ok=True)
    result.to_csv(OUT / "b4a_cap_arm_selection.csv", index=False)
    result.to_csv(HISTORY / f"{date.date()}.csv", index=False)
    if args.print:
        print(result.to_string(index=False), flush=True)
    print(f"[b4a-cap] {date.date()} 选出 {len(result)} 只 -> {OUT / 'b4a_cap_arm_selection.csv'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
