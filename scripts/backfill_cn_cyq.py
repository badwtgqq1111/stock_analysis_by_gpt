#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""补「筹码分布」数据（tushare `cyq_perf`）：主力追踪的第三条线（用户 2026-10-14）。

问题："筹码分布 有作为主力追踪参考之一么？" —— **有**，而且是 A 股最老牌的"主力追踪"工具之一
（陈浩《筹码分布》一脉：把成交量按价格分布累积成"持仓成本分布"，用**获利盘比例 / 成本分位 / 集中度**
推断主力是否控盘）。tushare 直接给标准口径：

    cyq_perf(ts_code, start_date, end_date)   每日筹码及胜率：cost_5pct/15pct/50pct/85pct/95pct + weight_avg + winner_rate
    cyq_chips(ts_code, trade_date)            每日筹码分布明细（价格 × 占比，~99 档；按票拉，适合单票展示）

**关键效率**：`cyq_perf(trade_date=...)` **一次返回全市场**（实测 2026-10-13 → 5,561 只）⇒ 按交易日补库，
570 个交易日只需 570 次调用（不是 5400×570）。

产物：
  * 原始快照：`assets/data/raw/cyq_perf_snapshots/cyq_perf_<日期>.parquet`
  * 派生表： `assets/data/derived/cn_cyq_perf.parquet`（按 trade_date+ts_code 去重）
  * 特征表： `assets/data/derived/cn_chip_features.parquet`
      - `chip_winner_rate`：获利盘比例（%）
      - `chip_cost_width`：筹码宽度 = (cost_95pct − cost_5pct) / cost_50pct（**越小越集中 = 单峰密集**）
      - `chip_price_vs_cost`：现价 / 平均成本 − 1（现价相对主力成本的位置）
      - `chip_upper_pressure`：上方套牢盘比例代理 = (cost_95pct − close) / (cost_95pct − cost_5pct)
      - 上述三项的 20 日变化（`_chg20`）
  * 台账：`assets/data/raw/cyq_perf_snapshots/_sync/progress.json`（断点续跑）

用法：
  uv run python scripts/backfill_cn_cyq.py --start 2024-01-01 --dry-run
  uv run python scripts/backfill_cn_cyq.py --start 2024-01-01 --days 10     # 冒烟
  uv run python scripts/backfill_cn_cyq.py --start 2024-01-01               # 全量（约 570 个交易日）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.ingest.providers.cn_tushare_official import TushareOfficialClient  # noqa: E402

RAW = ROOT / "assets/data/raw/cyq_perf_snapshots"
DERIVED = ROOT / "assets/data/derived"
SYNC = RAW / "_sync"
PROGRESS = SYNC / "progress.json"
BARS_DIR = ROOT / "assets/data/clean/ohlcv/market=CN"


def _trading_days(start: str, end: str) -> list[str]:
    """交易日取本地行情里的日期（不猜日历）。"""
    import pyarrow.dataset as ds

    files = [str(p) for year in range(pd.Timestamp(start).year, pd.Timestamp(end).year + 1)
             for p in BARS_DIR.glob(f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={year}/*.parquet")]
    if not files:
        raise SystemExit("找不到本地日线，先跑数据补齐")
    table = ds.dataset(files, format="parquet").to_table(columns=["trade_date"]).to_pandas()
    days = sorted({str(pd.Timestamp(d).date()) for d in table["trade_date"]})
    return [d for d in days if start <= d <= end]


def _load_progress() -> dict:
    if PROGRESS.exists():
        try:
            return json.loads(PROGRESS.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"done_dates": [], "updated_at": None}


def _save_progress(payload: dict) -> None:
    SYNC.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
    PROGRESS.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_features(cyq: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    """筹码特征（PIT：筹码数据当日收盘后可得 ⇒ `available_from` = 下一个交易日）。"""
    if cyq.empty:
        return pd.DataFrame()
    df = cyq.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    for c in ("cost_5pct", "cost_15pct", "cost_50pct", "cost_85pct", "cost_95pct", "weight_avg", "winner_rate"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[(df["cost_50pct"] > 0) & (df["cost_5pct"] > 0) & (df["cost_95pct"] > 0)]
    # tushare 的 winner_rate 有约 0.08% 的行**略高于 100**（实测 max=100.61，四舍五入造成）
    # ⇒ 特征表里夹到 [0,100]，不把这个脏尾巴带给下游因子。
    df["chip_winner_rate"] = pd.to_numeric(df["winner_rate"], errors="coerce").clip(0, 100)
    df["chip_cost_width"] = (df["cost_95pct"] - df["cost_5pct"]) / df["cost_50pct"]
    df["chip_concentration"] = 1.0 / df["chip_cost_width"].replace(0, pd.NA)     # 越大越集中
    close = bars[["stock_code", "trade_date", "close"]].rename(columns={"stock_code": "ts_code"})
    df = df.merge(close, on=["ts_code", "trade_date"], how="left")
    df["chip_price_vs_cost"] = df["close"] / df["weight_avg"] - 1
    df["chip_upper_pressure"] = (df["cost_95pct"] - df["close"]) / (df["cost_95pct"] - df["cost_5pct"])
    df = df.sort_values(["ts_code", "trade_date"])
    for col in ("chip_winner_rate", "chip_cost_width", "chip_concentration", "chip_price_vs_cost",
                "chip_upper_pressure"):
        df[f"{col}_chg20"] = df.groupby("ts_code")[col].diff(20)
    # PIT：当日收盘后可得 ⇒ 下一个交易日才允许使用
    days = pd.DatetimeIndex(sorted(bars["trade_date"].unique()))
    mapping = {d: days[i + 1] for i, d in enumerate(days[:-1])}
    df["available_from"] = df["trade_date"].map(mapping)
    keep = ["ts_code", "trade_date", "available_from", "chip_winner_rate", "chip_cost_width",
            "chip_concentration", "chip_price_vs_cost", "chip_upper_pressure",
            "chip_winner_rate_chg20", "chip_cost_width_chg20", "chip_concentration_chg20",
            "chip_price_vs_cost_chg20", "chip_upper_pressure_chg20",
            "cost_5pct", "cost_50pct", "cost_95pct", "weight_avg"]
    return df[[c for c in keep if c in df.columns]].dropna(subset=["available_from"])


def main() -> int:
    ap = argparse.ArgumentParser(description="补筹码分布（tushare cyq_perf，按交易日全市场一次拿）")
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="")
    ap.add_argument("--days", type=int, default=0, help="只跑最近 N 个交易日（冒烟用）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    end = args.end or str(pd.Timestamp.now().date())
    days = _trading_days(args.start, end)
    if args.days:
        days = days[-args.days:]
    progress = _load_progress()
    todo = [d for d in days if d not in progress["done_dates"]]
    print(f"[cyq] 交易日 {days[0]} → {days[-1]}（{len(days)} 天）｜待补 {len(todo)} 天")
    if args.dry_run:
        return 0
    client = TushareOfficialClient()
    if not client.token:
        raise SystemExit("缺少 TUSHARE_TOKEN")

    RAW.mkdir(parents=True, exist_ok=True)
    t0, rows = time.time(), 0
    for i, day in enumerate(todo, 1):
        stamp = day.replace("-", "")
        path = RAW / f"cyq_perf_{stamp}.parquet"
        if path.exists():
            frame = pd.read_parquet(path)
        else:
            try:
                frame = client.get("cyq_perf", trade_date=stamp)
            except Exception as exc:                                   # noqa: BLE001
                print(f"   ⚠️ {day} 失败：{str(exc)[:90]}")
                continue
            if frame.empty:
                progress["done_dates"].append(day)     # 非交易日/无数据也记账，避免重复请求
                continue
            frame.to_parquet(path, index=False)
        rows += len(frame)
        progress["done_dates"].append(day)
        if i % 25 == 0 or i == len(todo):
            print(f"   ✓ {day} → {len(frame):,} 行（累计 {rows:,}）｜已用 {time.time() - t0:.0f}s")
            _save_progress(progress)
    _save_progress(progress)

    # 合并派生表 + 特征
    files = sorted(RAW.glob("cyq_perf_*.parquet"))
    if files:
        all_df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
        all_df = all_df.drop_duplicates(subset=["ts_code", "trade_date"], keep="last")
        DERIVED.mkdir(parents=True, exist_ok=True)
        all_df.to_parquet(DERIVED / "cn_cyq_perf.parquet", index=False)
        import wyckoff_study as ws
        bars = ws.load_bars(max(args.start, "2024-01-01"))
        feats = build_features(all_df, bars)
        feats.to_parquet(DERIVED / "cn_chip_features.parquet", index=False)
        print(f"[cyq] 派生 cn_cyq_perf.parquet {len(all_df):,} 行｜特征 cn_chip_features.parquet {len(feats):,} 行"
              f"（{feats.ts_code.nunique() if len(feats) else 0} 只，{feats.trade_date.min().date() if len(feats) else '-'} 起）")
    print(f"[cyq] 完成｜耗时 {time.time() - t0:.0f}s｜台账 {PROGRESS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
