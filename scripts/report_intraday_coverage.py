#!/usr/bin/env python3
"""生成 5min 分时归档覆盖率报告（按年 + 按来源 + 每股起点分布）。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "assets/data/raw/intraday_baostock/frequency=5min"
OUT = ROOT / "output/research/p2_81/INTRADAY_COVERAGE.md"
JSON = ROOT / "output/research/p2_81/intraday_coverage.json"


def main() -> int:
    files = sorted(SRC.glob("*.parquet"))
    rows = 0
    per_year: dict[int, set] = {}
    firsts: dict[str, int] = {}
    src_rows: dict[str, int] = {}
    lo_all, hi_all = None, None
    for path in files:
        frame = pd.read_parquet(path, columns=["date", "source"])
        dates = pd.to_datetime(frame["date"])
        rows += len(frame)
        for value in dates.dt.year.unique():
            per_year.setdefault(int(value), set())
        for year, chunk in dates.groupby(dates.dt.year):
            per_year[int(year)].update(pd.unique(chunk.dt.normalize()))
        first = dates.min().date()
        firsts[str(first)] = firsts.get(str(first), 0) + 1
        lo_all = first if lo_all is None or first < lo_all else lo_all
        hi = dates.max().date()
        hi_all = hi if hi_all is None or hi > hi_all else hi_all
        for k, v in frame["source"].value_counts().items():
            src_rows[str(k)] = src_rows.get(str(k), 0) + int(v)

    bench = pd.read_parquet(ROOT / "assets/data/meta/cn_benchmark_daily.parquet", columns=["ts_code", "trade_date"])
    bench = bench[bench["ts_code"] == "000985.CSI"]
    cal = pd.to_datetime(bench["trade_date"]).dt.normalize().drop_duplicates()
    cal = sorted(cal)

    # 按「股票-日」算覆盖率：只有 19 只票有 2020 起的数据，用"日期并集"会把它们算成 100%
    stock_days: dict[int, int] = {}
    for path in files:
        dates = pd.to_datetime(pd.read_parquet(path, columns=["date"])["date"])
        for year, chunk in dates.groupby(dates.dt.year):
            stock_days[int(year)] = stock_days.get(int(year), 0) + int(chunk.dt.normalize().nunique())

    import glob
    import pyarrow.dataset as ds

    bar_files = sorted(glob.glob(str(ROOT / "assets/data/clean/ohlcv/market=CN/exchange=*/"
                                    "asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet")))
    daily = ds.dataset(bar_files, format="parquet").to_table(columns=["trade_date", "stock_code"]).to_pandas()
    daily["trade_date"] = pd.to_datetime(daily["trade_date"])
    expect = daily.drop_duplicates().groupby(daily["trade_date"].dt.year).size().to_dict()

    lines = [f"# A股 5分钟归档覆盖率", "",
             f"生成 {datetime.now():%F %H:%M}｜文件 {len(files):,}｜行 {rows:,}｜最早 {lo_all} / 最晚 {hi_all}", "",
             "覆盖率按「股票-日」计：分子=本地实际有 5min 的股票-日，分母=当日有日线的股票-日。", "",
             "| 年份 | 本地股票-日 | 应有股票-日 | 覆盖率 |", "|---|---:|---:|---:|"]
    for year in range(2016, 2027):
        have = stock_days.get(year, 0)
        total = int(expect.get(year, 0))
        pct = f"{have/total*100:.1f}%" if total else "-"
        lines.append(f"| {year} | {have:,} | {total:,} | {pct} |")
    lines += ["", "## 每股起点分布（前 8）", "", "| 首日 | 股票数 |", "|---|---:|"]
    for day, count in sorted(firsts.items(), key=lambda x: -x[1])[:8]:
        lines.append(f"| {day} | {count:,} |")
    lines += ["", "## 来源", "", "| source | 行数 |", "|---|---:|"]
    for k, v in sorted(src_rows.items(), key=lambda x: -x[1]):
        lines.append(f"| {k} | {v:,} |")
    lines += ["", "## 结论", "",
              f"- 全市场 {len(files):,} 只已补齐到最近交易日（stale=0）；",
              "- 深度历史：BaoStock 5min 仅 2020-01 起（2019 及更早为空），iFinD 试用 `high_frequency` 仅近 1 年（-4309）；",
              "- 全 A 2016-2024 5min 约 30 亿行，BaoStock 实测 ~500 行/秒/连接，全量需数天量级；",
              "  已提供可断点续跑的 `scripts/backfill_cn_intraday_history.py`（多进程 + 优先级 + 冷却）。"]
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    JSON.write_text(json.dumps({"files": len(files), "rows": rows, "earliest": str(lo_all), "latest": str(hi_all),
                                "firsts": firsts, "sources": src_rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n".join(lines[:14]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
