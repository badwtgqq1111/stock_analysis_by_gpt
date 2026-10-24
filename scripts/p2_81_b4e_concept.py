#!/usr/bin/env python3
"""B4e：B4a 的 100 特征 + 概念因子，3 折 walk-forward 对照。

对照：
  B4a_current100   100 个准入特征
  B4e_plus_concept 100 + 6 个概念因子（heat/rise/breadth/ret5/rank/n_concepts）

折与 purge 与 p2_81_b4_exact.py 完全一致（F1 2023 / F2 2024 / F3 2025-26）。
注意：概念成员是当前快照，存在幸存者/前视偏差。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from p2_81_walk_forward import run_fold  # noqa: E402

PANEL = ROOT / "assets/data/derived/cn_b4_long_panel"
META = ROOT / "assets/data/derived/cn_meta_dataset.parquet"
MANIFEST = ROOT / "output/models/cn/lightgbm/alpha_zoo_hk/model_manifest.json"
OUT = ROOT / "output/research/p2_81"
CONCEPT = ["concept_heat", "concept_rise", "concept_breadth", "concept_ret5", "concept_rank", "n_concepts"]


def load_panel(start: str) -> pd.DataFrame:
    files = sorted(str(p) for p in PANEL.glob("year=*/*.parquet"))
    features = json.loads(MANIFEST.read_text(encoding="utf-8"))["feature_columns"]
    table = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code"] + features).to_pandas()
    table["trade_date"] = pd.to_datetime(table["trade_date"]).dt.normalize()
    table = table[table["trade_date"] >= pd.Timestamp(start)]
    return table.drop_duplicates(["trade_date", "stock_code"], keep="last")


def main() -> int:
    print("[b4e] load panel", flush=True)
    panel = load_panel("2016-01-01")
    print(f"[b4e] panel {len(panel):,}", flush=True)
    meta = pd.read_parquet(META, columns=["trade_date", "stock_code", "fwd_excess_20"] + CONCEPT)
    meta["trade_date"] = pd.to_datetime(meta["trade_date"]).dt.normalize()
    frame = panel.merge(meta, on=["trade_date", "stock_code"], how="inner").dropna(subset=["fwd_excess_20"])
    frame["label"] = frame["fwd_excess_20"].clip(-0.4, 0.4)
    print(f"[b4e] joined {len(frame):,} rows, concept coverage {frame['concept_heat'].notna().mean():.1%}", flush=True)

    panel_features = json.loads(MANIFEST.read_text(encoding="utf-8"))["feature_columns"]
    sets = [("B4a_current100", panel_features), ("B4e_plus_concept", panel_features + CONCEPT)]
    folds = [
        ("F1_2023", "2021-11-30", "2022-01-01", "2022-11-30", "2023-01-01", "2023-12-31"),
        ("F2_2024", "2022-11-30", "2023-01-01", "2023-11-30", "2024-01-01", "2024-12-31"),
        ("F3_2025_26", "2023-11-30", "2024-01-01", "2024-11-30", "2025-01-01", "2026-09-15"),
    ]
    results = []
    for fold_name, tr_end, va_start, va_end, te_start, te_end in folds:
        for model_name, features in sets:
            print(f"[b4e] {fold_name} {model_name}", flush=True)
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
    payload = {"generated_at": datetime.now().isoformat(timespec="seconds"), "rows": int(len(frame)),
               "summary": summary, "results": results,
               "warning": "概念成员为当前快照；3-fold walk-forward；执行仍为 20 日标签模拟"}
    (OUT / "b4e_concept.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# B4e：100 特征 + 概念因子（2026-10-23）", "", "| 模型 | 平均测试 RankIC | Top10% 超额 | Top20 净超额/20日 | 三折累计 |", "|---|---:|---:|---:|---:|"]
    for name, row in summary.items():
        lines.append(f"| {name} | {row['mean_test_rank_ic']:+.4f} | {row['mean_test_top10_excess']*100:+.3f}% | "
                     f"{row['mean_top20_net_excess_20d']*100:+.3f}% | {row['compound_net_excess']*100:+.2f}% |")
    (OUT / "B4E_CONCEPT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
