#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""B4L 长历史 shadow：53 特征 + 市场/板块，加行业上限和流动性过滤。"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from p2_81_shadow import load_context, load_predictions, run_variant, META, OUT, COST  # noqa: E402


def main() -> int:
    sector, liquidity, zt = load_context()
    market = pd.read_parquet(META, columns=["trade_date", "mkt_fwd_20"]).drop_duplicates("trade_date")
    market["trade_date"] = pd.to_datetime(market["trade_date"]).dt.normalize()
    models = {name: load_predictions(name) for name in
              ("L0_long53", "L1_plus_market", "L2_plus_sector", "L3_plus_both")}
    variants = [
        ("L0_top20", "L0_long53", 20, None, 0, False),
        ("L0_top50", "L0_long53", 50, None, 0, False),
        ("L1_top20_ind2_liq20m", "L1_plus_market", 20, 2, 20_000_000, True),
        ("L2_top20_ind2_liq20m", "L2_plus_sector", 20, 2, 20_000_000, True),
        ("L3_top20_ind2_liq20m", "L3_plus_both", 20, 2, 20_000_000, True),
        ("L2_top50_ind2_liq20m", "L2_plus_sector", 50, 2, 20_000_000, True),
    ]
    results = [run_variant(name, models[model], sector, liquidity, zt, market, n, ind, liq, ex)
               for name, model, n, ind, liq, ex in variants]
    payload = {"generated_at": datetime.now().isoformat(timespec="seconds"),
               "cost_round_trip": COST, "rebalance": "every 20 trading days",
               "results": results}
    (OUT / "b4l_shadow.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# B4L 长历史 shadow（53 特征 + 市场/板块）", "",
             f"生成 {payload['generated_at']}｜20 日换仓｜双边 31bp｜研究回测", "",
             "| 组合 | 期数 | 平均持仓 | 20日净超额 | 命中率 | 累计超额 | 年化超额 | 超额回撤 | 2023 / 2024 / 2025-26 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for row in results:
        folds = row.get("by_fold_mean_excess", {})
        folds_text = " / ".join(f"{folds.get(k, float('nan'))*100:+.2f}%"
                               for k in ("F1_2023", "F2_2024", "F3_2025_26"))
        lines.append(f"| {row['name']} | {row['n_periods']} | {row['avg_holdings']:.1f} | "
                     f"{row['mean_excess_20d']*100:+.3f}% | {row['hit_rate']*100:.1f}% | "
                     f"{row['cumulative_excess']*100:+.2f}% | {row['annualized_excess']*100:+.2f}% | "
                     f"{row['max_drawdown_excess']*100:.1f}% | {folds_text} |")
    lines += ["", "> 基础是 53 个长面板特征；不是 Alpha101 的 100 特征。"]
    (OUT / "B4L_SHADOW.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
