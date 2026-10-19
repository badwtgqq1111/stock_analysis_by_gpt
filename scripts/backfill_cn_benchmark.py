#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""回补 A 股基准/大盘指数日线（供组合绩效、页面指数卡与指数 K 线使用）。

为什么需要
----------
绩效页要的「基准收益 / 基准波动率 / 阿尔法 / 贝塔 / 信息比率 / 超额收益最大回撤」
都需要一条**外部基准**的日频序列。仓库里此前没有：
`assets/data/clean/ohlcv/market=CN` 下 `asset_type` 只有 `equity`（没有 index）。

踩过的坑：中证系列指数的代码后缀是 **`.CSI`** 而不是 `.SH` ——
`000985.SH` 返回 0 行，`000985.CSI` 才有数据（667 行 / 2024-01-02 起）。

用法:
  uv run python scripts/backfill_cn_benchmark.py            # 增量（只补缺失区间）
  uv run python scripts/backfill_cn_benchmark.py --start 20150101
输出:
  assets/data/meta/cn_benchmark_daily.parquet
  列：trade_date, ts_code, open, high, low, close, pre_close, change,
      pct_chg, vol, amount, amount_yuan, benchmark_name

口径：Tushare `index_daily` 的 amount 单位是**千元**，这里保留原列并另存
`amount_yuan = amount * 1000`，页面只读 amount_yuan，避免把千元当元。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "assets/data/meta/cn_benchmark_daily.parquet"

# 主基准 = 中证全指（覆盖全 A 股，最贴近"小市值策略"的对照物）；其余为对照
BENCHMARKS = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000985.CSI": "中证全指",
    "000688.SH": "科创50",
    "000016.SH": "上证50",
}


def main() -> int:
    ap = argparse.ArgumentParser(description="回补基准指数日线")
    ap.add_argument("--start", default="20150101")
    ap.add_argument("--end", default="")
    args = ap.parse_args()
    end = args.end or datetime.now().strftime("%Y%m%d")

    from data.ingest.providers.cn_tushare_official import TushareOfficialClient

    client = TushareOfficialClient()
    frames = []
    for code, name in BENCHMARKS.items():
        try:
            df = client.get("index_daily", ts_code=code, start_date=args.start, end_date=end)
        except Exception as exc:
            print(f"[benchmark] {code} {name} 拉取失败：{str(exc)[:100]}", flush=True)
            continue
        if df is None or df.empty:
            print(f"[benchmark] {code} {name} 返回空（注意中证系列要 .CSI 后缀）", flush=True)
            continue
        wanted = ["ts_code", "trade_date", "open", "high", "low", "close",
                  "pre_close", "change", "pct_chg", "vol", "amount"]
        for col in wanted:
            if col not in df.columns:
                df[col] = pd.NA
        df = df[wanted].copy()
        df["benchmark_name"] = name
        # Tushare index_daily: amount 千元 -> 元；vol 原样保留（指数成交量）。
        df["amount_yuan"] = pd.to_numeric(df["amount"], errors="coerce") * 1000.0
        frames.append(df)
        d = df.sort_values("trade_date")
        print(f"[benchmark] {code} {name}: {len(df)} 行 {d.trade_date.iloc[0]} → {d.trade_date.iloc[-1]}",
              flush=True)

    if not frames:
        raise SystemExit("一个基准都没拉到")
    out = pd.concat(frames, ignore_index=True)
    out["trade_date"] = pd.to_datetime(out["trade_date"], format="%Y%m%d", errors="coerce")
    for col in ["open", "high", "low", "close", "pre_close", "change",
                "pct_chg", "vol", "amount", "amount_yuan"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["trade_date", "close"]).sort_values(["ts_code", "trade_date"])
    out = out.drop_duplicates(["ts_code", "trade_date"], keep="last")
    out.to_parquet(OUT, index=False)
    print(f"[benchmark] 写出 {OUT.relative_to(ROOT)}｜{len(out):,} 行 / {out['ts_code'].nunique()} 个指数", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
