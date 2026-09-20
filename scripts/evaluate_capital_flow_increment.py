#!/usr/bin/env python
"""Evaluate price-only vs capital-flow-enhanced cross-sectional ranking.

This is deliberately factor-level rather than a claim about a retrained model:
both variants use the same OHLCV universe, point-in-time trade date and five-day
forward close return.  It is useful while the 9/11 model artifact is frozen and
the new capital panel is being rebuilt.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds


ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "assets/data/raw/moneyflow_snapshots"
OHLCV = ROOT / "assets/data/clean/ohlcv"
OUT = ROOT / "output/research"


def read_ohlcv(start: str, end: str) -> pd.DataFrame:
    dataset = ds.dataset(str(OHLCV), format="parquet", partitioning="hive")
    cols = ["trade_date", "stock_code", "close", "amount", "volume"]
    frame = dataset.to_table(columns=cols, filter=(ds.field("market") == "CN") & (ds.field("frequency") == "daily") & (ds.field("adjust") == "qfq")).to_pandas()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame = frame[(frame.trade_date >= pd.Timestamp(start)) & (frame.trade_date <= pd.Timestamp(end))]
    frame = frame.sort_values(["stock_code", "trade_date"]).drop_duplicates(["stock_code", "trade_date"])
    g = frame.groupby("stock_code", sort=False)
    frame["ret_20"] = g["close"].pct_change(20)
    frame["forward_return_5d"] = g["close"].shift(-5) / frame["close"] - 1.0
    return frame


def read_moneyflow(start: str, end: str) -> pd.DataFrame:
    files = [str(p) for p in RAW.glob("moneyflow_*.parquet") if "_dc_" not in p.name and "_ths_" not in p.name]
    frames = []
    use = ["trade_date", "stock_code", "buy_lg_amount", "sell_lg_amount", "buy_elg_amount", "sell_elg_amount", "net_mf_amount"]
    for p in files:
        columns = set(pd.read_parquet(p, engine="pyarrow").columns)
        if not set(use).issubset(columns):
            continue
        frames.append(pd.read_parquet(p, columns=use))
    if not frames:
        return pd.DataFrame()
    f = pd.concat(frames, ignore_index=True)
    f["trade_date"] = pd.to_datetime(f["trade_date"], errors="coerce")
    f = f[(f.trade_date >= pd.Timestamp(start)) & (f.trade_date <= pd.Timestamp(end))]
    for c in use[2:]:
        f[c] = pd.to_numeric(f[c], errors="coerce")
    f["main_net"] = f["buy_lg_amount"].fillna(0) + f["buy_elg_amount"].fillna(0) - f["sell_lg_amount"].fillna(0) - f["sell_elg_amount"].fillna(0)
    f["dare_net"] = f["buy_elg_amount"].fillna(0) - f["sell_elg_amount"].fillna(0)
    f = f.sort_values(["stock_code", "trade_date"]).drop_duplicates(["stock_code", "trade_date"], keep="last")
    g = f.groupby("stock_code", sort=False)
    for name, col in [("main", "main_net"), ("dare", "dare_net")]:
        f[f"{name}_net_3d"] = g[col].rolling(3, min_periods=1).sum().reset_index(level=0, drop=True)
        f[f"{name}_net_10d"] = g[col].rolling(10, min_periods=3).sum().reset_index(level=0, drop=True)
    f["flow_score_raw"] = f["main_net_3d"] + 0.5 * f["dare_net_3d"]
    return f[["trade_date", "stock_code", "main_net_3d", "main_net_10d", "dare_net_3d", "dare_net_10d", "flow_score_raw"]]


def cs_rank(frame: pd.DataFrame, col: str) -> pd.Series:
    return frame.groupby("trade_date")[col].rank(pct=True)


def metrics(frame: pd.DataFrame, score: str, top_pct: float = 0.10) -> dict:
    daily = []
    top = []
    bottom = []
    selected = {}
    for date, g in frame.groupby("trade_date"):
        g = g[[score, "forward_return_5d", "stock_code"]].dropna()
        if len(g) < 30:
            continue
        ic = g[score].corr(g.forward_return_5d, method="pearson")
        ric = g[score].corr(g.forward_return_5d, method="spearman")
        n = max(1, int(np.ceil(len(g) * top_pct)))
        hi = g.nlargest(n, score)
        lo = g.nsmallest(n, score)
        daily.append((date, ic, ric))
        top.append(float(hi.forward_return_5d.mean()))
        bottom.append(float(lo.forward_return_5d.mean()))
        selected[str(date.date())] = hi.stock_code.astype(str).tolist()
    d = pd.DataFrame(daily, columns=["trade_date", "ic", "rank_ic"])
    def avg(col): return float(d[col].mean()) if not d.empty else None
    def ir(col):
        s = d[col].dropna()
        return float(s.mean() / s.std(ddof=1)) if len(s) > 1 and s.std(ddof=1) > 0 else None
    return {
        "dates": int(len(d)), "ic_mean": avg("ic"), "rank_ic_mean": avg("rank_ic"),
        "ic_ir": ir("ic"), "rank_ic_ir": ir("rank_ic"),
        "future_5d_top10_mean": float(np.mean(top)) if top else None,
        "future_5d_bottom10_mean": float(np.mean(bottom)) if bottom else None,
        "future_5d_long_short_mean": float(np.mean(np.asarray(top) - np.asarray(bottom))) if top else None,
        "top10_selection_count": int(sum(len(v) for v in selected.values())),
        "selection_by_date": selected,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-08-23")
    ap.add_argument("--end", default="2026-09-18")
    ap.add_argument("--cutoff", default="2026-09-11")
    args = ap.parse_args()
    bars = read_ohlcv(args.start, args.end)
    flow = read_moneyflow(args.start, args.end)
    frame = bars.merge(flow, on=["trade_date", "stock_code"], how="left")
    for c in ["main_net_3d", "dare_net_3d", "flow_score_raw"]:
        frame[c] = frame[c].fillna(0.0)
    frame["price_score"] = cs_rank(frame, "ret_20")
    frame["flow_score"] = cs_rank(frame, "flow_score_raw")
    frame["enhanced_score"] = 0.65 * frame["price_score"] + 0.35 * frame["flow_score"]
    cutoff = pd.Timestamp(args.cutoff)
    result = {
        "method": "price_20d_rank vs 0.65*price_20d_rank + 0.35*main/dare capital rank",
        "window": {"start": args.start, "end": args.end, "cutoff": args.cutoff},
        "rows": int(len(frame)), "stock_count": int(frame.stock_code.nunique()),
        "flow_match_ratio": float(frame.flow_score_raw.notna().mean()),
        "all_period": {"baseline_price_only": metrics(frame, "price_score"), "capital_enhanced": metrics(frame, "enhanced_score")},
        "before_cutoff": {"baseline_price_only": metrics(frame[frame.trade_date < cutoff], "price_score"), "capital_enhanced": metrics(frame[frame.trade_date < cutoff], "enhanced_score")},
        "after_cutoff": {"baseline_price_only": metrics(frame[frame.trade_date >= cutoff], "price_score"), "capital_enhanced": metrics(frame[frame.trade_date >= cutoff], "enhanced_score")},
        "caveat": "This is a factor-level ablation. A true post-9/11 model gain requires retraining LightGBM/Transformer on a rebuilt full capital feature panel; cyq_chips is currently only 66 stocks.",
    }
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"capital_flow_incremental_eval_{pd.Timestamp.utcnow():%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(json.dumps({k: v for k, v in result.items() if k not in {"all_period", "before_cutoff", "after_cutoff"}}, ensure_ascii=False, indent=2, default=str))
    for split in ["all_period", "before_cutoff", "after_cutoff"]:
        print(split, json.dumps({k: {m: {x: y for x, y in q.items() if x != "selection_by_date"} for m, q in v.items()} for k, v in result[split].items()}, ensure_ascii=False))
    print("report=", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
