#!/usr/bin/env python3
"""P2.76 探针：熵特征在全市场截面上的增量（控制波动/动量/规模/换手后）。

为什么不用候选台账训练：台账每天中位数只有 5 只票，做不了截面排序（AUC≈0.5）。
这里在全市场样本上检验「熵是否提供控制变量之外的增量」，验证通过后再用于候选池打分。

用法：uv run python scripts/p2_76_entropy_probe.py --start 2019-01-01 --step 3
"""
from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output/research/p2_76_entropy"
ENT = ROOT / "assets/data/derived/cn_entropy_features.parquet"
MKT = ROOT / "assets/data/derived/cn_market_entropy.parquet"

ENT_COLS = ["entropy_ret_20", "entropy_ret_60", "entropy_ret_120", "entropy_perm_60",
            "entropy_vol_20", "entropy_vol_60", "entropy_ret_chg"]
CTRL = ["vol_20", "mom_20", "log_amount", "turnover_20"]
MKT_COLS = ["mkt_ind_amount_entropy", "mkt_ind_ret_disp", "mkt_ret_entropy"]


def build(start: str, step: int) -> pd.DataFrame:
    files = sorted(glob.glob(str(ROOT / "assets/data/clean/ohlcv/market=CN/exchange=*/"
                                    "asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet")))
    px = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "close", "amount", "turnover"]).to_pandas()
    px["trade_date"] = pd.to_datetime(px["trade_date"]).dt.normalize()
    px = px.drop_duplicates(["trade_date", "stock_code"], keep="last").sort_values(["stock_code", "trade_date"])
    px = px[px["trade_date"] >= pd.Timestamp(start)]
    g = px.groupby("stock_code", sort=False)
    px["fwd5"] = g["close"].shift(-5) / px["close"] - 1
    px["vol_20"] = g["close"].transform(lambda s: s.pct_change().rolling(20).std())
    px["mom_20"] = g["close"].transform(lambda s: s / s.shift(20) - 1)
    px["log_amount"] = np.log1p(px["amount"])
    px["turnover_20"] = g["turnover"].transform(lambda s: s.rolling(20).mean())

    bench = pd.read_parquet(ROOT / "assets/data/meta/cn_benchmark_daily.parquet", columns=["ts_code", "trade_date", "close"])
    bench = bench[bench["ts_code"] == "000905.SH"].copy()
    bench["trade_date"] = pd.to_datetime(bench["trade_date"])
    bs = bench.set_index("trade_date")["close"].sort_index()
    bfwd = (bs.shift(-5) / bs - 1)

    ent = pd.read_parquet(ENT, columns=["trade_date", "stock_code"] + ENT_COLS)
    mkt = pd.read_parquet(MKT).rename(columns={"trade_date": "trade_date"})
    df = px.merge(ent, on=["trade_date", "stock_code"], how="inner").merge(mkt, on="trade_date", how="left")
    df["fwd_excess"] = df["fwd5"] - df["trade_date"].map(bfwd)
    df = df.dropna(subset=["fwd_excess"])
    dates = np.sort(df["trade_date"].unique())[::step]
    df = df[df["trade_date"].isin(dates)]
    return df.dropna(subset=["vol_20", "mom_20", "log_amount", "turnover_20"])


def auc(y, p):
    from sklearn.metrics import roc_auc_score
    return float(roc_auc_score(y, p)) if len(np.unique(y)) > 1 else np.nan


def run(df: pd.DataFrame, cols: list[str], train_end: str) -> dict:
    import lightgbm as lgb
    tr = df[df["trade_date"] < pd.Timestamp(train_end)]
    te = df[df["trade_date"] >= pd.Timestamp(train_end)]
    model = lgb.LGBMClassifier(n_estimators=400, learning_rate=0.03, num_leaves=63,
                               min_child_samples=100, subsample=0.8, colsample_bytree=0.8,
                               reg_lambda=1.0, random_state=7, verbose=-1)
    model.fit(tr[cols], (tr["fwd_excess"] > 0).astype(int))
    p = model.predict_proba(te[cols])[:, 1]
    y = (te["fwd_excess"] > 0).astype(int).to_numpy()
    q = te.assign(p=p, y=y).copy()
    q["decile"] = q.groupby("trade_date")["p"].transform(lambda s: pd.qcut(s.rank(method="first"), 10, labels=False, duplicates="drop"))
    top = q[q["decile"] == 9]
    return {"auc": round(auc(y, p), 4), "n_test": int(len(te)),
            "base": round(float(y.mean()), 4),
            "top_decile_hit": round(float(top["y"].mean()), 4),
            "top_decile_excess_bp": round(float(top["fwd_excess"].mean()) * 1e4, 1),
            "pool_excess_bp": round(float(q["fwd_excess"].mean()) * 1e4, 1),
            "importance": dict(zip(cols, np.round(model.feature_importances_, 1).tolist()))}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--step", type=int, default=3)
    ap.add_argument("--train-end", default="2025-01-01")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    df = build(args.start, args.step)
    print(f"[probe] 样本 {len(df):,} / {df.trade_date.nunique()} 天 / 正例率 {(df.fwd_excess>0).mean():.3f}")
    res = {}
    for name, cols in {"controls": CTRL,
                       "controls_stock_entropy": CTRL + ENT_COLS,
                       "controls_entropy": CTRL + ENT_COLS + MKT_COLS,
                       "entropy_only": ENT_COLS + MKT_COLS}.items():
        res[name] = run(df, cols, args.train_end)
        print(name, json.dumps({k: v for k, v in res[name].items() if k != "importance"}, ensure_ascii=False))
    (OUT / "probe.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    imp = res["controls_stock_entropy"]["importance"]
    print("top features:", sorted(imp.items(), key=lambda x: -x[1])[:10])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
