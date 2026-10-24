#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""B4 exact shadow：长历史 100 特征 + 市场/板块，加行业上限/流动性过滤。"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from p2_81_shadow import load_context, load_predictions, run_variant, META, OUT, COST  # noqa: E402

LONG = ROOT / "assets/data/meta/cn_sleeve_features_long_v4.parquet"


def main() -> int:
    sector, liquidity, zt = load_context()
    market = pd.read_parquet(META, columns=["trade_date", "mkt_fwd_20"]).drop_duplicates("trade_date")
    market["trade_date"] = pd.to_datetime(market["trade_date"]).dt.normalize()
    models = {name: load_predictions(name) for name in
              ("B4a_current100", "B4b_plus_market", "B4c_plus_sector", "B4d_plus_both")}
    size = pd.read_parquet(LONG, columns=["trade_date", "stock_code", "mktcap"])
    size["trade_date"] = pd.to_datetime(size["trade_date"]).dt.normalize()
    size = size.drop_duplicates(["trade_date", "stock_code"], keep="last")
    models = {name: frame.merge(size, on=["trade_date", "stock_code"], how="left")
              for name, frame in models.items()}
    variants = [
        ("B4a_top20", "B4a_current100", 20, None, 0, False, 0),
        ("B4a_top50", "B4a_current100", 50, None, 0, False, 0),
        ("B4a_top20_ind2_liq20m", "B4a_current100", 20, 2, 20_000_000, True, 0),
        ("B4a_top20_ind2_liq20m_cap1b", "B4a_current100", 20, 2, 20_000_000, True, 1_000_000_000),
        ("B4a_top20_ind2_liq20m_cap2b", "B4a_current100", 20, 2, 20_000_000, True, 2_000_000_000),
        ("B4a_top20_ind2_liq20m_cap5b", "B4a_current100", 20, 2, 20_000_000, True, 5_000_000_000),
        ("B4c_top20_ind2_liq20m", "B4c_plus_sector", 20, 2, 20_000_000, True, 0),
        ("B4c_top50_ind2_liq20m", "B4c_plus_sector", 50, 2, 20_000_000, True, 0),
        ("B4d_top20_ind2_liq20m", "B4d_plus_both", 20, 2, 20_000_000, True, 0),
    ]
    results = [run_variant(name, models[model], sector, liquidity, zt, market, n, ind, liq, ex, cap)
               for name, model, n, ind, liq, ex, cap in variants]
    payload = {"generated_at": datetime.now().isoformat(timespec="seconds"),
               "cost_round_trip": COST, "rebalance": "every 20 trading days",
               "results": results}
    (OUT / "b4_exact_shadow.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# B4 exact shadow（100 特征 + 市场/板块）", "",
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
    lines += ["", "> 100 特征来自 feature store 的 100 个准入列（按年 rank）；不是完整 683 列 clean_panel。"]
    (OUT / "B4_EXACT_SHADOW.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
