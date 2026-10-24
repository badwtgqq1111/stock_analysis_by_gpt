#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""B4 短样本：生产模型 100 个准入特征 + 市场/板块块。

为什么是短样本：clean_feature_panel 只覆盖 2025-04-09 起，B4 无法做
2016-2026 长历史。这里只跑一个**描述性** A/B：
  B4a = 100 个现有准入特征
  B4b = +7 个市场/beta 特征
  B4c = +7 个板块特征
  B4d = +24 个市场+板块特征

训练/验证/测试按时间切分，跨段 purge 20 个交易日；结果不能当正式回测。
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
import lightgbm as lgb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from p2_81_meta_ab import MARKET, MARKET_STATE, SECTOR, metrics  # noqa: E402

PANEL = ROOT / "assets/data/feature/clean_feature_panel"
META = ROOT / "assets/data/derived/cn_meta_dataset.parquet"
MANIFEST = ROOT / "output/models/cn/lightgbm/alpha_zoo_hk/model_manifest.json"
OUT = ROOT / "output/research/p2_81"


def load_panel() -> pd.DataFrame:
    files = []
    for exchange in ("SSE", "SZSE"):
        files.extend(str(p) for p in PANEL.glob(
            f"market=CN/exchange={exchange}/asset_type=equity/frequency=daily/adjust=qfq/"
            f"feature_set=alpha_zoo_hk/year=*/*.parquet"))
    features = json.loads(MANIFEST.read_text(encoding="utf-8"))["feature_columns"]
    table = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code"] + features).to_pandas()
    table["trade_date"] = pd.to_datetime(table["trade_date"]).dt.normalize()
    table = table[table["trade_date"] >= "2025-04-01"]
    table = table.drop_duplicates(["trade_date", "stock_code"], keep="last")
    return table


def main() -> int:
    print("[b4] load clean panel", flush=True)
    panel = load_panel()
    print(f"[b4] panel {len(panel):,} rows / {panel.stock_code.nunique()} stocks", flush=True)
    market_cols = ["trade_date", "stock_code", "fwd_excess_20", "mkt_fwd_20"] + MARKET + MARKET_STATE + SECTOR
    meta = pd.read_parquet(META, columns=market_cols)
    meta["trade_date"] = pd.to_datetime(meta["trade_date"]).dt.normalize()
    frame = panel.merge(meta, on=["trade_date", "stock_code"], how="inner")
    frame = frame.dropna(subset=["fwd_excess_20"]).copy()
    frame["label"] = frame["fwd_excess_20"].clip(-0.4, 0.4)
    print(f"[b4] joined {len(frame):,} rows {frame.trade_date.min().date()} ~ {frame.trade_date.max().date()}",
          flush=True)

    panel_features = json.loads(MANIFEST.read_text(encoding="utf-8"))["feature_columns"]
    feature_sets = [
        ("B4a_current100", panel_features),
        ("B4b_plus_market", panel_features + MARKET),
        ("B4c_plus_sector", panel_features + SECTOR),
        ("B4d_plus_both", panel_features + MARKET + MARKET_STATE + SECTOR),
    ]
    train = frame[frame["trade_date"] <= "2025-11-30"]
    valid = frame[(frame["trade_date"] >= "2026-01-01") & (frame["trade_date"] <= "2026-02-28")]
    test = frame[(frame["trade_date"] >= "2026-04-01") & (frame["trade_date"] <= "2026-09-15")]
    print(f"[b4] train {len(train):,} valid {len(valid):,} test {len(test):,}", flush=True)
    params = {
        "objective": "regression", "metric": "l2", "learning_rate": 0.05,
        "num_leaves": 31, "max_depth": 6, "min_data_in_leaf": 100,
        "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 1,
        "lambda_l2": 10.0, "n_jobs": -1, "verbose": -1, "seed": 42,
    }
    results = []
    for name, features in feature_sets:
        cols = [c for c in features if c in frame.columns]
        print(f"[b4] {name}: {len(cols)} features", flush=True)
        model = lgb.train(params, lgb.Dataset(train[cols], label=train["label"]), num_boost_round=150)
        payload = {"name": name, "n_features": len(cols)}
        for split, part in (("valid", valid), ("test", test)):
            pred = model.predict(part[cols])
            payload[split] = metrics(pred, part["fwd_excess_20"].to_numpy(),
                                     part["trade_date"].dt.strftime("%Y-%m-%d").to_numpy())
        results.append(payload)

    OUT.mkdir(parents=True, exist_ok=True)
    output = {"generated_at": datetime.now().isoformat(timespec="seconds"),
              "rows": int(len(frame)), "train": int(len(train)),
              "valid": int(len(valid)), "test": int(len(test)),
              "split": "train<=2025-11-30; valid 2026-01~02; test 2026-04~09; 20-day purge",
              "warning": "clean panel only starts 2025-04; B4 is short-sample descriptive only",
              "results": results}
    (OUT / "b4_short.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# P2.81 B4 短样本：现有 100 特征 + 市场/板块", "",
             f"生成 {output['generated_at']}｜joined {len(frame):,} 行｜"
             f"train {len(train):,} / valid {len(valid):,} / test {len(test):,}", "",
             "> clean_feature_panel 只从 2025-04 起，B4 是短样本描述性检验，不是正式回测。", "",
             "| 模型 | 特征数 | 验证 RankIC | 验证 NW t | 测试 RankIC | 测试 NW t | 测试 Top10% 超额 |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for row in results:
        v, t = row["valid"], row["test"]
        lines.append(f"| {row['name']} | {row['n_features']} | {v['rank_ic']:+.4f} | {v['rank_ic_nw_t']:+.2f} | "
                     f"{t['rank_ic']:+.4f} | {t['rank_ic_nw_t']:+.2f} | {t['top_decile_excess_mean']*100:+.3f}% |")
    (OUT / "B4_SHORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
