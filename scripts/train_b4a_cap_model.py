#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""训练 B4a-cap 最终选股模型（100 个准入特征，LightGBM）。

训练样本：cn_b4_long_panel 2016-2026 + meta fwd_excess_20。
模型不做线上再训练；每日选股脚本只加载本模型做推理。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds
import lightgbm as lgb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

PANEL = ROOT / "assets/data/derived/cn_b4_long_panel"
META = ROOT / "assets/data/derived/cn_meta_dataset.parquet"
OUT = ROOT / "output/models/p2_81/b4a_cap"
MANIFEST = ROOT / "output/models/cn/lightgbm/alpha_zoo_hk/model_manifest.json"


def main() -> int:
    features = json.loads(MANIFEST.read_text(encoding="utf-8"))["feature_columns"]
    files = sorted(str(p) for p in PANEL.glob("year=*/*.parquet"))
    panel = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code"] + features).to_pandas()
    panel["trade_date"] = pd.to_datetime(panel["trade_date"]).dt.normalize()
    panel = panel[panel["trade_date"] >= "2016-01-01"].drop_duplicates(
        ["trade_date", "stock_code"], keep="last")
    meta = pd.read_parquet(META, columns=["trade_date", "stock_code", "fwd_excess_20"])
    meta["trade_date"] = pd.to_datetime(meta["trade_date"]).dt.normalize()
    frame = panel.merge(meta, on=["trade_date", "stock_code"], how="inner")
    frame = frame.dropna(subset=["fwd_excess_20"]).copy()
    frame["label"] = frame["fwd_excess_20"].clip(-0.4, 0.4)
    print(f"[b4a-train] rows {len(frame):,} {frame.trade_date.min().date()} ~ {frame.trade_date.max().date()}",
          flush=True)
    params = {
        "objective": "regression", "metric": "l2", "learning_rate": 0.05,
        "num_leaves": 31, "max_depth": 6, "min_data_in_leaf": 100,
        "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 1,
        "lambda_l2": 10.0, "n_jobs": -1, "verbose": -1, "seed": 42,
    }
    model = lgb.train(params, lgb.Dataset(frame[features], label=frame["label"]),
                      num_boost_round=150)
    OUT.mkdir(parents=True, exist_ok=True)
    model.save_model(str(OUT / "model.txt"))
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "rows": int(len(frame)), "start": str(frame.trade_date.min().date()),
        "end": str(frame.trade_date.max().date()),
        "features": features, "feature_count": len(features),
        "model": str((OUT / "model.txt").relative_to(ROOT)),
        "params": params,
        "label": "fwd_excess_20 clipped [-0.4,0.4]",
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[b4a-train] saved {OUT / 'model.txt'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
