#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把一年窗口的 5min 派生特征并进面板 v3 → v3i（供 LLM 挖掘使用）。

注意覆盖范围：分时特征只覆盖 **2025-10-09~2026-09-30（241 个交易日）**，
而面板从 2015 起 —— 所以合并后新增列的覆盖率 ~1.3%（全年份里只有一年有值）。
因此挖掘/评估都必须显式用 `--start 2025-10-01` 跑"一年窗口"，否则这两类字段的样本会严重错配。

用法:
  uv run python scripts/panel_merge_intraday.py
输出:
  assets/data/meta/cn_sleeve_features_long_v3i.parquet + .manifest.json
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
META = ROOT / "assets/data/meta"
V3 = META / "cn_sleeve_features_long_v3.parquet"
INTRA = META / "cn_intraday_daily_features.parquet"
OUT = META / "cn_sleeve_features_long_v3i.parquet"
INTRA_COLS = ["intraday_mom", "overnight_ret", "head_30m", "tail_30m", "intraday_vol",
              "vwap_dev", "range_pct", "tail_vol_share", "am_pm_gap"]


def main() -> int:
    panel = pd.read_parquet(V3)
    panel["trade_date"] = pd.to_datetime(panel["trade_date"])
    intra = pd.read_parquet(INTRA)
    intra["trade_date"] = pd.to_datetime(intra["trade_date"])
    intra = intra[["trade_date", "stock_code"] + INTRA_COLS]
    merged = panel.merge(intra, on=["trade_date", "stock_code"], how="left")
    sub = merged[merged["trade_date"] >= pd.Timestamp("2025-10-01")]
    coverage = {c: round(float(sub[c].notna().mean()), 4) for c in INTRA_COLS}
    print(f"[v3i] {len(merged):,} 行 / {merged.shape[1]} 列；一年窗口 {len(sub):,} 行，"
          f"分时列覆盖 {coverage}", flush=True)
    merged.to_parquet(OUT, index=False)
    OUT.with_suffix(".manifest.json").write_text(json.dumps({
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rows": int(len(merged)), "fields": int(merged.shape[1]),
        "added_intraday_cols": INTRA_COLS,
        "intraday_coverage_in_window": coverage,
        "window": "2025-10-09~2026-09-30",
        "note": "分时列只在一年窗口内有值；用小窗口挖掘/评估时必须 --start 2025-10-01",
        "sources": {"v3": str(V3), "intraday": str(INTRA)},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[v3i] 写出 {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
