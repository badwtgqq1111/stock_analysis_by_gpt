#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""构建 A 股市场因子块（P2.81 Phase 1）。

市场因子不是「上证指数涨跌」这一列：
  * 市场收益/波动/回撤：中证全指 000985.CSI（全市场口径）
  * 市场宽度：上涨家数占比、站上 MA20 占比
  * 市场投机温度：涨停家数占比、全市场成交额 Z
  * 个股暴露：beta_60/120/250、idio_vol_60、downside_beta_120

输出：
  assets/data/derived/cn_market_factors.parquet
  assets/data/derived/cn_stock_market_beta.parquet
  assets/data/derived/cn_market_factors.manifest.json

用法：
  .venv/bin/python scripts/build_market_factors.py
  .venv/bin/python scripts/build_market_factors.py --start 2016-01-01 --end 2026-12-31
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
BENCH = ROOT / "assets/data/meta/cn_benchmark_daily.parquet"
TURNOVER = ROOT / "assets/data/meta/cn_market_turnover.parquet"
ZT = ROOT / "output/research/zt_board/zt_board_panel.parquet"
BASIC = ROOT / "assets/data/meta/cn_stock_basic.parquet"
DELISTED = ROOT / "assets/data/meta/cn_delisted_daily.parquet"
OUT_MARKET = ROOT / "assets/data/derived/cn_market_factors.parquet"
OUT_BETA = ROOT / "assets/data/derived/cn_stock_market_beta.parquet"
MANIFEST = ROOT / "assets/data/derived/cn_market_factors.manifest.json"


def _bar_files(start: str, end: str) -> list[str]:
    start_year, end_year = pd.Timestamp(start).year, pd.Timestamp(end).year
    files = []
    for year in range(start_year, end_year + 1):
        files.extend(str(p) for p in OHLCV.glob(
            f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={year}/*.parquet"))
    return sorted(set(files))


def stock_universe() -> set[str]:
    """真实股票代码集合；clean/ohlcv 里混有 000001.SH 等指数行，必须剔除。"""
    basic = pd.read_parquet(BASIC, columns=["ts_code"])["ts_code"].astype(str)
    delisted = pd.read_parquet(DELISTED, columns=["stock_code"])["stock_code"].astype(str)
    return set(basic) | set(delisted)


def load_prices(start: str, end: str) -> pd.DataFrame:
    files = _bar_files(start, end)
    if not files:
        raise SystemExit("没有日线文件")
    table = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "close"]).to_pandas()
    table["trade_date"] = pd.to_datetime(table["trade_date"], errors="coerce").dt.normalize()
    table["close"] = pd.to_numeric(table["close"], errors="coerce")
    table = table.dropna(subset=["trade_date", "stock_code", "close"])
    table = table[(table["trade_date"] >= pd.Timestamp(start)) & (table["trade_date"] <= pd.Timestamp(end))]
    table = table[table["stock_code"].isin(stock_universe())]
    table = table[table["close"] > 0].drop_duplicates(["trade_date", "stock_code"], keep="last")
    return table


def build_market_series(prices: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    bench = pd.read_parquet(BENCH)
    bench = bench[bench["ts_code"] == "000985.CSI"][["trade_date", "close"]].copy()
    bench["trade_date"] = pd.to_datetime(bench["trade_date"]).dt.normalize()
    bench = bench.sort_values("trade_date").set_index("trade_date")["close"]

    turnover = pd.read_parquet(TURNOVER)
    turnover["trade_date"] = pd.to_datetime(turnover["trade_date"]).dt.normalize()
    turnover = turnover.set_index("trade_date").sort_index()

    # 站上 MA20 的股票占比：只用当日及以前的数据
    close = prices.pivot_table(index="trade_date", columns="stock_code", values="close", aggfunc="last").sort_index()
    ma20 = close.rolling(20, min_periods=20).mean()
    breadth_ma20 = (close > ma20).where(ma20.notna()).mean(axis=1)

    zt = pd.read_parquet(ZT, columns=["trade_date", "is_zt"])
    zt["trade_date"] = pd.to_datetime(zt["trade_date"]).dt.normalize()
    zt_count = zt.groupby("trade_date")["is_zt"].sum().rename("zt_count")

    frame = pd.DataFrame(index=bench.index)
    frame.index.name = "trade_date"
    frame["mkt_close"] = bench.reindex(frame.index)
    frame["mkt_ret_1"] = frame["mkt_close"].pct_change()
    for days in (5, 20, 60):
        frame[f"mkt_ret_{days}"] = frame["mkt_close"].pct_change(days)
    frame["mkt_vol_20"] = frame["mkt_ret_1"].rolling(20, min_periods=10).std() * np.sqrt(252)
    frame["mkt_vol_60"] = frame["mkt_ret_1"].rolling(60, min_periods=30).std() * np.sqrt(252)
    frame["mkt_dd_60"] = frame["mkt_close"] / frame["mkt_close"].rolling(60, min_periods=20).max() - 1
    frame["mkt_breadth_above_ma20"] = breadth_ma20.reindex(frame.index)
    frame["mkt_breadth_up"] = turnover["up_ratio"].reindex(frame.index)
    frame["mkt_amount_yi"] = turnover["amount_yi"].reindex(frame.index)
    frame["mkt_turnover_z"] = (
        np.log(frame["mkt_amount_yi"].replace(0, np.nan))
        - np.log(frame["mkt_amount_yi"].replace(0, np.nan)).rolling(20, min_periods=10).mean()
    ) / np.log(frame["mkt_amount_yi"].replace(0, np.nan)).rolling(20, min_periods=10).std()
    frame["mkt_limitup_ratio"] = (zt_count / turnover["n_priced"]).reindex(frame.index)
    frame = frame[(frame.index >= pd.Timestamp(start)) & (frame.index <= pd.Timestamp(end))].copy()
    frame = frame.reset_index()
    return frame


def build_stock_beta(prices: pd.DataFrame, market: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    close = prices.pivot_table(index="trade_date", columns="stock_code", values="close", aggfunc="last").sort_index()
    rets = close.pct_change(fill_method=None)
    mkt = market.set_index("trade_date")["mkt_ret_1"].reindex(rets.index)
    # 不做全样本覆盖率过滤：次新股/停牌股在 rolling 窗口不足时自然是 NaN，
    # 全样本过滤会把 2016 年之后才上市的股票整段删掉，反而制造幸存者偏差。
    out = {}
    for window in (60, 120, 250):
        min_periods = max(20, int(window * 0.6))
        cov = rets.rolling(window, min_periods=min_periods).cov(mkt)
        var = mkt.rolling(window, min_periods=min_periods).var()
        out[f"beta_{window}"] = cov.div(var.replace(0, np.nan), axis=0).astype("float32")
    # 特质波动：60 日残差波动（用 beta_60 的时变暴露近似）
    beta60 = out["beta_60"]
    resid = rets.sub(beta60.mul(mkt, axis=0))
    out["idio_vol_60"] = (resid.rolling(60, min_periods=30).std() * np.sqrt(252)).astype("float32")
    # 下行 beta：只用市场下跌日；120 日窗口，至少 20 个下跌观测
    rets_down = rets.where(mkt < 0, axis=0)
    mkt_down = mkt.where(mkt < 0)
    cov_down = rets_down.rolling(120, min_periods=20).cov(mkt_down)
    var_down = mkt_down.rolling(120, min_periods=20).var()
    out["downside_beta_120"] = cov_down.div(var_down.replace(0, np.nan), axis=0).astype("float32")

    pieces = []
    for name, matrix in out.items():
        long = matrix.stack().dropna().rename(name).reset_index()
        long.columns = ["trade_date", "stock_code", name]
        pieces.append(long)
    merged = pieces[0]
    for piece in pieces[1:]:
        merged = merged.merge(piece, on=["trade_date", "stock_code"], how="outer")
    merged["trade_date"] = pd.to_datetime(merged["trade_date"]).dt.normalize()
    merged = merged[(merged["trade_date"] >= pd.Timestamp(start)) & (merged["trade_date"] <= pd.Timestamp(end))]
    # 加交互项（不改变原始 beta；下游可直接用）
    market_ctx = market[["trade_date", "mkt_ret_20", "mkt_vol_20", "mkt_breadth_up", "mkt_turnover_z"]].copy()
    merged = merged.merge(market_ctx, on="trade_date", how="left")
    merged["beta_60_x_mkt_ret_20"] = merged["beta_60"] * merged["mkt_ret_20"]
    merged["beta_60_x_mkt_vol_20"] = merged["beta_60"] * merged["mkt_vol_20"]
    return merged.sort_values(["trade_date", "stock_code"]).reset_index(drop=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="构建市场因子块")
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default="2026-12-31")
    args = ap.parse_args()
    started = datetime.now()
    print(f"[market] 读取日线 {args.start} ~ {args.end}", flush=True)
    prices = load_prices(args.start, args.end)
    print(f"[market] 日线 {len(prices):,} 行 / {prices.stock_code.nunique()} 只", flush=True)
    market = build_market_series(prices, args.start, args.end)
    print(f"[market] 市场时间序列 {len(market):,} 行 ({market.trade_date.min().date()} ~ {market.trade_date.max().date()})", flush=True)
    beta = build_stock_beta(prices, market, args.start, args.end)
    print(f"[market] 个股 beta {len(beta):,} 行", flush=True)
    OUT_MARKET.parent.mkdir(parents=True, exist_ok=True)
    market.to_parquet(OUT_MARKET, index=False)
    beta.to_parquet(OUT_BETA, index=False)
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "start": str(market.trade_date.min().date()), "end": str(market.trade_date.max().date()),
        "market_rows": int(len(market)), "beta_rows": int(len(beta)),
        "stocks": int(beta.stock_code.nunique()),
        "market_columns": list(market.columns), "beta_columns": list(beta.columns),
        "market_path": str(OUT_MARKET.relative_to(ROOT)),
        "beta_path": str(OUT_BETA.relative_to(ROOT)),
        "benchmark": "000985.CSI",
        "notes": [
            "市场收益/波动/回撤用中证全指 000985.CSI；上证指数不作为唯一市场代理",
            "beta 是滚动协方差/方差；min_periods 为窗口的 60%",
            "idio_vol_60 是 60 日市场回归残差波动年化",
            "downside_beta_120 只用市场下跌日，至少 20 个下跌观测",
            "mkt_breadth_above_ma20 使用当日收盘与 20 日均线，不含未来数据",
            "mkt_limitup_ratio 的涨停面板只到 2026-10-08；之后为缺失，不向前填充",
        ],
        "seconds": round((datetime.now() - started).total_seconds(), 1),
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in ("start", "end", "market_rows", "beta_rows", "stocks", "seconds")},
                     ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
