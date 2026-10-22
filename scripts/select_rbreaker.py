#!/usr/bin/env python3
"""Daily R-Breaker candidate list for the strategy pool.

The backtest lives in `scripts/rbreaker_study.py`. This script only produces the
next-session candidates: stocks whose next-session buy-stop would sit at R3,
using the latest close's H/L/C and pre-open volume/liquidity filters.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import rbreaker_core as core  # noqa: E402

RESULTS = ROOT / "output/results_cn"
HISTORY = RESULTS / "rbreaker_history"
LATEST = RESULTS / "rbreaker_latest_selection.csv"
LATEST_JSON = RESULTS / "rbreaker_latest_selection.json"


def board(code: str) -> str:
    code = str(code)
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith(("300", "301")):
        return "创业板"
    if code.startswith(("4", "8", "92")):
        return "北交所"
    return "主板"


def main() -> int:
    ap = argparse.ArgumentParser(description="R-Breaker 日线候选名单")
    ap.add_argument("--date", default="", help="决策日；留空取最新交易日")
    ap.add_argument("--slots", type=int, default=5)
    args = ap.parse_args()

    end = pd.Timestamp(args.date) if args.date else pd.Timestamp.today().normalize()
    bars = core.load_bars(end - pd.Timedelta(days=60), end)
    if bars.empty:
        raise SystemExit("没有 CN 日线")
    params = core.RBreakerParams(slots=args.slots)
    bars = core.add_signals(bars, params)
    latest = pd.Timestamp(args.date) if args.date else bars["trade_date"].max()
    day = bars[bars["trade_date"] == latest].copy()
    if day.empty:
        raise SystemExit(f"{latest.date()} 没有行情")
    candidates = day[day["stop_candidate"]].sort_values(
        ["stop_score", "amount_ma20_prev"], ascending=False).head(args.slots)

    rows = []
    for rank, (_, r) in enumerate(candidates.iterrows(), 1):
        close = float(r["close"])
        target = params.gross_exposure / args.slots
        rows.append({
            "trade_date": latest.date().isoformat(),
            "rank": rank,
            "stock_code": str(r["stock_code"]),
            "name": str(r["name"]),
            "board": board(str(r["stock_code"])),
            "industry": str(r.get("industry") or ""),
            "score": round(float(r["stop_score"]), 6),
            "r3": round(float(r["rb_r3"]), 6),
            "r2": round(float(r["rb_r2"]), 6),
            "r3_distance": round(float(r["r3_distance"]), 6),
            "volume_ratio_prev": round(float(r["vol_ratio_prev"]), 6),
            "last_close": round(close, 4),
            "target_weight": round(target, 6),
            "lots_at_capital": int((params.initial_capital * target) // (close * params.lot_size)),
            "planned_amount": round(params.initial_capital * target, 2),
            "in_portfolio": False,
            "stage": "下一交易日 R3 买入止损候选",
        })

    columns = [
        "trade_date", "rank", "stock_code", "name", "board", "industry", "score",
        "r3", "r2", "r3_distance", "volume_ratio_prev", "last_close",
        "target_weight", "lots_at_capital", "planned_amount", "in_portfolio", "stage",
    ]
    frame = pd.DataFrame(rows, columns=columns)
    RESULTS.mkdir(parents=True, exist_ok=True)
    HISTORY.mkdir(parents=True, exist_ok=True)
    frame.to_csv(LATEST, index=False)
    frame.to_csv(HISTORY / f"{latest.date()}.csv", index=False)
    payload = {
        "date": latest.date().isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n": len(frame),
        "params": params.__dict__,
        "latest_selection": str(LATEST.relative_to(ROOT)),
        "history": str((HISTORY / f"{latest.date()}.csv").relative_to(ROOT)),
        "methodology": (
            "标准 R-Breaker：R2=P+(H-L)，R3=H+2(P-L)；下一交易日在 R3 挂买入止损，"
            "量能/流动性使用前一日数据；A股 T+1 下持仓后可挂 R2 卖出止损。"
            "本文件只给候选，不代表已成交；完整净值见 output/research/rbreaker/。"
        ),
        "rows": rows,
    }
    LATEST_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[rbreaker] {latest.date()} candidates={len(frame)} -> {LATEST.relative_to(ROOT)}", flush=True)
    if not frame.empty:
        print(frame[["rank", "stock_code", "name", "r3", "r3_distance", "volume_ratio_prev"]]
              .head(10).to_string(index=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
