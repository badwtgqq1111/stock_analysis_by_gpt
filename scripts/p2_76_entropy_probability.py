#!/usr/bin/env python3
"""P2.76 信息熵概率层：给候选池每只票一个校准过的「未来 5/10 日上涨/超额」概率。

流程：
  1. 训练样本 = 选股台账（`selection_ledger.parquet`，2025-01 起 3 万条候选）
     + 熵特征（`cn_entropy_features.parquet`）+ 市场熵 + 策略共识（n_strategies）。
  2. 标签 = 未来 5 日 / 10 日超额收益 > 0（基准中证500，台账里已算好）。
  3. 按季度 walk-forward，purge 10 个交易日；LightGBM + isotonic 校准。
  4. 对照：只用策略先验（n_strategies / rank）vs 只用熵 vs 先验+熵。
  5. 输出最新交易日的每只候选概率 + 评估报告。

用法：
  uv run python scripts/p2_76_entropy_probability.py --horizon 5
  uv run python scripts/p2_76_entropy_probability.py --horizon 5 --latest 2026-10-22
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "output/results_cn/selection_ledger.parquet"
ENTROPY = ROOT / "assets/data/derived/cn_entropy_features.parquet"
MKT_ENTROPY = ROOT / "assets/data/derived/cn_market_entropy.parquet"
OUT = ROOT / "output/research/p2_76_entropy"

ENT_COLS = ["entropy_ret_20", "entropy_ret_60", "entropy_ret_120", "entropy_perm_60",
            "entropy_vol_20", "entropy_vol_60", "entropy_ret_chg"]
MKT_COLS = ["mkt_ind_amount_entropy", "mkt_ind_ret_disp", "mkt_ret_entropy"]
PRIOR_COLS = ["prior_n", "prior_rank_pct", "prior_score_z"]


def build_dataset(horizon: int) -> pd.DataFrame:
    led = pd.read_parquet(LEDGER)
    led["decision_date"] = pd.to_datetime(led["decision_date"]).dt.normalize()
    label_col = f"excess_r{horizon}"
    ret_col = f"r{horizon}"
    led = led.dropna(subset=[label_col])
    led["y"] = (led[label_col] > 0).astype(int)
    led["fwd_excess"] = led[label_col]
    led["fwd_ret"] = led[ret_col]

    # 策略共识：同一 (date, code) 被多少条策略选中
    votes = led.groupby(["decision_date", "stock_code"]).agg(
        prior_n=("source", "nunique"), prior_rank=("rank", "min"), prior_score=("model_score", "max")).reset_index()
    led = led.merge(votes, on=["decision_date", "stock_code"], how="left", suffixes=("", "_v"))
    led = led.sort_values(["decision_date", "stock_code", "source"]).drop_duplicates(
        ["decision_date", "stock_code"], keep="first")

    ent = pd.read_parquet(ENTROPY, columns=["trade_date", "stock_code"] + ENT_COLS)
    ent = ent.rename(columns={"trade_date": "decision_date"})
    led = led.merge(ent, on=["decision_date", "stock_code"], how="left")
    mkt = pd.read_parquet(MKT_ENTROPY).rename(columns={"trade_date": "decision_date"})
    led = led.merge(mkt, on="decision_date", how="left")

    led["prior_score_z"] = led.groupby("decision_date")["prior_score"].transform(
        lambda s: (s - s.mean()) / (s.std(ddof=0) + 1e-9))
    led["prior_rank_pct"] = led.groupby("decision_date")["prior_rank"].rank(pct=True)
    return led


def walk_forward(df: pd.DataFrame, feat_cols: list[str], horizon: int, purge_days: int = 10) -> pd.DataFrame:
    import lightgbm as lgb
    from sklearn.isotonic import IsotonicRegression

    dates = np.sort(df["decision_date"].unique())
    quarters = pd.Series(pd.to_datetime(dates)).dt.to_period("Q").unique()
    rows = []
    for q in quarters[1:]:
        test_mask = df["decision_date"].dt.to_period("Q") == q
        test_dates = df.loc[test_mask, "decision_date"].unique()
        if len(test_dates) < 3:
            continue
        cutoff = pd.Timestamp(test_dates.min()) - pd.Timedelta(days=purge_days * 1.5)
        train = df[df["decision_date"] < cutoff]
        test = df[test_mask]
        if len(train) < 800 or test["y"].nunique() < 2:
            continue
        # 时间切分做校准集（最后 20% 训练日期）
        train_dates = np.sort(train["decision_date"].unique())
        cal_cut = train_dates[int(len(train_dates) * 0.8)]
        fit = train[train["decision_date"] < cal_cut]
        cal = train[train["decision_date"] >= cal_cut]
        model = lgb.LGBMClassifier(n_estimators=300, learning_rate=0.03, num_leaves=31,
                                   min_child_samples=60, subsample=0.8, colsample_bytree=0.8,
                                   reg_lambda=1.0, random_state=7, verbose=-1)
        model.fit(fit[feat_cols], fit["y"])
        raw_cal = model.predict_proba(cal[feat_cols])[:, 1] if len(cal) else np.array([])
        iso = None
        if len(cal) > 200 and cal["y"].nunique() > 1:
            iso = IsotonicRegression(out_of_bounds="clip").fit(raw_cal, cal["y"])
        prob = model.predict_proba(test[feat_cols])[:, 1]
        if iso is not None:
            prob = iso.predict(prob)
        rows.append(pd.DataFrame({
            "decision_date": test["decision_date"].values,
            "stock_code": test["stock_code"].values,
            "y": test["y"].values,
            "fwd_excess": test["fwd_excess"].values,
            "prob": prob,
            "model_score": test["model_score"].values,
            "prior_n": test["prior_n"].values,
        }))
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def evaluate(pred: pd.DataFrame) -> dict:
    from sklearn.metrics import roc_auc_score, brier_score_loss, log_loss
    y = pred["y"].to_numpy()
    p = pred["prob"].to_numpy()
    out = {"n": int(len(pred)), "dates": int(pred["decision_date"].nunique()),
           "base_rate": round(float(y.mean()), 4),
           "auc": round(float(roc_auc_score(y, p)), 4) if len(np.unique(y)) > 1 else None,
           "brier": round(float(brier_score_loss(y, p)), 4),
           "logloss": round(float(log_loss(y, np.clip(p, 1e-6, 1 - 1e-6))), 4)}
    q = pred.copy()
    q["decile"] = q.groupby("decision_date")["prob"].transform(lambda s: pd.qcut(s.rank(method="first"), 10, labels=False, duplicates="drop"))
    top = q[q["decile"] == 9]
    out["top_decile_mean_excess"] = round(float(top["fwd_excess"].mean()) * 100, 3)
    out["pool_mean_excess"] = round(float(q["fwd_excess"].mean()) * 100, 3)
    out["top_decile_hit"] = round(float(top["y"].mean()), 3)
    out["lift"] = round(out["top_decile_hit"] / max(out["base_rate"], 1e-6), 3)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=5, choices=[5, 10])
    ap.add_argument("--latest", default="")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    df = build_dataset(args.horizon)
    print(f"[P2.76] 样本 {len(df):,} 行 / {df.decision_date.nunique()} 天 / 正例率 {df.y.mean():.3f}")

    sets = {
        "prior_only": PRIOR_COLS,
        "entropy_only": ENT_COLS + MKT_COLS,
        "prior_entropy": PRIOR_COLS + ENT_COLS + MKT_COLS,
    }
    results = {}
    for name, cols in sets.items():
        cols = [c for c in cols if c in df.columns]
        pred = walk_forward(df, cols, args.horizon)
        if pred.empty:
            results[name] = {"error": "no folds"}
            continue
        results[name] = evaluate(pred)
        pred.to_parquet(OUT / f"pred_{name}_h{args.horizon}.parquet", index=False)
        print(name, json.dumps(results[name], ensure_ascii=False))

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"report_h{args.horizon}.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    # 最新一天：用全样本训练，输出候选概率
    latest = args.latest or str(df["decision_date"].max().date())
    latest_ts = pd.Timestamp(latest)
    cols = [c for c in PRIOR_COLS + ENT_COLS + MKT_COLS if c in df.columns]
    import lightgbm as lgb
    train = df[df["decision_date"] < latest_ts]
    today = df[df["decision_date"] == latest_ts]
    if len(today) and len(train) >= 800:
        model = lgb.LGBMClassifier(n_estimators=400, learning_rate=0.03, num_leaves=31,
                                   min_child_samples=60, subsample=0.8, colsample_bytree=0.8,
                                   reg_lambda=1.0, random_state=7, verbose=-1)
        model.fit(train[cols], train["y"])
        today = today.copy()
        today["p_up"] = model.predict_proba(today[cols])[:, 1]
        keep = ["decision_date", "stock_code", "name", "industry", "source", "prob_source" if False else "model_score",
                "prior_n", "p_up"] + [c for c in ("entropy_ret_20", "entropy_ret_chg", "mkt_ret_entropy") if c in today.columns]
        out = today.sort_values("p_up", ascending=False)[keep]
        out.to_csv(OUT / f"latest_h{args.horizon}_{latest}.csv", index=False)
        print(f"[P2.76] 最新 {latest} 候选 {len(out)} 只 → {OUT}/latest_h{args.horizon}_{latest}.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
