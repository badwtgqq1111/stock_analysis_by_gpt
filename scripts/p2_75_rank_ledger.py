#!/usr/bin/env python3
"""P2.75 二次排序台账 + 前向评估。

读取 `output/research/p2_75_candidate_rank/<date>.json`，追加到 `ledger.csv`；
对已有足够未来数据的日期，计算 5/10/21 日相对中证500 的超额收益，并汇总：
  * 每个决策日的 RankIC（composite vs fwd21 超额）
  * Top20 超额 vs 候选池均值
  * 各分量（prior / ai / research / wyckoff / chip）的 RankIC

用法：
  python3 scripts/p2_75_rank_ledger.py --update
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services.data_service import load_cn_stock_ohlcv  # noqa: E402

RANK_DIR = ROOT / "output/research/p2_75_candidate_rank"
LEDGER = RANK_DIR / "ledger.csv"
SUMMARY = RANK_DIR / "ledger_summary.json"
BENCH = ROOT / "assets/data/meta/cn_benchmark_daily.parquet"


def _bench() -> pd.DataFrame:
    b = pd.read_parquet(BENCH, columns=["ts_code", "trade_date", "close"])
    b = b[b["ts_code"] == "000905.SH"].copy()
    b["trade_date"] = pd.to_datetime(b["trade_date"]).dt.normalize()
    return b.sort_values("trade_date").drop_duplicates("trade_date", keep="last").set_index("trade_date")


def _forward(frame: pd.DataFrame, day: pd.Timestamp, horizon: int) -> float | None:
    if frame is None or frame.empty or day not in frame.index:
        return None
    idx = frame.index.get_loc(day)
    if isinstance(idx, slice) or idx + horizon >= len(frame):
        return None
    start = float(frame["Close"].iloc[idx])
    end = float(frame["Close"].iloc[idx + horizon])
    return end / start - 1.0 if start > 0 else None


def _excess(bench: pd.DataFrame, day: pd.Timestamp, horizon: int, stock_ret: float | None) -> float | None:
    if stock_ret is None or day not in bench.index:
        return None
    idx = bench.index.get_loc(day)
    if isinstance(idx, slice) or idx + horizon >= len(bench):
        return None
    b0, b1 = float(bench["close"].iloc[idx]), float(bench["close"].iloc[idx + horizon])
    return stock_ret - (b1 / b0 - 1.0)


def _rank_ic(frame: pd.DataFrame, score: str, target: str) -> float | None:
    sub = frame[[score, target]].dropna()
    if len(sub) < 10 or sub[score].nunique() < 3:
        return None
    return float(sub[score].corr(sub[target], method="spearman"))


def build(update_all: bool = True) -> dict:
    files = sorted(p for p in RANK_DIR.glob("*.json") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem))
    if not files:
        raise SystemExit("没有排序产物")
    bench = _bench()
    bars_cache: dict[str, pd.DataFrame | None] = {}
    rows = []
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        date = str(payload.get("date") or path.stem)
        day = pd.Timestamp(date)
        for item in payload.get("rows") or []:
            code = str(item.get("code") or "")
            if code not in bars_cache:
                bars_cache[code] = load_cn_stock_ohlcv(code, days=800)
            frame = bars_cache[code]
            rec = {
                "date": date, "rank": item.get("rank"), "code": code, "name": item.get("name"),
                "composite": item.get("composite"), "core_score": item.get("core_score"),
                "overlay_score": item.get("overlay_score"), "coverage": item.get("coverage"),
                "risk_flags": item.get("risk_flags"), "ai_action": item.get("ai_action"),
                "research_stance": item.get("research_stance"),
                "z_prior": item.get("z_prior"), "z_ai": item.get("z_ai"),
                "z_research": item.get("z_research"), "z_wyckoff": item.get("z_wyckoff"),
                "z_chip": item.get("z_chip"),
            }
            for h in (5, 10, 21):
                ret = _forward(frame, day, h)
                rec[f"fwd{h}"] = ret
                rec[f"fwd{h}_excess"] = _excess(bench, day, h, ret)
            rows.append(rec)
    ledger = pd.DataFrame(rows).sort_values(["date", "rank"], ascending=[True, True])
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    ledger.to_csv(LEDGER, index=False)

    by_date = []
    for date, part in ledger.groupby("date"):
        ic = _rank_ic(part, "composite", "fwd21_excess")
        top = part.nsmallest(20, "rank")
        pool = part["fwd21_excess"].dropna()
        top_excess = float(top["fwd21_excess"].dropna().mean()) if top["fwd21_excess"].notna().any() else None
        by_date.append({
            "date": date, "n": int(len(part)), "ic21": ic,
            "top20_excess": top_excess,
            "pool_excess": float(pool.mean()) if len(pool) else None,
            "top20_minus_pool": (top_excess - float(pool.mean())) if top_excess is not None and len(pool) else None,
        })
    ics = [x["ic21"] for x in by_date if x["ic21"] is not None]
    summary = {
        "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "dates": by_date,
        "n_dates": len(by_date),
        "n_dates_with_ic": len(ics),
        "mean_ic21": float(np.mean(ics)) if ics else None,
        "ic_t": float(np.mean(ics) / (np.std(ics, ddof=1) / math.sqrt(len(ics)))) if len(ics) >= 2 and np.std(ics, ddof=1) > 0 else None,
        "top20_excess_mean": float(np.mean([x["top20_minus_pool"] for x in by_date if x["top20_minus_pool"] is not None])) if any(x["top20_minus_pool"] is not None for x in by_date) else None,
    }
    SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("n_dates", "n_dates_with_ic", "mean_ic21", "ic_t", "top20_excess_mean")}, ensure_ascii=False))
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="P2.75 排序台账/前向评估")
    ap.add_argument("--update", action="store_true", help="重建全部（默认即是）")
    build(update_all=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
