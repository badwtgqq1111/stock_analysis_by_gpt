#!/usr/bin/env python3
"""Daily research-only HS300 original and dynamic-diagnostic lists (no orders).

Uses historical index weights, 63-session tradability, strictly prior announcements,
and exact-date daily_basic capitalization. No replay outcome is used to rank names.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.validate_jq_multi_factor import finance_asof

INPUTS = ROOT / "output/research/jq_multi_factor/inputs"
OUT = ROOT / "output/results_cn"


def _client():
    from data.ingest.providers.cn_tushare_official import TushareOfficialClient
    from data.store.scheduler_env import load_scheduler_env
    load_scheduler_env()
    return TushareOfficialClient(timeout=40)


def score_frame(frame: pd.DataFrame, variant: str) -> pd.DataFrame:
    """Match replay ranks: rank on the eligible universe before dropping missing scores."""
    if variant not in ("original", "dynamic"):
        raise ValueError(f"Unknown variant {variant}")
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna(subset=["roe", "market_cap"])
    frame = frame[frame.market_cap > 0].copy()
    frame["roe_rank"] = frame.roe.rank(pct=True)
    frame["market_cap_rank"] = frame.market_cap.rank(pct=True)
    if variant == "dynamic":
        frame["roa_rank"] = frame.roa.rank(pct=True)
        frame["score"] = frame.roe_rank + 0.25 * frame.roa_rank
    else:
        frame["score"] = frame.roe_rank - frame.market_cap_rank
    return frame.dropna(subset=["score"])


def select(day: str, variant: str = "original") -> pd.DataFrame:
    if variant not in ("original", "dynamic"):
        raise ValueError(f"Unknown variant {variant}")
    target = pd.Timestamp(day)
    INPUTS.mkdir(parents=True, exist_ok=True)
    weights_file = INPUTS / f"hs300_weights_{target.year}.parquet"
    if not weights_file.exists():
        weights = pd.DataFrame()
    else:
        weights = pd.read_parquet(weights_file)
    last_roster = pd.to_datetime(weights.trade_date.astype(str), errors="coerce").max() if not weights.empty else pd.NaT
    if pd.isna(last_roster) or (target - last_roster).days > 65:
        fresh = _client().get("index_weight", index_code="000300.SH",
                              start_date=f"{target.year}0101", end_date=f"{target:%Y%m%d}", limit=5000)
        if fresh.empty or len(fresh) >= 5000:
            raise ValueError("Missing/truncated historical HS300 roster refresh")
        weights = fresh.drop_duplicates(["trade_date", "con_code"])
        weights.to_parquet(weights_file, index=False)
    weights.trade_date = pd.to_datetime(weights.trade_date)
    weights = weights[weights.trade_date <= target]
    if weights.empty:
        raise ValueError("No historical HS300 roster")
    roster_day = weights.trade_date.max()
    if (target - roster_day).days > 65:
        raise ValueError(f"Stale HS300 roster {roster_day.date()}")
    codes = sorted(set(weights.loc[weights.trade_date == roster_day, "con_code"]))

    cap_files = sorted((ROOT / "assets/data/raw/moneyflow_snapshots").glob(
        f"daily_basic_{target:%Y%m%d}_*.parquet"))
    cap_file = (INPUTS / f"daily_basic_{target:%Y%m%d}.parquet")
    if cap_files:
        cap_file = cap_files[-1]
    if not cap_file.exists():
        cap = _client().get("daily_basic", trade_date=f"{target:%Y%m%d}",
                            fields="ts_code,trade_date,total_mv", limit=6000)
        if cap.empty or len(cap) >= 6000:
            raise ValueError(f"Missing/truncated exact-date capitalization {day}")
        cap.to_parquet(cap_file, index=False)
    cap = pd.read_parquet(cap_file)
    cap = cap[pd.to_datetime(cap.trade_date) == target]
    if "ts_code" not in cap and "stock_code" in cap:
        cap = cap.rename(columns={"stock_code": "ts_code"})
    cap = cap.drop_duplicates("ts_code")[["ts_code", "total_mv"]].rename(
        columns={"ts_code": "stock_code", "total_mv": "market_cap"})
    cap.market_cap = pd.to_numeric(cap.market_cap, errors="coerce") / 1e4

    files = [str(p) for year in (target.year - 1, target.year)
             for p in (ROOT / "assets/data/clean/ohlcv/market=CN").glob(
                 f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={year}/*.parquet")]
    bars = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "volume", "close"],
        filter=ds.field("stock_code").isin(codes)).to_pandas()
    bars.trade_date = pd.to_datetime(bars.trade_date)
    bars = bars[bars.trade_date <= target].drop_duplicates(["trade_date", "stock_code"])
    days = sorted(bars.trade_date.unique())
    # 候选池的 `day` 来自**中证500 基准的最新交易日**，而当日日线要等流水线的 daily_bars 阶段
    # 才落地 ⇒ 两者有一个时间差窗口。原来这里直接抛 `No close bar on 2026-10-15`，
    # 结果 2026-10-15 18:54 的 refresh_strategy_lists 把两条聚宽策略记成 failed，
    # 每日预检的 `test_strategy_lists_contract` 因此变红、自动化被挡在门外（第九十八轮实测）。
    # 现在退到"≤ 基准日的最后一个真实收盘日"，并把退让**写进日志**（不静默）。
    if not days:
        raise ValueError("No close bar at all for the requested universe")
    if target not in days:
        settled = days[-1]
        print(f"[jq] 目标日 {day} 还没有收盘日线（基准走在前头）⇒ 退到最近收盘日 "
              f"{pd.Timestamp(settled).date()}；名单日期会如实落后一个交易日", flush=True)
        target = settled
        day = str(pd.Timestamp(settled).date())
    last63 = days[-63:]
    if len(last63) < 63:
        raise ValueError("Fewer than 63 market sessions")
    recent = bars[bars.trade_date.isin(last63)].copy()
    recent["valid"] = recent.volume.gt(0) & recent.close.gt(0)
    valid = recent.groupby("stock_code").valid.sum()
    codes = sorted(c for c in codes if valid.get(c, 0) == 63)
    grid = pd.DataFrame({"trade_date": target, "stock_code": codes})
    fin = pd.read_parquet(ROOT / "assets/data/meta/cn_financial_indicators.parquet",
                          columns=["ts_code", "ann_date", "end_date", "roe"]
                          + (["roa"] if variant == "dynamic" else []))
    frame = finance_asof(grid, fin).merge(cap, on="stock_code", how="left")
    frame = score_frame(frame, variant)
    if len(frame) < 20:
        raise ValueError(f"Only {len(frame)} PIT-qualified names")
    # Per-date percentile ranks, with deterministic stock-code tie-breaking.
    frame = frame.sort_values(["score", "stock_code"], ascending=[False, True]).head(20)
    frame.insert(0, "rank", range(1, 21))
    frame["target_weight"] = 1 / 20
    frame["trade_date"] = day
    columns = ["trade_date", "rank", "stock_code", "roe", "market_cap", "roe_rank",
               "market_cap_rank", "score", "target_weight", "ann_date", "end_date"]
    if variant == "dynamic":
        columns.insert(4, "roa")
        columns.insert(7, "roa_rank")
    return frame[columns]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="", help="Signal trading date; default latest CSI500 date")
    ap.add_argument("--variant", choices=("original", "dynamic", "both"), default="both")
    args = ap.parse_args()
    if args.date:
        day = args.date
    else:
        bench = pd.read_parquet(ROOT / "assets/data/meta/cn_benchmark_daily.parquet")
        day = str(pd.to_datetime(bench[bench.ts_code == "000905.SH"].trade_date).max().date())
    OUT.mkdir(parents=True, exist_ok=True)
    for variant in (("original", "dynamic") if args.variant == "both" else (args.variant,)):
        result = select(day, variant)
        # ⚠️ `select()` 可能因为"目标日还没有收盘日线"而**退到最近收盘日**（见里面的注释），
        # 此时它以 `result` 的 trade_date 为准；沿用入参 `day` 会把名单日期写成 10-15
        # 而信号其实来自 10-14（实测踩到：日志说退到 10-14，产物却写 10-15）。
        settled = str(pd.to_datetime(result["trade_date"]).max().date()) if len(result) else day
        stem = "jq_multi_factor" if variant == "original" else "jq_roe_roa"
        history = OUT / f"{stem}_history"
        history.mkdir(exist_ok=True)
        result.to_csv(OUT / f"{stem}_selection.csv", index=False)
        result.to_csv(history / f"{settled}.csv", index=False)
        metadata = {
            "trade_date": settled,
            "strategy_id": "jq_multi_factor_size_roe" if variant == "original" else "jq_roe_roa_diagnostic",
            "stage": "candidate",
            "formula": ("rank(roe)-rank(market_cap)" if variant == "original"
                        else "rank(roe)+0.25*rank(roa)"),
            "signal_time": "close", "next_execution": "next trading day open", "count": len(result),
            "roster_source": "historical HS300 index_weight", "capitalization": "exact-date daily_basic",
        }
        if variant == "dynamic":
            metadata.update(variant=variant, selection_note="Daily diagnostic list, not the 15-session research replay")
        (OUT / f"{stem}_selection.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
        print(f"{stem} {settled}: {len(result)} candidates; research only"
              + ("" if settled == day else f"（请求 {day}，当日日线未就绪）"))


if __name__ == "__main__":
    main()
