#!/usr/bin/env python3
"""概念因子的有效性检验：单因子 RankIC + 控制变量下的增量 AUC。

结论（2026-10-23）：
  * 单因子 IC ≈ 0（−0.02~+0.015），不能单独当 alpha；
  * 与 波动/动量/成交额/换手 一起进 LightGBM，AUC 0.5301 → 0.5465（+0.016），有增量。
"""
from __future__ import annotations

import glob
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
CF = ROOT / "assets/data/derived/cn_concept_factors.parquet"
OUT = ROOT / "output/research/p2_concept_v2/factor_eval.json"
CTRL = ["vol20", "mom20", "logamt", "turn20"]
CF_COLS = ["concept_heat", "concept_rise", "concept_breadth", "concept_ret5", "concept_rank", "n_concepts"]


def main() -> int:
    cf = pd.read_parquet(CF)
    files = sorted(glob.glob(str(ROOT / "assets/data/clean/ohlcv/market=CN/exchange=*/"
                                    "asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet")))
    px = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "close", "amount", "turnover"]).to_pandas()
    px["trade_date"] = pd.to_datetime(px["trade_date"])
    px = px[px["trade_date"] >= "2024-01-01"].drop_duplicates(["trade_date", "stock_code"], keep="last")
    px = px.sort_values(["stock_code", "trade_date"])
    g = px.groupby("stock_code", sort=False)
    px["fwd5"] = g["close"].shift(-5) / px["close"] - 1
    px["vol20"] = g["close"].transform(lambda s: s.pct_change().rolling(20).std())
    px["mom20"] = g["close"].transform(lambda s: s / s.shift(20) - 1)
    px["logamt"] = np.log1p(px["amount"])
    px["turn20"] = g["turnover"].transform(lambda s: s.rolling(20).mean())

    b = pd.read_parquet(ROOT / "assets/data/meta/cn_benchmark_daily.parquet", columns=["ts_code", "trade_date", "close"])
    b = b[b.ts_code == "000905.SH"].copy(); b["trade_date"] = pd.to_datetime(b["trade_date"])
    bs = b.set_index("trade_date")["close"]
    m = px.merge(cf, on=["trade_date", "stock_code"], how="inner")
    m["ex"] = m["fwd5"] - m["trade_date"].map(bs.shift(-5) / bs - 1)
    m = m.dropna(subset=["ex", "vol20", "mom20", "logamt", "turn20"])

    dates = np.sort(m["trade_date"].unique())[::3]
    m = m[m["trade_date"].isin(dates)]

    ic = {}
    for c in CF_COLS:
        d = m.dropna(subset=[c])
        vals = d.groupby("trade_date").apply(lambda x: x[c].corr(x["ex"], method="spearman"), include_groups=False).dropna()
        ic[c] = round(float(vals.mean()), 4)

    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score
    tr = m[m["trade_date"] < "2026-01-01"]; te = m[m["trade_date"] >= "2026-01-01"]
    res = {}
    for name, cols in (("controls", CTRL), ("controls_concept", CTRL + CF_COLS), ("concept", CF_COLS)):
        model = lgb.LGBMClassifier(n_estimators=300, learning_rate=0.05, num_leaves=63,
                                   min_child_samples=100, subsample=0.8, colsample_bytree=0.8,
                                   verbose=-1, random_state=7)
        model.fit(tr[cols], (tr["ex"] > 0).astype(int))
        p = model.predict_proba(te[cols])[:, 1]
        res[name] = {"auc": round(float(roc_auc_score((te["ex"] > 0).astype(int), p)), 4), "n": int(len(te))}
        if name == "controls_concept":
            res[name]["importance"] = dict(zip(cols, np.round(model.feature_importances_, 1).tolist()))

    payload = {"sample": int(len(m)), "days": int(m["trade_date"].nunique()),
               "rank_ic": ic, "auc": res}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"rank_ic": ic, "auc": {k: v["auc"] for k, v in res.items()}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
