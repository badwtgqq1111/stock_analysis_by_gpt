#!/usr/bin/env python
"""D：换标签 —— 用"头部相关"的标签替代"全截面分位/原始收益"，看头部是否变好。

同一划分（train = 最后 60 个交易日之前、valid = 最后 60 个交易日）、同一套准入 90 列、同一套超参：
  reg_return      回归到原始 20 日收益（= 生产现状）
  cls_topdecile   二分类：当日收益是否进前 10%（用 predict_proba 当分数）
  reg_headrank    回归到"头部权重"目标 max(0, rank_pct − 0.9)，即只有头部有梯度
  reg_pathscore   回归到仓库自带的路径分数标签（label_mode=path_score_20d，若可得）

指标：验证窗口的秩 IC、Top-5/10/20 超额（百分点）、vs 随机（×200 配对）、
      真·前 10% 命中率（选中的名字里有多少真的在前 10%）。
输出：output/research/head_label_ab/{metrics.csv, deciles.csv, REPORT.md}
"""
from __future__ import annotations

import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from data.ingest.service import MarketDataService  # noqa: E402
from factor_engine.ml.preprocessing import preprocess_features_by_date  # noqa: E402

OUT = ROOT / "output/research/head_label_ab"
PROTOCOL = ROOT / "output/research/factor_ic_protocol"
KS = (5, 10, 20)
N_RANDOM = 200


def evaluate(frame: pd.DataFrame, score: np.ndarray, label: str, name: str, rng) -> tuple[dict, pd.Series]:
    work = pd.DataFrame({"trade_date": frame["trade_date"].to_numpy(), "score": score, "label": frame[label].to_numpy()}).dropna()
    rows, deciles = [], []
    for date, group in work.groupby("trade_date"):
        if len(group) < 200:
            continue
        order = np.argsort(-group["score"].to_numpy())
        values = group["label"].to_numpy()
        pool = float(values.mean())
        entry = {"trade_date": date, "ic": float(group["score"].corr(group["label"], method="spearman"))}
        threshold = np.quantile(values, 0.9)
        for k in KS:
            top_values = values[order[:k]]
            random_means = values[rng.integers(0, len(values), size=(N_RANDOM, k))].mean(axis=1)
            entry[f"top{k}_excess"] = float(top_values.mean() - pool)
            entry[f"top{k}_vs_random"] = float(top_values.mean() - random_means.mean())
            entry[f"top{k}_hit_decile"] = float((top_values >= threshold).mean())
        bucket = pd.qcut(pd.Series(group["score"].to_numpy()), 10, labels=False, duplicates="drop")
        deciles.append(pd.Series(values).groupby(bucket.to_numpy()).mean())
        rows.append(entry)
    detail = pd.DataFrame(rows)
    summary = {"arm": name, "days": len(detail),
               "ic": round(float(detail["ic"].mean()), 4),
               "ic_t": round(float(detail["ic"].mean() / detail["ic"].std() * np.sqrt(len(detail))), 2)}
    for k in KS:
        summary[f"top{k}_excess_%"] = round(float(detail[f"top{k}_excess"].mean()) * 100, 3)
        summary[f"top{k}_vs_random_t"] = round(float(detail[f"top{k}_vs_random"].mean() / detail[f"top{k}_vs_random"].std()
                                                       * np.sqrt(len(detail))), 2)
        summary[f"top{k}_hit_decile"] = round(float(detail[f"top{k}_hit_decile"].mean()), 3)
    dec = pd.DataFrame(deciles).mean(axis=0)
    dec.name = name
    return summary, dec


def main() -> int:
    import json
    import tomllib
    import lightgbm as lgb
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = tomllib.load(open(ROOT / "config/cn_pipeline.toml", "rb"))
    lg = cfg["lightgbm"]
    svc = MarketDataService(base_dir=str(ROOT / "assets/data"))
    panel, features, label = svc._clean_panel_training_data(
        market="CN", factor_set=cfg["pipeline"]["factor_set"], frequency="daily", adjust="qfq",
        days=int(lg["days"]), label_horizon=int(lg["label_horizon"]),
        cleaning_version=cfg["clean_panel"]["cleaning_version"], label_mode=lg["label_mode"],
        startup_only=False, label_path_horizon=60, preserve_startup_context=False,
        min_stock_count=int(cfg["pipeline"]["min_training_stocks"]), end_date=None, show_progress=False)
    panel["trade_date"] = pd.to_datetime(panel["trade_date"])
    labeled = panel.loc[panel[label].notna()].copy()
    admitted = json.loads((PROTOCOL / "factor_ic_admitted.json").read_text(encoding="utf-8"))
    cols = [c for c in [*(admitted["admitted_A"]), *(admitted["admitted_B"])] if c in labeled.columns]
    dates = np.array(sorted(labeled["trade_date"].unique()))
    valid_start = pd.Timestamp(dates[-60])
    train = labeled[labeled["trade_date"] < valid_start].copy()
    valid = labeled[labeled["trade_date"] >= valid_start].copy()
    print(f"[D] 准入 {len(cols)} 列 | train {train.trade_date.min().date()}~{train.trade_date.max().date()} "
          f"| valid {valid.trade_date.min().date()}~{valid.trade_date.max().date()}", flush=True)

    train_p, _ = preprocess_features_by_date(train, cols)
    valid_p, _ = preprocess_features_by_date(valid, cols)
    rank_pct = train_p.groupby("trade_date")[label].rank(pct=True)
    params = {"n_estimators": int(lg["n_estimators"]), "learning_rate": float(lg["learning_rate"]),
              "num_leaves": int(lg["num_leaves"]), "max_depth": int(lg["max_depth"]),
              "min_child_samples": int(lg["min_child_samples"]), "reg_lambda": float(lg["reg_lambda"]),
              "subsample": 0.9, "colsample_bytree": 0.9, "random_state": 42, "n_jobs": -1, "verbosity": -1}
    rng = np.random.default_rng(31)
    summaries, decile_frame = [], None

    arms = []
    # 生产等价基线：标签是**当日横截面分位**（train_lightgbm_panel 内部就是这么造的），
    # 直接回归原始收益会被厚尾支配（p99≈+55%），不能当基线。
    arms.append(("reg_rank", "regression", (rank_pct - 0.5).to_numpy()))
    arms.append(("reg_return", "regression", train_p[label].to_numpy()))
    arms.append(("cls_topdecile", "binary", (rank_pct >= 0.9).astype(int).to_numpy()))
    arms.append(("reg_headrank", "regression", np.clip(rank_pct - 0.9, 0, None).to_numpy()))
    for name, objective, target in arms:
        t0 = time.time()
        model = lgb.LGBMClassifier(objective=objective, **params) if objective == "binary" else lgb.LGBMRegressor(objective=objective, **params)
        model.fit(train_p[cols], target)
        if objective == "binary":
            score = model.predict_proba(valid_p[cols])[:, 1]
        else:
            score = model.predict(valid_p[cols])
        summary, dec = evaluate(valid, score, label, name, rng)
        summaries.append(summary)
        decile_frame = dec.to_frame() if decile_frame is None else decile_frame.join(dec)
        print(f"[{name:<13}] IC={summary['ic']:+.4f} t={summary['ic_t']:+.2f} | "
              f"Top20={summary['top20_excess_%']:+.3f}% (vs随机 t={summary['top20_vs_random_t']:+.2f}) | "
              f"Top20 前10%命中率={summary['top20_hit_decile']:.2f} | {time.time()-t0:.0f}s", flush=True)
        pd.DataFrame(summaries).to_csv(OUT / "metrics.csv", index=False)

    table = pd.DataFrame(summaries)
    decile_frame.to_csv(OUT / "deciles.csv")
    (OUT / "REPORT.md").write_text(
        "# D：换标签（头部相关标签）A/B\n\n"
        f"train {train.trade_date.min().date()}~{train.trade_date.max().date()} / "
        f"valid {valid.trade_date.min().date()}~{valid.trade_date.max().date()}；准入 90 列；同超参。\n\n"
        + table.to_markdown(index=False) + "\n\n## 十分位（%）\n\n" + decile_frame.mul(100).round(3).to_markdown() + "\n",
        encoding="utf-8")
    print("\n" + table.to_string(index=False), flush=True)
    print("\n[十分位 %]\n" + decile_frame.mul(100).round(2).to_string(), flush=True)
    print(f"\n[done] {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
