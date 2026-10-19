#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""汇总 A 股每日总成交额（页面“大盘”用）。

输入：`assets/data/clean/ohlcv/market=CN/.../frequency=daily/adjust=qfq/year=*/`
输出：`assets/data/meta/cn_market_turnover.parquet`
列：trade_date, amount_yuan, volume, n_stocks

口径：
- amount 为 clean 日线的原始成交额，单位按现有数据契约为**元**；
- 不做复权换算（成交额本身就是名义金额，不是价格）；
- n_stocks = 当日有正成交额的股票数，停牌/无成交不计入；
- 只聚合 equity 分区，不把指数代码混进来。
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.ingest.service import CN_EQUITY_PREFIXES  # noqa: E402

BARS = ROOT / "assets/data/clean/ohlcv/market=CN"
OUT = ROOT / "assets/data/meta/cn_market_turnover.parquet"


def main() -> int:
    ap = argparse.ArgumentParser(description="汇总 A 股每日总成交额")
    ap.add_argument("--start-year", type=int, default=2015)
    args = ap.parse_args()

    files = sorted(BARS.glob(
        "exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet"))
    files = [p for p in files if int(p.parent.name.split("=")[1]) >= args.start_year]
    if not files:
        raise SystemExit(f"没有找到日线文件：{BARS}")

    table = ds.dataset([str(p) for p in files], format="parquet").to_table(
        columns=["trade_date", "stock_code", "amount", "volume"])
    df = table.to_pandas()
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df = df.dropna(subset=["trade_date", "stock_code"])
    df = df[df["amount"].fillna(0) > 0]

    # clean/ohlcv 里混有指数代码（000001.SH 等）。不滤掉会把指数成交额
    # 和个股成交额相加，2026-03 的曲线会出现 28 万亿级假尖峰。
    codes = df["stock_code"].astype(str).str.upper()
    digits = codes.str.slice(0, 6)
    exchange = codes.str.slice(7)
    equity = (
        (exchange.eq("SH") & digits.str[:3].isin(CN_EQUITY_PREFIXES["SH"]))
        | (exchange.eq("SZ") & digits.str[:3].isin(CN_EQUITY_PREFIXES["SZ"]))
    )
    dropped = int((~equity).sum())
    df = df[equity]

    grouped = (
        df.groupby("trade_date", as_index=False)
        .agg(amount_yuan=("amount", "sum"),
             volume=("volume", "sum"),
             n_stocks=("stock_code", "nunique"))
        .sort_values("trade_date")
    )
    grouped["amount_yi"] = grouped["amount_yuan"] / 1e8
    grouped.to_parquet(OUT, index=False)
    print(
        f"[market-turnover] {OUT.relative_to(ROOT)} | {len(grouped):,} 行 | "
        f"{grouped.trade_date.min().date()} → {grouped.trade_date.max().date()} | "
        f"最新 {grouped.amount_yi.iloc[-1]:,.1f} 亿 / {int(grouped.n_stocks.iloc[-1]):,} 只"
        f" | 剔除非 A 股代码 {dropped:,} 行"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
