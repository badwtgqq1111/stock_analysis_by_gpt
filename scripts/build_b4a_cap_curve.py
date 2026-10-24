#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 B4a-cap shadow 的 20 日非重叠收益展开成日频净值（策略池用）。

注意：这是把每个 20 日持有期收益按几何平均摊到 20 个交易日，用于统一
年化/回撤口径；不是逐日真实持仓收益。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SHADOW = ROOT / "output/research/p2_81/b4_exact_shadow.json"
BENCH = ROOT / "assets/data/meta/cn_benchmark_daily.parquet"
OUT = ROOT / "output/research/p2_81/b4a_cap/curve"
VARIANT = "B4a_top20_ind2_liq20m_cap1b"


def main() -> int:
    payload = json.loads(SHADOW.read_text(encoding="utf-8"))
    row = next(r for r in payload["results"] if r["name"] == VARIANT)
    periods = pd.DataFrame(row["periods"])
    periods["trade_date"] = pd.to_datetime(periods["trade_date"])
    periods = periods.sort_values("trade_date").reset_index(drop=True)
    bench = pd.read_parquet(BENCH)
    bench = bench[bench["ts_code"] == "000905.SH"][["trade_date", "close"]].copy()
    bench["trade_date"] = pd.to_datetime(bench["trade_date"]).dt.normalize()
    bench = bench.sort_values("trade_date").set_index("trade_date")["close"]

    dates = list(bench.index)
    first = periods["trade_date"].min()
    last = periods["trade_date"].max()
    calendar = [d for d in dates if d >= first]
    nav_abs, nav_bench, n_picks = [], [], []
    nav = 1.0
    for i, period in periods.iterrows():
        start = period["trade_date"]
        end = periods["trade_date"].iloc[i + 1] if i + 1 < len(periods) else None
        window = [d for d in calendar if d >= start and (end is None or d < end)]
        if not window:
            continue
        days = max(len(window), 1)
        daily_abs = (1 + period["absolute"]) ** (1 / days) - 1
        for d in window:
            nav *= 1 + daily_abs
            nav_abs.append(nav)
            nav_bench.append(float(bench.loc[d] / bench.loc[calendar[0]]))
            n_picks.append(int(period["n"]))
    curve = pd.DataFrame({"asof_date": calendar[:len(nav_abs)], "nav": nav_abs,
                          "csi500": nav_bench, "n_picks": n_picks})
    OUT.mkdir(parents=True, exist_ok=True)
    curve.to_csv(OUT / "nav_engine.csv", index=False)
    x = np.r_[1.0, curve["nav"].to_numpy()]
    rets = x[1:] / x[:-1] - 1
    years = len(rets) / 252
    peak = np.maximum.accumulate(x)
    metrics = {
        "total_return": float(x[-1] - 1),
        "annual_return": float(x[-1] ** (1 / years) - 1) if years > 0 else None,
        "max_drawdown": float((x / peak - 1).min()),
        "sharpe_rf0": float(rets.mean() / rets.std(ddof=1) * np.sqrt(252)) if rets.std(ddof=1) > 0 else None,
        "n_days": int(len(rets)),
    }
    out = {
        "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "variant": VARIANT,
        "kind": "strategy_rule_net",
        "metrics": metrics,
        "benchmark": {"code": "000905.SH", "name": "中证500",
                      "total_return": float(curve["csi500"].iloc[-1] - 1)},
        "methodology": "20 日非重叠 Top20 等权，行业≤2、日额≥2000万、市值≥10亿、双边31bp；"
                       "20 日收益按几何平均摊到 20 个交易日生成日频净值（研究近似）。",
        "evidence": "output/research/p2_81/B4_EXACT_SHADOW.md",
    }
    (OUT / "curve.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"rows": len(curve), **metrics, "benchmark": out["benchmark"]["total_return"]},
                     ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
