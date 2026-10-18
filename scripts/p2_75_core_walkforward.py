#!/usr/bin/env python3
"""P2.75 核心层历史评估（策略先验 + 威科夫 + 筹码）。

数据：`output/results_cn/selection_ledger.csv` 的历史决策日候选池（不是全市场），
标签：未来 21 日相对中证500 的超额；全部 PIT（只用决策日及以前的日线）。

AI/研报不在本脚本里（它们无法历史回测，见 P2.75 §1）。

用法：
  python3 scripts/p2_75_core_walkforward.py --months-step 2
产物：
  output/research/p2_75_candidate_rank/core_walkforward.csv
  output/research/p2_75_candidate_rank/core_walkforward_summary.json
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services.data_service import load_cn_stock_ohlcv  # noqa: E402

SPEC = importlib.util.spec_from_file_location("p2_75_candidate_rank", ROOT / "scripts/p2_75_candidate_rank.py")
RANK = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(RANK)

LEDGER = ROOT / "output/results_cn/selection_ledger.csv"
BENCH = ROOT / "assets/data/meta/cn_benchmark_daily.parquet"
OUT = ROOT / "output/research/p2_75_candidate_rank"
WEIGHTS = {"prior": 0.30, "wyckoff": 0.40, "chip": 0.30}


def _bench() -> pd.DataFrame:
    b = pd.read_parquet(BENCH, columns=["ts_code", "trade_date", "close"])
    b = b[b["ts_code"] == "000905.SH"].copy()
    b["trade_date"] = pd.to_datetime(b["trade_date"]).dt.normalize()
    return b.sort_values("trade_date").drop_duplicates("trade_date", keep="last").set_index("trade_date")


def _fwd_excess(frame: pd.DataFrame, bench: pd.DataFrame, day: pd.Timestamp, horizon: int = 21) -> float | None:
    if frame is None or frame.empty or day not in frame.index or day not in bench.index:
        return None
    i = frame.index.get_loc(day)
    j = bench.index.get_loc(day)
    if isinstance(i, slice) or isinstance(j, slice) or i + horizon >= len(frame) or j + horizon >= len(bench):
        return None
    s0, s1 = float(frame["Close"].iloc[i]), float(frame["Close"].iloc[i + horizon])
    b0, b1 = float(bench["close"].iloc[j]), float(bench["close"].iloc[j + horizon])
    if s0 <= 0 or b0 <= 0:
        return None
    return (s1 / s0 - 1.0) - (b1 / b0 - 1.0)


def _ic(frame: pd.DataFrame, score: str, target: str) -> float | None:
    sub = frame[[score, target]].dropna()
    if len(sub) < 15 or sub[score].nunique() < 3:
        return None
    return float(sub[score].corr(sub[target], method="spearman"))


def run(months_step: int = 2, horizon: int = 21) -> dict:
    ledger = pd.read_csv(LEDGER, usecols=["decision_date", "source", "stock_code"])
    ledger["decision_date"] = pd.to_datetime(ledger["decision_date"])
    ledger = ledger[(ledger["decision_date"] >= "2024-01-01") & (ledger["decision_date"] <= "2026-06-30")]
    # 每 N 个月的最后一个决策日
    ledger["ym"] = ledger["decision_date"].dt.to_period("M")
    months = sorted(ledger["ym"].unique())
    picked = months[:: max(1, int(months_step))]
    # 每个月只取**最后一个决策日**（月频调仓口径），避免同一个月内多天样本互相重叠
    dates = sorted(ledger[ledger["ym"].isin(picked)].groupby("ym")["decision_date"].max().tolist())
    print(f"[core] {len(dates)} decision dates: {dates[0]} .. {dates[-1]}", flush=True)

    bench = _bench()
    bars_cache: dict[str, pd.DataFrame | None] = {}
    rows = []
    by_date = []
    for n, day in enumerate(dates, 1):
        day = pd.Timestamp(day)
        part = ledger[ledger["decision_date"] == day]
        picks = part.groupby("stock_code")["source"].nunique().reset_index()
        picks.columns = ["code", "prior"]
        if len(picks) < 20:
            continue
        records = []
        for r in picks.itertuples():
            code = str(r.code)
            if code not in bars_cache:
                bars_cache[code] = load_cn_stock_ohlcv(code, days=500)
            frame = bars_cache[code]
            if frame is None or frame.empty:
                continue
            hist = frame[frame.index <= day]
            if len(hist) < 80:
                continue
            chip = RANK._chip_stats(hist)
            wy, wso, pos = RANK._wyckoff_score(hist)
            records.append({
                "code": code, "prior": float(r.prior), "wyckoff": wy, "chip_raw": chip,
                "wso": wso, "pos": pos,
                "fwd21_excess": _fwd_excess(frame, bench, day, horizon),
            })
        frame = pd.DataFrame(records)
        if frame.empty:
            continue
        profit = frame["chip_raw"].apply(lambda x: (x or {}).get("profit_pct"))
        conc = frame["chip_raw"].apply(lambda x: (x or {}).get("concentration"))
        peak_dist = frame["chip_raw"].apply(lambda x: (x or {}).get("peak_dist"))
        profit = pd.to_numeric(profit, errors="coerce") / 100.0
        frame["chip"] = (
            0.4 * (1.0 - (profit - 0.5).abs() / 0.5)
            + 0.3 * (-pd.to_numeric(conc, errors="coerce"))
            + 0.3 * np.clip(pd.to_numeric(peak_dist, errors="coerce") / 0.10, -1, 1)
        )
        for key in ("prior", "wyckoff", "chip"):
            frame[f"z_{key}"] = RANK._zscore(frame[key])
        avail = sum(frame[f"z_{k}"].notna().astype(float) * w for k, w in WEIGHTS.items())
        frame["core"] = sum(frame[f"z_{k}"].fillna(0.0) * w for k, w in WEIGHTS.items()) / avail.replace(0, np.nan)
        frame["date"] = day.date().isoformat()
        rows.append(frame)
        top = frame.nlargest(20, "core")["fwd21_excess"].dropna()
        pool = frame["fwd21_excess"].dropna()
        by_date.append({
            "date": frame["date"].iloc[0], "n": int(len(frame)),
            "ic": _ic(frame, "core", "fwd21_excess"),
            "ic_prior": _ic(frame, "z_prior", "fwd21_excess"),
            "ic_wyckoff": _ic(frame, "z_wyckoff", "fwd21_excess"),
            "ic_chip": _ic(frame, "z_chip", "fwd21_excess"),
            "top20": float(top.mean()) if len(top) else None,
            "pool": float(pool.mean()) if len(pool) else None,
            "top20_minus_pool": (float(top.mean()) - float(pool.mean())) if len(top) and len(pool) else None,
        })
        print(f"[{n}/{len(dates)}] {frame['date'].iloc[0]} n={len(frame)} ic={by_date[-1]['ic']}", flush=True)

    detail = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    OUT.mkdir(parents=True, exist_ok=True)
    detail.to_csv(OUT / "core_walkforward.csv", index=False)
    ics = [x["ic"] for x in by_date if x["ic"] is not None]
    summary = {
        "dates": by_date,
        "n_dates": len(by_date),
        "mean_ic": float(np.mean(ics)) if ics else None,
        "ic_t": float(np.mean(ics) / (np.std(ics, ddof=1) / math.sqrt(len(ics)))) if len(ics) >= 2 and np.std(ics, ddof=1) > 0 else None,
        "mean_top20_minus_pool": float(np.mean([x["top20_minus_pool"] for x in by_date if x["top20_minus_pool"] is not None])) if any(x["top20_minus_pool"] is not None for x in by_date) else None,
        "weights": WEIGHTS,
        "horizon": horizon,
    }
    (OUT / "core_walkforward_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("n_dates", "mean_ic", "ic_t", "mean_top20_minus_pool")}, ensure_ascii=False))
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="P2.75 核心层 walk-forward")
    ap.add_argument("--months-step", type=int, default=2)
    ap.add_argument("--horizon", type=int, default=21)
    args = ap.parse_args()
    run(args.months_step, args.horizon)
    return 0


if __name__ == "__main__":
    sys.exit(main())
