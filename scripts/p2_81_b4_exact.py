#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""B4 exact：长历史 clean_feature_panel 的 100 个准入特征 + 市场/板块。

需要先完成：
  python scripts/run_cn_pipeline.py --config config/cn_pipeline_p2_81_long.toml --stage features
  python scripts/build_b4_long_panel.py

3 折 rolling walk-forward：F1 2023 / F2 2024 / F3 2025-26，purge 20 日。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from p2_81_meta_ab import MARKET, MARKET_STATE, SECTOR  # noqa: E402
from p2_81_walk_forward import run_fold  # noqa: E402

PANEL = ROOT / "assets/data/derived/cn_b4_long_panel"
META = ROOT / "assets/data/derived/cn_meta_dataset.parquet"
MANIFEST = ROOT / "output/models/cn/lightgbm/alpha_zoo_hk/model_manifest.json"
OUT = ROOT / "output/research/p2_81"


def load_panel(start: str) -> pd.DataFrame:
    files = sorted(str(p) for p in PANEL.glob("year=*/*.parquet"))
    if not files:
        raise SystemExit("缺 cn_b4_long_panel；先跑 scripts/build_b4_long_panel.py")
    features = json.loads(MANIFEST.read_text(encoding="utf-8"))["feature_columns"]
    table = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code"] + features).to_pandas()
    table["trade_date"] = pd.to_datetime(table["trade_date"]).dt.normalize()
    table = table[table["trade_date"] >= pd.Timestamp(start)]
    table = table.drop_duplicates(["trade_date", "stock_code"], keep="last")
    return table


def main() -> int:
    print("[b4-exact] load panel", flush=True)
    panel = load_panel("2016-01-01")
    print(f"[b4-exact] panel {len(panel):,} rows / {panel.stock_code.nunique()} stocks", flush=True)
    meta_cols = ["trade_date", "stock_code", "fwd_excess_20"] + MARKET + MARKET_STATE + SECTOR
    meta = pd.read_parquet(META, columns=meta_cols)
    meta["trade_date"] = pd.to_datetime(meta["trade_date"]).dt.normalize()
    frame = panel.merge(meta, on=["trade_date", "stock_code"], how="inner")
    frame = frame.dropna(subset=["fwd_excess_20"]).copy()
    frame["label"] = frame["fwd_excess_20"].clip(-0.4, 0.4)
    print(f"[b4-exact] joined {len(frame):,} rows {frame.trade_date.min().date()} ~ {frame.trade_date.max().date()}",
          flush=True)

    panel_features = json.loads(MANIFEST.read_text(encoding="utf-8"))["feature_columns"]
    sets = [
        ("B4a_current100", panel_features),
        ("B4b_plus_market", panel_features + MARKET),
        ("B4c_plus_sector", panel_features + SECTOR),
        ("B4d_plus_both", panel_features + MARKET + MARKET_STATE + SECTOR),
    ]
    folds = [
        ("F1_2023", "2021-11-30", "2022-01-01", "2022-11-30", "2023-01-01", "2023-12-31"),
        ("F2_2024", "2022-11-30", "2023-01-01", "2023-11-30", "2024-01-01", "2024-12-31"),
        ("F3_2025_26", "2023-11-30", "2024-01-01", "2024-11-30", "2025-01-01", "2026-09-15"),
    ]
    results = []
    for fold_name, tr_end, va_start, va_end, te_start, te_end in folds:
        for model_name, features in sets:
            print(f"[b4-exact] {fold_name} {model_name}", flush=True)
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
               "rows": int(len(frame)), "summary": summary, "results": results,
               "warning": "3-fold walk-forward; macro/execution constraints still approximate"}
    (OUT / "b4_exact.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# P2.81 B4 exact：长历史 100 特征 + 市场/板块", "",
             f"生成 {payload['generated_at']}｜{len(frame):,} 行｜"
             f"2018-2026｜3 折 walk-forward｜purge 20 日", "",
             "| 模型 | 平均测试 RankIC | 平均 Top10% 超额 | 平均 Top20 净超额/20日 | 三折净超额累计 |",
             "|---|---:|---:|---:|---:|"]
    for name, row in summary.items():
        lines.append(f"| {name} | {row['mean_test_rank_ic']:+.4f} | {row['mean_test_top10_excess']*100:+.3f}% | "
                     f"{row['mean_top20_net_excess_20d']*100:+.3f}% | {row['compound_net_excess']*100:+.2f}% |")
    lines += ["", "> 这是正式 A/B 的数据层版本，但执行仍用 20 日标签模拟；涨跌停/停牌/整手需在执行 shadow 里再加。"]
    (OUT / "B4_EXACT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
