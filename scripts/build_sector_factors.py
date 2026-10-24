#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""构建 A 股板块因子块（P2.81 Phase 2）。

口径：
  * 行业成员用 `cn_sw2021_industry_history.parquet` 的 PIT 区间
    `effective_from <= trade_date <= effective_to`，不是当前快照回填；
  * 板块收益 = 成员等权日收益（同时保留中位数口径）；
  * 板块因子 = 5/20/60 日收益、20 日反转、离散度、成交额份额/Z、涨停密度、资金流 Z、拥挤度；
  * 个股相对板块 = 个股收益 − 同行业板块收益。

输出：
  assets/data/derived/cn_sector_factors.parquet
  assets/data/derived/cn_stock_sector_features.parquet
  assets/data/derived/cn_sector_factors.manifest.json

用法：
  .venv/bin/python scripts/build_sector_factors.py
  .venv/bin/python scripts/build_sector_factors.py --start 2016-01-01 --end 2026-12-31
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OHLCV = ROOT / "assets/data/clean/ohlcv/market=CN"
MEMBERSHIP = ROOT / "assets/data/meta/cn_sw2021_industry_history.parquet"
ZT = ROOT / "output/research/zt_board/zt_board_panel.parquet"
MONEYFLOW = ROOT / "assets/data/derived/cn_moneyflow_features.parquet"
BASIC = ROOT / "assets/data/meta/cn_stock_basic.parquet"
DELISTED = ROOT / "assets/data/meta/cn_delisted_daily.parquet"
OUT_SECTOR = ROOT / "assets/data/derived/cn_sector_factors.parquet"
OUT_STOCK = ROOT / "assets/data/derived/cn_stock_sector_features.parquet"
MANIFEST = ROOT / "assets/data/derived/cn_sector_factors.manifest.json"


def _bar_files(start: str, end: str) -> list[str]:
    start_year, end_year = pd.Timestamp(start).year, pd.Timestamp(end).year
    files = []
    for year in range(start_year, end_year + 1):
        files.extend(str(p) for p in OHLCV.glob(
            f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={year}/*.parquet"))
    return sorted(set(files))


def stock_universe() -> set[str]:
    basic = pd.read_parquet(BASIC, columns=["ts_code"])["ts_code"].astype(str)
    delisted = pd.read_parquet(DELISTED, columns=["stock_code"])["stock_code"].astype(str)
    return set(basic) | set(delisted)


def load_bars(start: str, end: str) -> pd.DataFrame:
    files = _bar_files(start, end)
    if not files:
        raise SystemExit("没有日线文件")
    frame = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "close", "amount"]).to_pandas()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce").dt.normalize()
    for col in ("close", "amount"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["trade_date", "stock_code", "close"])
    frame = frame[(frame["trade_date"] >= pd.Timestamp(start)) & (frame["trade_date"] <= pd.Timestamp(end))]
    frame = frame[frame["stock_code"].isin(stock_universe())]
    frame = frame[frame["close"] > 0].drop_duplicates(["trade_date", "stock_code"], keep="last")
    return frame.sort_values(["stock_code", "trade_date"]).reset_index(drop=True)


def load_membership() -> pd.DataFrame:
    mem = pd.read_parquet(MEMBERSHIP)
    mem = mem[["stock_code", "industry_l1", "industry_l2", "industry_l1_code", "industry_l2_code",
               "effective_from", "effective_to"]].copy()
    mem["effective_from"] = pd.to_datetime(mem["effective_from"]).dt.normalize()
    mem["effective_to"] = pd.to_datetime(mem["effective_to"]).dt.normalize()
    mem = mem.sort_values(["stock_code", "effective_from"]).reset_index(drop=True)
    return mem


def attach_pit_industry(bars: pd.DataFrame, mem: pd.DataFrame) -> pd.DataFrame:
    # merge_asof 的 on 键必须全局有序；by 只保证组内匹配。
    left = bars.sort_values(["trade_date", "stock_code"])
    right = mem.sort_values(["effective_from", "stock_code"])
    merged = pd.merge_asof(
        left, right, left_on="trade_date", right_on="effective_from",
        by="stock_code", direction="backward", allow_exact_matches=True)
    merged = merged[merged["trade_date"] <= merged["effective_to"]].copy()
    merged = merged.sort_values(["stock_code", "trade_date"]).reset_index(drop=True)
    missing = merged["industry_l2"].isna().mean()
    print(f"[sector] PIT 行业映射缺失 {missing:.2%}", flush=True)
    return merged


def add_returns(merged: pd.DataFrame) -> pd.DataFrame:
    grouped = merged.groupby("stock_code", sort=False)
    merged["ret_1d"] = grouped["close"].pct_change(fill_method=None)
    for days in (5, 20, 60):
        merged[f"ret_{days}"] = grouped["close"].pct_change(days, fill_method=None)
    return merged


def main() -> int:
    ap = argparse.ArgumentParser(description="构建板块因子块")
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default="2026-12-31")
    args = ap.parse_args()
    started = datetime.now()
    print(f"[sector] 读取日线 {args.start} ~ {args.end}", flush=True)
    bars = load_bars(args.start, args.end)
    mem = load_membership()
    merged = attach_pit_industry(bars, mem)
    merged = add_returns(merged)

    # 涨停：用已有的涨停面板；没有匹配到的日期保留 NaN，不伪造 0
    zt = pd.read_parquet(ZT, columns=["trade_date", "stock_code", "is_zt"])
    zt["trade_date"] = pd.to_datetime(zt["trade_date"]).dt.normalize()
    zt["is_zt"] = pd.to_numeric(zt["is_zt"], errors="coerce")
    merged = merged.merge(zt, on=["trade_date", "stock_code"], how="left")

    # 资金流：只有 2024-08 之后；按日/行业取成员中位数
    try:
        mf = pd.read_parquet(MONEYFLOW, columns=["trade_date", "stock_code", "source", "moneyflow_net_z_5d"])
        mf = mf[mf["source"] == "moneyflow"].copy()
        mf["trade_date"] = pd.to_datetime(mf["trade_date"]).dt.normalize()
        merged = merged.merge(mf[["trade_date", "stock_code", "moneyflow_net_z_5d"]],
                              on=["trade_date", "stock_code"], how="left")
    except Exception as exc:  # noqa: BLE001
        print(f"[sector] 资金流跳过：{type(exc).__name__}: {exc}", flush=True)
        merged["moneyflow_net_z_5d"] = np.nan

    key = ["trade_date", "industry_l1", "industry_l2", "industry_l1_code", "industry_l2_code"]
    daily = merged.groupby(key, dropna=False).agg(
        sector_ret_1d=("ret_1d", "mean"),
        sector_ret_1d_median=("ret_1d", "median"),
        sector_amount=("amount", "sum"),
        sector_n=("stock_code", "nunique"),
        sector_zt=("is_zt", "sum"),
        sector_dispersion_20=("ret_20", "std"),
        sector_moneyflow_z=("moneyflow_net_z_5d", "median"),
    ).reset_index()

    # 板块 5/20/60 日收益：用等权日收益复利；groupby 后 transform 避免把日期顺序打乱
    daily = daily.sort_values(["industry_l2_code", "trade_date"])
    by_sector = daily.groupby("industry_l2_code", sort=False)
    for days in (5, 20, 60):
        daily[f"sector_ret_{days}"] = by_sector["sector_ret_1d"].transform(
            lambda s, d=days: (1 + s.fillna(0)).rolling(d, min_periods=max(2, d // 2)).apply(np.prod, raw=True) - 1)
    daily["sector_rev_20"] = -daily["sector_ret_20"]

    total_amount = daily.groupby("trade_date")["sector_amount"].transform("sum")
    daily["sector_turnover_share"] = daily["sector_amount"] / total_amount.replace(0, np.nan)
    daily["sector_limitup_density"] = daily["sector_zt"] / daily["sector_n"].replace(0, np.nan)
    daily = daily.sort_values(["industry_l2_code", "trade_date"])
    by_sector = daily.groupby("industry_l2_code", sort=False)
    daily["sector_turnover_z"] = (
        daily["sector_turnover_share"]
        - by_sector["sector_turnover_share"].transform(lambda s: s.rolling(20, min_periods=10).mean())
    ) / by_sector["sector_turnover_share"].transform(lambda s: s.rolling(20, min_periods=10).std()).replace(0, np.nan)

    # 拥挤度：成交额份额 Z + 涨停密度 Z + 20 日收益 Z（同一交易日截面）
    # 方向：高拥挤 = 高成交份额 + 高涨停密度 + 已经涨过；本地 2016-2026
    # 行业层 IC 约 -0.016（NW t -1.7），只作诊断/交互，不单独当 alpha。
    def _cs_z(series: pd.Series) -> pd.Series:
        values = pd.to_numeric(series, errors="coerce")
        std = values.std(ddof=0)
        return (values - values.mean()) / std if std and std > 0 else pd.Series(0.0, index=series.index)

    daily["_z_share"] = daily.groupby("trade_date", sort=False)["sector_turnover_share"].transform(_cs_z)
    daily["_z_limit"] = daily.groupby("trade_date", sort=False)["sector_limitup_density"].transform(_cs_z)
    daily["_z_ret20"] = daily.groupby("trade_date", sort=False)["sector_ret_20"].transform(_cs_z)
    daily["sector_crowding"] = daily["_z_share"] + daily["_z_limit"] + daily["_z_ret20"]

    # 个股相对板块：按 (date, industry) 合并板块收益/离散/拥挤
    sector_cols = ["trade_date", "industry_l2_code", "sector_ret_5", "sector_ret_20", "sector_ret_60",
                   "sector_rev_20", "sector_dispersion_20", "sector_crowding",
                   "sector_turnover_z", "sector_limitup_density"]
    stock = merged.merge(daily[sector_cols], on=["trade_date", "industry_l2_code"], how="left")
    for days in (5, 20, 60):
        stock[f"stock_vs_sector_ret_{days}"] = stock[f"ret_{days}"] - stock[f"sector_ret_{days}"]

    stock_cols = ["trade_date", "stock_code", "industry_l1", "industry_l2",
                  "sector_ret_5", "sector_ret_20", "sector_ret_60", "sector_rev_20",
                  "sector_dispersion_20", "sector_crowding", "sector_turnover_z",
                  "sector_limitup_density", "stock_vs_sector_ret_5", "stock_vs_sector_ret_20",
                  "stock_vs_sector_ret_60"]
    stock_out = stock[stock_cols].copy()
    for col in stock_cols[4:]:
        stock_out[col] = pd.to_numeric(stock_out[col], errors="coerce").astype("float32")

    sector_cols_out = ["trade_date", "industry_l1", "industry_l2", "industry_l1_code", "industry_l2_code",
                       "sector_ret_1d", "sector_ret_1d_median", "sector_ret_5", "sector_ret_20",
                       "sector_ret_60", "sector_rev_20", "sector_dispersion_20",
                       "sector_turnover_share", "sector_turnover_z", "sector_limitup_density",
                       "sector_moneyflow_z", "sector_crowding", "sector_n"]
    sector_out = daily[sector_cols_out].copy()
    for col in sector_cols_out[5:]:
        sector_out[col] = pd.to_numeric(sector_out[col], errors="coerce").astype("float32")

    OUT_SECTOR.parent.mkdir(parents=True, exist_ok=True)
    sector_out.to_parquet(OUT_SECTOR, index=False)
    stock_out.to_parquet(OUT_STOCK, index=False)
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "start": str(sector_out.trade_date.min().date()), "end": str(sector_out.trade_date.max().date()),
        "sector_rows": int(len(sector_out)), "stock_rows": int(len(stock_out)),
        "sectors_l1": int(sector_out.industry_l1_code.nunique()),
        "sectors_l2": int(sector_out.industry_l2_code.nunique()),
        "stocks": int(stock_out.stock_code.nunique()),
        "sector_path": str(OUT_SECTOR.relative_to(ROOT)),
        "stock_path": str(OUT_STOCK.relative_to(ROOT)),
        "membership": str(MEMBERSHIP.relative_to(ROOT)),
        "columns": list(sector_out.columns),
        "notes": [
            "行业成员使用 PIT 区间 effective_from <= trade_date <= effective_to",
            "板块收益为成员等权日收益；sector_ret_1d_median 为中位数口径",
            "sector_moneyflow_z 只有 2024-08 之后有值；之前为缺失",
            "sector_crowding = z(成交额份额) + z(涨停密度) + z(20日收益)；只作诊断/交互",
            "sector_limitup_density 依赖涨停面板，面板截止 2026-10-08",
        ],
        "seconds": round((datetime.now() - started).total_seconds(), 1),
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in
                      ("start", "end", "sector_rows", "stock_rows", "sectors_l1", "sectors_l2",
                       "stocks", "seconds")}, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
