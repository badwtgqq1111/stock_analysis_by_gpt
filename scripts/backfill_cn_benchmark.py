#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""回补 A 股基准指数日线（供组合绩效的 α/β/信息比率/基准波动率 使用）。

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
  assets/data/meta/cn_benchmark_daily.parquet（trade_date, ts_code, close, pct_chg）
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
    "000985.CSI": "中证全指",
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000001.SH": "上证指数",
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
        df = df[["ts_code", "trade_date", "close", "pct_chg"]].copy()
        df["benchmark_name"] = name
        frames.append(df)
        d = df.sort_values("trade_date")
        print(f"[benchmark] {code} {name}: {len(df)} 行 {d.trade_date.iloc[0]} → {d.trade_date.iloc[-1]}",
              flush=True)

    if not frames:
        raise SystemExit("一个基准都没拉到")
    out = pd.concat(frames, ignore_index=True)
    out["trade_date"] = pd.to_datetime(out["trade_date"], format="%Y%m%d", errors="coerce")
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    out["pct_chg"] = pd.to_numeric(out["pct_chg"], errors="coerce")
    out = out.dropna(subset=["trade_date", "close"]).sort_values(["ts_code", "trade_date"])
    out.to_parquet(OUT, index=False)
    print(f"[benchmark] 写出 {OUT.relative_to(ROOT)}｜{len(out):,} 行 / {out['ts_code'].nunique()} 个指数", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
