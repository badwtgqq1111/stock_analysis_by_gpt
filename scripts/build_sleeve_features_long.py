#!/usr/bin/env python3
"""构建**统一长期 sleeve 特征面板**（2014-2026），让四条家族腿在同一段样本上可比较、可组合。

三块特征：
  1) 估值（来自 `cn_valuation_features_long.parquet`）：pe/pb 行业分位、组合分位；
  2) 质量（来自回补的 PIT 财务 `cn_financial_indicators.parquet`）：roe/roa/毛利/净利/资产负债率/成长，
     按 `ann_date` 回看（公告后才可见），并转成**行业分位**；
  3) 技术/反转（由日线 qfq 现算）：动量（ret_120/250、12m-skip-1m、RPS_20/60、52 周位置、均线偏离）
     与反转/波动（ret_5、ret_20 取负、60 日最大回撤、20 日波动）。

输出：`assets/data/meta/cn_sleeve_features_long.parquet`（键：trade_date, stock_code）

注意：行业内分类用 registry 当前值（非 PIT）；技术特征用 qfq 收益计算，除权日附近存在轻微失真。

用法：
  uv run python scripts/build_sleeve_features_long.py --start 2014 --end 2026
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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT = ROOT / "assets/data/meta/cn_sleeve_features_long.parquet"
MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")


def load_industry() -> pd.DataFrame:
    import pyarrow.dataset as ds

    files = sorted(str(p) for p in (ROOT / "assets/data/meta/stock_info_registry").glob("**/*.parquet"))
    frame = ds.dataset(files, format="parquet").to_table(columns=["stock_code", "industry_l1"]).to_pandas()
    frame["stock_code"] = frame["stock_code"].astype(str)
    return frame.dropna(subset=["industry_l1"]).drop_duplicates("stock_code")


def build_tech(start: str, end: str, include_delisted: bool = True) -> pd.DataFrame:
    import pyarrow.dataset as ds

    years = list(range(pd.Timestamp(start).year - 1, pd.Timestamp(end).year + 1))
    files = [str(p) for p in (ROOT / "assets/data/clean/ohlcv/market=CN").glob(
        "exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet")
        if any(f"year={y}/" in str(p) for y in years)]
    bars = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "close", "high", "low"]).to_pandas()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    bars = bars[bars["stock_code"].str[:3].isin(MAIN_PREFIX)]
    if include_delisted:
        # 幸存者偏差修复：把退市股行情并进来（否则动量/反转腿会被"活下来的赢家"系统性美化）
        delisted_path = ROOT / "assets/data/meta/cn_delisted_daily.parquet"
        if delisted_path.exists():
            extra = pd.read_parquet(delisted_path, columns=["trade_date", "stock_code", "close"])
            extra["trade_date"] = pd.to_datetime(extra["trade_date"])
            extra = extra[extra["stock_code"].str[:3].isin(MAIN_PREFIX)]
            bars = pd.concat([bars, extra[["trade_date", "stock_code", "close", "high", "low"]]]
                             if "high" in extra.columns else
                             [bars, extra.assign(high=extra["close"], low=extra["close"])[
                                 ["trade_date", "stock_code", "close", "high", "low"]]],
                             ignore_index=True)
            print(f"[features-long] 并入退市股 {extra['stock_code'].nunique()} 只 / {len(extra):,} 行", flush=True)
    bars = bars.drop_duplicates(["trade_date", "stock_code"], keep="first")
    close = bars.pivot_table(index="trade_date", columns="stock_code", values="close", aggfunc="last").sort_index()
    ret = close.pct_change(fill_method=None)

    feat = {}
    feat["ret_5"] = close.pct_change(5, fill_method=None)
    feat["ret_20"] = close.pct_change(20, fill_method=None)
    feat["ret_120"] = close.pct_change(120, fill_method=None)
    feat["ret_250"] = close.pct_change(250, fill_method=None)
    feat["mom_12m_skip_1m"] = feat["ret_250"] - feat["ret_20"]
    feat["rps_20"] = feat["ret_20"].rank(axis=1, pct=True)
    feat["rps_60"] = close.pct_change(60, fill_method=None).rank(axis=1, pct=True)
    feat["pos_52w"] = close / close.rolling(250, min_periods=120).max() - 1
    feat["ma20_dev"] = close / close.rolling(20, min_periods=10).mean() - 1
    feat["ma60_dev"] = close / close.rolling(60, min_periods=30).mean() - 1
    feat["rev_1m"] = -feat["ret_20"]                      # 短期反转
    feat["rev_5d"] = -feat["ret_5"]
    feat["vol_20"] = ret.rolling(20, min_periods=10).std()
    roll_max = close.rolling(60, min_periods=30).max()
    feat["mdd_60"] = (close / roll_max - 1)               # 60 日回撤（负值）

    long = pd.concat({k: v.stack() for k, v in feat.items()}, axis=1)
    long.index.names = ["trade_date", "stock_code"]
    long = long.reset_index()
    return long[(long["trade_date"] >= pd.Timestamp(start)) & (long["trade_date"] <= pd.Timestamp(end))]


def build_quality(grid: pd.DataFrame, industry: pd.DataFrame) -> pd.DataFrame:
    """把 PIT 财务挂到给定的 (trade_date, stock_code) 网格上。

    简化而明确的 PIT 口径：对每个**自然年**，取该年年底前已公告的最近一期报告，
    作为该年所有交易日可见的财务快照（年度更新；比季度更新的信息集更保守）。
    """
    fin = pd.read_parquet(ROOT / "assets/data/meta/cn_financial_indicators.parquet")
    fin = fin[fin["ann_date"].notna()].copy()
    fin["ann_date"] = pd.to_datetime(fin["ann_date"])
    fin = fin.rename(columns={"ts_code": "stock_code"})
    cols = ["roe", "roa", "grossprofit_margin", "netprofit_margin", "debt_to_assets",
            "netprofit_yoy", "or_yoy", "ocfps"]
    fin = fin[["stock_code", "ann_date"] + [c for c in cols if c in fin.columns]]
    fin["stock_code"] = fin["stock_code"].astype(str)
    fin = fin.sort_values(["ann_date", "stock_code"])
    snapshots = []
    for year in sorted(set(pd.to_datetime(grid["trade_date"]).dt.year)):
        chunk = fin[fin["ann_date"] <= pd.Timestamp(f"{year}-12-31")]
        if chunk.empty:
            continue
        latest = chunk.groupby("stock_code").tail(1).copy()
        latest["year"] = year
        snapshots.append(latest)
    if not snapshots:
        return pd.DataFrame()
    yearly = pd.concat(snapshots, ignore_index=True)
    quality = grid[["trade_date", "stock_code"]].copy()
    quality["trade_date"] = pd.to_datetime(quality["trade_date"])
    quality["year"] = quality["trade_date"].dt.year
    quality = quality.merge(yearly.drop(columns=["ann_date"]), on=["year", "stock_code"], how="left").drop(columns="year")
    quality = quality.merge(industry, on="stock_code", how="left")
    quality["industry_l1"] = quality["industry_l1"].fillna("__ALL__")
    for col in ("roe", "roa", "grossprofit_margin", "netprofit_margin", "debt_to_assets"):
        quality[f"{col}_ind_pct"] = quality.groupby(["trade_date", "industry_l1"])[col].rank(pct=True)
    return quality[["trade_date", "stock_code", "roe_ind_pct", "roa_ind_pct", "grossprofit_margin_ind_pct",
                    "netprofit_margin_ind_pct", "debt_to_assets_ind_pct",
                    "netprofit_yoy", "or_yoy", "ocfps"]]


def main() -> int:
    parser = argparse.ArgumentParser(description="构建统一长期 sleeve 特征面板")
    parser.add_argument("--start", default="2015-01-01")
    # `--end` 曾经硬编码成 "2026-09-30" —— 于是**无论什么时候重建**，面板都停在 9 月末，
    # 下游（arm_selection / sleeve 研究 / 前端日历）拿到的一直是过期面板。
    # 实测：2026-10-09 重建一次，v1..v4 五份产物的最新交易日都还是 2026-09-30。
    parser.add_argument("--end", default="", help="默认=今天（留空即动态取当天）")
    args = parser.parse_args()
    if not args.end:
        args.end = datetime.now().strftime("%Y-%m-%d")

    industry = load_industry()
    tech = build_tech(args.start, args.end)
    print(f"[features-long] 技术特征 {len(tech):,} 行 / {tech['stock_code'].nunique()} 只", flush=True)

    valuation_path = ROOT / "assets/data/meta/cn_valuation_features_long.parquet"
    valuation = pd.DataFrame()
    if valuation_path.exists():
        valuation = pd.read_parquet(valuation_path, columns=["trade_date", "stock_code", "pe_ind_pct", "pb_ind_pct", "pe_pb_combo"])
        valuation["trade_date"] = pd.to_datetime(valuation["trade_date"])
        valuation["stock_code"] = valuation["stock_code"].astype(str)
        print(f"[features-long] 估值特征 {len(valuation):,} 行", flush=True)

    quality = build_quality(tech, industry)
    print(f"[features-long] 质量特征 {len(quality):,} 行", flush=True)

    merged = tech
    for frame in (valuation, quality):
        if not frame.empty:
            merged = merged.merge(frame, on=["trade_date", "stock_code"], how="left")
    merged = merged.sort_values(["trade_date", "stock_code"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    merged.to_parquet(OUT, index=False)
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "rows": int(len(merged)), "codes": int(merged["stock_code"].nunique()),
        "window": [str(merged["trade_date"].min().date()), str(merged["trade_date"].max().date())],
        "columns": list(merged.columns),
        "coverage": {c: round(float(merged[c].notna().mean()), 4)
                     for c in merged.columns if c not in ("trade_date", "stock_code")},
        "limitations": ["行业分类用 registry 当前值（非 PIT）",
                        "财务特征按 ann_date 年度快照挂载（同一年内不随季度更新）",
                        "技术特征由 qfq 收益计算"],
        "out": str(OUT),
    }
    (OUT.parent / "cn_sleeve_features_long.manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
