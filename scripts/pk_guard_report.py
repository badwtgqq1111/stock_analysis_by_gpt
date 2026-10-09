#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PK 护栏日报：把"不该拿到权重的票"在**下单之前**暴露出来（2026-10-09 第三十五轮）。

为什么需要
----------
第三十四轮的 A/B 发现：真正下单的名单里有 **24% 的笔数给了 rank>20 的票**，尾巴伸到 rank 4017/4055，
而 `rank>20` 恰好是全部复盘假设里**唯一**通过按决策日符号检验的负向特征（23/32 天更差，p=0.02）。
在动生产权重之前，至少要先把这类"违规下单"**每天点出来**，避免它继续静默发生。

本脚本对**最新决策日**的最终名单做三件事（只读，不改生产权重）：
  1. 违规：`rank > rank_cap` 却拿到 `weight > 0` 的票，逐条列出；
  2. 集中度：单票最大权重、Top1 占比、以及"若按 rank≤cap 归一化"的对照权重；
  3. 结论行：`ok` / `violations`，写进 `output/results_cn/pk_guard/<date>.json` 与 `latest.json`。

用法:
  uv run python scripts/pk_guard_report.py                 # 最新决策日
  uv run python scripts/pk_guard_report.py --date 2026-10-08 --rank-cap 20
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
sys.path.insert(0, str(ROOT))

LEDGER = ROOT / "output/results_cn/selection_ledger.parquet"
OUT_DIR = ROOT / "output/results_cn/pk_guard"


def _name(row) -> str:
    value = getattr(row, "name", None)
    return "" if value is None or (isinstance(value, float) and pd.isna(value)) else str(value)


def analyse(rows: pd.DataFrame, rank_cap: int) -> dict:
    frame = rows.copy()
    frame["weight"] = pd.to_numeric(frame["weight"], errors="coerce").fillna(0.0)
    frame["rank"] = pd.to_numeric(frame["rank"], errors="coerce")
    ordered = frame.sort_values(["weight", "rank"], ascending=[False, True])
    buys = ordered[ordered["weight"] > 0]
    violations = buys[buys["rank"] > rank_cap]
    eligible = buys[buys["rank"] <= rank_cap]
    capped = eligible.copy()
    capped["capped_weight"] = (capped["weight"] / capped["weight"].sum()) if len(capped) else np.nan
    total_weight = float(buys["weight"].sum())
    return {
        "n_names": int(len(frame)), "n_holdings": int(len(buys)),
        "total_weight": round(total_weight, 4),
        "max_weight": round(float(buys["weight"].max()), 4) if len(buys) else None,
        "top1_code": str(ordered["stock_code"].iloc[0]) if len(ordered) else None,
        "top1_name": _name(ordered.iloc[0]) if len(ordered) else None,
        "median_rank_of_holdings": float(buys["rank"].median()) if len(buys) else None,
        "violations": [{"stock_code": str(r.stock_code), "name": _name(r),
                        "rank": None if pd.isna(r.rank) else int(r.rank),
                        "weight": round(float(r.weight), 4),
                        "note": str(r.note)[:80]}
                       for r in violations.itertuples()],
        "violation_weight_share": round(float(violations["weight"].sum() / total_weight), 4)
        if total_weight > 0 else 0.0,
        "capped_weights": [{"stock_code": str(r.stock_code), "name": _name(r),
                            "rank": int(r.rank), "weight": round(float(r.weight), 4),
                            "capped_weight": round(float(r.capped_weight), 4)}
                           for r in capped.itertuples()],
        "status": "violations" if len(violations) else "ok",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="PK 护栏日报")
    ap.add_argument("--date", default=None, help="决策日（默认最新）")
    ap.add_argument("--rank-cap", type=int, default=20)
    ap.add_argument("--out-dir", default=str(OUT_DIR.relative_to(ROOT)))
    args = ap.parse_args()

    ledger = pd.read_parquet(LEDGER)
    ntf = ledger[ledger["source"] == "notify"].copy()
    if ntf.empty:
        print("[pk_guard] 台账里没有 notify 记录，跳过")
        return 0
    date = args.date or str(ntf["decision_date"].max())[:10]
    rows = ntf[ntf["decision_date"].astype(str).str.slice(0, 10) == date]
    if rows.empty:
        print(f"[pk_guard] {date} 没有最终名单记录")
        return 0

    payload = {"date": date, "rank_cap": args.rank_cap, "source": "notify",
               "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               **analyse(rows, args.rank_cap)}
    out = ROOT / args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{date}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[pk_guard] {date}｜持仓 {payload['n_holdings']} 只｜最大权重 {payload['max_weight']}"
          f"｜持仓 rank 中位数 {payload['median_rank_of_holdings']}｜状态 {payload['status']}")
    if payload["violations"]:
        print(f"[pk_guard] 违规 {len(payload['violations'])} 条（占权重 {payload['violation_weight_share']:.1%}）：")
        for v in payload["violations"]:
            print(f"    {v['stock_code']} {v['name']:<6} rank={v['rank']:<6} 权重 {v['weight']:.2%}")
    else:
        print("[pk_guard] 没有 rank 超限却拿到权重的票")
    # 全历史回顾：每个 notify 决策日一条，看"违规"是偶发还是常态
    hist = []
    for d, g in ntf.groupby(ntf["decision_date"].astype(str).str.slice(0, 10)):
        rec = analyse(g, args.rank_cap)
        hist.append({"date": d, "n_holdings": rec["n_holdings"], "max_weight": rec["max_weight"],
                     "median_rank_of_holdings": rec["median_rank_of_holdings"],
                     "n_violations": len(rec["violations"]),
                     "violation_weight_share": rec["violation_weight_share"], "status": rec["status"]})
    hist_df = pd.DataFrame(hist).sort_values("date")
    hist_df.to_csv(out / "history.csv", index=False)
    bad = hist_df[hist_df["status"] == "violations"]
    print(f"[pk_guard] 全历史 {len(hist_df)} 个决策日：违规 {len(bad)} 天"
          f"（占 {len(bad) / max(len(hist_df), 1):.0%}）；违规日里被塞进去的权重占比中位数 "
          f"{bad['violation_weight_share'].median() if len(bad) else 0:.1%}")
    print(f"[pk_guard] 产物 → {out}/latest.json、{out}/history.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
