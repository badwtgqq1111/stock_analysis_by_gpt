#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""B4L：长历史 53 特征面板 + 市场/板块块。

为什么叫 B4L 而不是 B4：`cn_sleeve_features_long_v4` 有 2015-2026 长历史，
但没有 Alpha101/158/GTJA 的 100 特征；它用于回答“市场/板块加到一个
长期基本面+价量面板上有没有增量”，不能替代真正的 Alpha101 长历史 B4。

特征：
  L0 = 53 个 sleeve 长面板特征
  L1 = +7 市场/beta
  L2 = +7 板块
  L3 = +24 市场+板块
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from p2_81_meta_ab import MARKET, MARKET_STATE, SECTOR  # noqa: E402
from p2_81_walk_forward import run_fold  # noqa: E402

LONG = ROOT / "assets/data/meta/cn_sleeve_features_long_v4.parquet"
META = ROOT / "assets/data/derived/cn_meta_dataset.parquet"
OUT = ROOT / "output/research/p2_81"


def main() -> int:
    from pyarrow.parquet import read_schema

    schema = read_schema(LONG)
    long_features = [c for c in schema.names if c not in
                     ("trade_date", "stock_code", "delisted_flag", "in_panel_v3", "in_domain_v4")]
    long = pd.read_parquet(LONG, columns=["trade_date", "stock_code"] + long_features)
    long["trade_date"] = pd.to_datetime(long["trade_date"]).dt.normalize()
    long = long[long["trade_date"] >= "2018-01-01"].drop_duplicates(["trade_date", "stock_code"], keep="last")
    meta = pd.read_parquet(
        META,
        columns=["trade_date", "stock_code", "fwd_excess_20"] + MARKET + MARKET_STATE + SECTOR,
    )
    meta["trade_date"] = pd.to_datetime(meta["trade_date"]).dt.normalize()
    frame = long.merge(meta, on=["trade_date", "stock_code"], how="inner")
    frame = frame.dropna(subset=["fwd_excess_20"]).copy()
    frame["label"] = frame["fwd_excess_20"].clip(-0.4, 0.4)
    print(f"[b4l] rows {len(frame):,} {frame.trade_date.min().date()} ~ {frame.trade_date.max().date()}",
          flush=True)

    sets = [
        ("L0_long53", long_features),
        ("L1_plus_market", long_features + MARKET),
        ("L2_plus_sector", long_features + SECTOR),
        ("L3_plus_both", long_features + MARKET + MARKET_STATE + SECTOR),
    ]
    folds = [
        ("F1_2023", "2021-11-30", "2022-01-01", "2022-11-30", "2023-01-01", "2023-12-31"),
        ("F2_2024", "2022-11-30", "2023-01-01", "2023-11-30", "2024-01-01", "2024-12-31"),
        ("F3_2025_26", "2023-11-30", "2024-01-01", "2024-11-30", "2025-01-01", "2026-09-15"),
    ]
    results = []
    for fold_name, tr_end, va_start, va_end, te_start, te_end in folds:
        for model_name, features in sets:
            print(f"[b4l] {fold_name} {model_name}", flush=True)
            results.append(run_fold(f"{fold_name}:{model_name}", features, frame,
                                    tr_end, va_start, va_end, te_start, te_end, 150))
    summary = {}
    for model_name, _ in sets:
        rows = [r for r in results if r["fold"].endswith(model_name)]
        summary[model_name] = {
            "folds": len(rows),
            "mean_test_rank_ic": float(np.mean([r["test"]["rank_ic"] for r in rows])),
            "mean_test_top10_excess": float(np.mean([r["test"]["top_decile_excess_mean"] for r in rows])),
            "mean_top20_net_excess_20d": float(np.mean([r["test_top20"]["mean_net_excess_20d"] for r in rows])),
            "compound_net_excess": float(np.prod([1 + r["test_top20"]["cumulative_net_excess"] for r in rows]) - 1),
        }
    payload = {"generated_at": datetime.now().isoformat(timespec="seconds"),
               "rows": int(len(frame)), "features": long_features,
               "summary": summary, "results": results}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "b4_long.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# P2.81 B4L 长历史：53 特征 + 市场/板块", "",
             f"生成 {payload['generated_at']}｜{len(frame):,} 行｜2018-2026｜"
             "基础是 sleeve 长面板，不含 Alpha101/158/GTJA", "",
             "| 模型 | 平均测试 RankIC | 平均 Top10% 超额 | 平均 Top20 净超额/20日 | 三折净超额累计 |",
             "|---|---:|---:|---:|---:|"]
    for name, row in summary.items():
        lines.append(f"| {name} | {row['mean_test_rank_ic']:+.4f} | {row['mean_test_top10_excess']*100:+.3f}% | "
                     f"{row['mean_top20_net_excess_20d']*100:+.3f}% | {row['compound_net_excess']*100:+.2f}% |")
    lines += ["", "> 基础是 53 个长面板特征，不是 Alpha101 的 100 特征；用于判断市场/板块在长历史上是否仍有增量。"]
    (OUT / "B4L_LONG.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
