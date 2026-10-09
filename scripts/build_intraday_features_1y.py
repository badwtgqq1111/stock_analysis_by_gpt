#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把**一年内**的 5min 分时数据派生成日频特征（L8 的"按 1 年窗口"路线）。

背景（2026-10-07 决定）：baostock 分钟线可回溯 2015，但按实测 ~0.13 只/秒，全市场回补约 7 小时；
用户决定**分时只看一年内**（现有 5,228 只 / 2025-10-09~2026-09-30 / 241 个交易日，不再回补历史）。
于是本脚本把这批数据榨干：把日内结构压成日频列，供"独立窗口"检验用。

每只股票每个交易日输出（全部只用**当天盘中以及前一交易日收盘**的信息，无未来函数）：
  intraday_mom    收盘/开盘 − 1                     （日内动量）
  overnight_ret   开盘/前收 − 1                      （隔夜跳空，前收来自日线）
  head_30m        开盘后 30 分钟收益（前 6 根 5min）
  tail_30m        收盘前 30 分钟收益                 （尾盘强度）
  intraday_vol    5min 收益的标准差                  （日内波动）
  vwap_dev        收盘/VWAP − 1（VWAP 用成交额/成交量加权）
  range_pct       (最高−最低)/VWAP
  tail_vol_share  尾盘 6 根成交量占全天比例          （尾盘放量）
  am_pm_gap       上午 / 下午 收益差

用法:
  uv run python scripts/build_intraday_features_1y.py            # 全量（约 5~15 分钟）
  uv run python scripts/build_intraday_features_1y.py --limit 50 # 抽样自检
输出:
  assets/data/meta/cn_intraday_daily_features.parquet + .manifest.json
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "assets/data/raw/intraday_baostock/frequency=5min"
DAILY = ROOT / "assets/data/clean/ohlcv/market=CN"
OUT = ROOT / "assets/data/meta/cn_intraday_daily_features.parquet"
BARS_PER_DAY = 48


def _one_stock(path_str: str):
    """返回 (DataFrame | None, 错误原因 | None)。

    实测坑（2026-10-07）：部分 5min 文件的 volume/amount 列被写成了**多个数值连成一串**的字符串
    （形如 "3847912.0000564053.0000…"），直接 float() 会抛 ValueError 把整个进程带崩。
    这里统一 to_numeric(errors="coerce")，并把坏行占比超 5% 的文件记为 quarantined。
    """
    path = Path(path_str)
    try:
        frame = pd.read_parquet(path, columns=["date", "time", "open", "high", "low", "close", "volume", "amount"])
    except Exception as exc:
        return None, f"{path.name}: read_error {type(exc).__name__}"
    if frame.empty or len(frame) < BARS_PER_DAY:
        return None, f"{path.name}: too_few_bars({len(frame)})"
    for col in ("open", "high", "low", "close", "volume", "amount"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    bad_ratio = float(frame[["close", "volume"]].isna().any(axis=1).mean())
    if bad_ratio > 0.05:
        return None, f"{path.name}: malformed_numeric({bad_ratio:.0%})"
    frame = frame.dropna(subset=["open", "high", "low", "close", "volume"])
    frame = frame.sort_values("time")
    frame["day"] = pd.to_datetime(frame["date"]).dt.normalize()
    rows = []
    for day, g in frame.groupby("day", sort=True):
        if len(g) < 10:
            continue
        o = float(g["open"].iloc[0]); c = float(g["close"].iloc[-1])
        hi = float(g["high"].max()); lo = float(g["low"].min())
        vol = float(g["volume"].sum()); amt = float(g["amount"].sum())
        vwap = amt / vol if vol > 0 else np.nan
        close_series = g["close"].astype(float)
        rets = close_series.pct_change().dropna()
        head6 = g.head(6); tail6 = g.tail(6)
        half = len(g) // 2
        rows.append({
            "trade_date": day, "stock_code": path.stem.replace("_", "."),
            "n_bars": int(len(g)),
            "intraday_mom": c / o - 1 if o else np.nan,
            "overnight_ret": np.nan,                      # 稍后用日线前收回填
            "head_30m": float(head6["close"].iloc[-1]) / o - 1 if o else np.nan,
            "tail_30m": c / float(tail6["open"].iloc[0]) - 1 if len(tail6) and tail6["open"].iloc[0] else np.nan,
            "intraday_vol": float(rets.std()) if len(rets) > 3 else np.nan,
            "vwap_dev": c / vwap - 1 if vwap and np.isfinite(vwap) else np.nan,
            "range_pct": (hi - lo) / vwap if vwap and np.isfinite(vwap) else np.nan,
            "tail_vol_share": float(tail6["volume"].sum()) / vol if vol > 0 else np.nan,
            "am_pm_gap": (float(g["close"].iloc[half - 1]) / o - 1) - (c / float(g["close"].iloc[half - 1]) - 1)
                         if o and half > 0 else np.nan,
            "day_open": o, "day_close": c,
        })
    return (pd.DataFrame(rows) if rows else None), None


def main() -> int:
    ap = argparse.ArgumentParser(description="5min → 日频特征（1 年窗口）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    files = sorted(SRC.glob("*.parquet"))
    if args.limit:
        files = files[: args.limit]
    print(f"[5min] 输入 {len(files)} 只股票的 5min 文件", flush=True)

    frames, problems, done = [], [], 0
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_one_stock, str(p)): p for p in files}
        for fut in as_completed(futures):
            out, err = fut.result()
            if out is not None and not out.empty:
                frames.append(out)
            elif err:
                problems.append(err)
            done += 1
            if done % 500 == 0:
                print(f"[5min]   {done}/{len(files)} 只，累计 {sum(len(f) for f in frames):,} 行，"
                      f"异常 {len(problems)}", flush=True)
    if not frames:
        raise SystemExit("没有产出任何日内特征")
    feat = pd.concat(frames, ignore_index=True)
    print(f"[5min] 日频特征 {len(feat):,} 行 / {feat['stock_code'].nunique()} 只 / "
          f"{feat['trade_date'].nunique()} 个交易日", flush=True)

    # 隔夜收益：需要日线前收（qfq）
    import pyarrow.dataset as ds
    years = sorted({str(d.year) for d in feat["trade_date"].dt.normalize().unique()})
    files_d = [str(p) for y in years
               for p in DAILY.glob(f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={y}/*.parquet")]
    bars = ds.dataset(files_d, format="parquet").to_table(
        columns=["trade_date", "stock_code", "close"]).to_pandas()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    bars = bars.sort_values(["stock_code", "trade_date"])
    bars["prev_close"] = bars.groupby("stock_code", sort=False)["close"].shift(1)
    feat = feat.merge(bars[["trade_date", "stock_code", "prev_close"]], on=["trade_date", "stock_code"], how="left")
    feat["overnight_ret"] = feat["day_open"] / feat["prev_close"] - 1

    keep = ["trade_date", "stock_code", "n_bars", "intraday_mom", "overnight_ret", "head_30m", "tail_30m",
            "intraday_vol", "vwap_dev", "range_pct", "tail_vol_share", "am_pm_gap"]
    feat = feat[keep].sort_values(["trade_date", "stock_code"])
    coverage = {c: round(float(feat[c].notna().mean()), 4) for c in keep[3:]}
    print(f"[5min] 覆盖率：{coverage}", flush=True)
    feat.to_parquet(OUT, index=False)
    OUT.with_suffix(".manifest.json").write_text(json.dumps({
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rows": int(len(feat)), "stocks": int(feat["stock_code"].nunique()),
        "trading_days": int(feat["trade_date"].nunique()),
        "date_range": [str(feat["trade_date"].min().date()), str(feat["trade_date"].max().date())],
        "fields": keep[3:], "coverage": coverage,
        "source": str(SRC), "window_note": "只覆盖一年内（2025-10-09 起），按用户决定不回补历史",
        "quarantined_files": len(problems), "quarantine_sample": problems[:10],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    if problems:
        (ROOT / "output/research/intraday_feature_quarantine.txt").write_text(
            "\n".join(problems), encoding="utf-8")
        print(f"[5min] 隔离坏文件 {len(problems)} 只（清单 output/research/intraday_feature_quarantine.txt）："
              f"{problems[:3]}", flush=True)
    print(f"[5min] 写出 {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
