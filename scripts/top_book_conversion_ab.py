#!/usr/bin/env python
"""IC → 头部收益的转化实验。

背景（2026-09-27）：准入 94 列模型在验证窗口的秩 IC 是 +0.149（t=19.4、60 天零负值），
但 Top-20 等权组合相对全池均值的超额是三臂全负。IC 是全截面秩相关，实盘只吃最前面几个名字，
必须单独验收"头部"。本脚本在同一划分（训练 2025-09~2026-05、验证最后 60 个交易日）下对比：

  prod_admitted  生产准入模型（90 列，回归）
  topw           同样 90 列，但样本权重偏向标签头部（top 20% 权重 ×4）
  lambdarank     同样 90 列，lambdarank 排序目标（20 档分位标签）
  composite      准入因子等权合成（按训练期 IC 方向对齐后 z-score 平均，取 |ICIR| 前 20 列）
  random         随机篮子的对照分布（同日同规模各抽 200 组）

指标：十分位均值（label 是**原始 20 日收益**，×100 即百分比点）、Top-k 相对全池均值的超额（百分点）、Top-5/10/20/50 超额、vs 随机配对差值、
IC/ICIR。输出 output/research/top_book_conversion/{metrics.csv, deciles.csv, REPORT.md}
"""
from __future__ import annotations

import json
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

OUT = ROOT / "output/research/top_book_conversion"
PROTOCOL = ROOT / "output/research/factor_ic_protocol"
K_LIST = (5, 10, 20, 50)
N_RANDOM = 200


def basket_metrics(scores: pd.Series, labels: pd.Series, rng: np.random.Generator) -> dict:
    """同一天内：Top-k 超额 + 随机篮子对照（返回配对差值）。"""
    order = np.argsort(-scores.to_numpy())
    label_values = labels.to_numpy()
    pool_mean = float(label_values.mean())
    row = {}
    for k in K_LIST:
        row[f"top{k}_excess"] = float(label_values[order[:k]].mean() - pool_mean)
    baskets = rng.integers(0, len(label_values), size=(N_RANDOM, K_LIST[2]))
    random_means = label_values[baskets].mean(axis=1)
    row["random20_mean"] = float(random_means.mean())
    row["vs_random20"] = row["top20_excess"] - row["random20_mean"]
    row["excess_pct_points"] = row["top20_excess"] * 100.0
    return row


def evaluate(frame: pd.DataFrame, score: pd.Series, label: str, name: str, rng: np.random.Generator) -> tuple[dict, pd.DataFrame]:
    work = pd.DataFrame({"trade_date": frame["trade_date"].to_numpy(), "score": score.to_numpy(),
                         "label": frame[label].to_numpy()}).dropna()
    rows, ic_values, decile_rows = [], [], []
    for date, group in work.groupby("trade_date"):
        if len(group) < 200:
            continue
        rows.append(basket_metrics(group["score"], group["label"], rng))
        ic = float(group["score"].corr(group["label"], method="spearman"))
        if np.isfinite(ic):
            ic_values.append(ic)
        ranks = group["score"].rank(pct=True)
        bucket = pd.cut(ranks, bins=np.linspace(0, 1, 11), labels=False, include_lowest=True)
        means = group["label"].groupby(bucket).mean()
        decile_rows.append(means)
    detail = pd.DataFrame(rows)
    summary = {"arm": name, "days": len(detail),
               "ic": round(float(np.mean(ic_values)), 5) if ic_values else None,
               "ic_t": round(float(np.mean(ic_values) / np.std(ic_values) * np.sqrt(len(ic_values))), 2) if ic_values else None}
    for k in K_LIST:
        series = detail[f"top{k}_excess"]
        summary[f"top{k}_excess_%"] = round(float(series.mean()) * 100, 3)
    summary["vs_random20_%"] = round(float(detail["vs_random20"].mean()) * 100, 3)
    summary["vs_random20_t"] = round(float(detail["vs_random20"].mean() / detail["vs_random20"].std()
                                             * np.sqrt(len(detail))), 2)
    summary["hit_vs_random"] = round(float((detail["vs_random20"] > 0).mean()), 3)
    deciles = pd.DataFrame(decile_rows).mean(axis=0)
    deciles.name = name
    return summary, deciles


def main() -> int:
    import tomllib
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
    admit_names = [*(admitted["admitted_A"]), *(admitted["admitted_B"])]
    direction = {k: int(v) for k, v in (admitted.get("direction") or {}).items()}
    dates = np.array(sorted(labeled["trade_date"].unique()))
    valid_start = pd.Timestamp(dates[-60])
    print(f"[data] 准入 {len(cols)} 列 | 验证窗口 {valid_start.date()}~{pd.Timestamp(dates[-1]).date()}", flush=True)

    rng = np.random.default_rng(7)
    summaries, decile_frame = [], None

    # 1) 生产准入模型（回归）
    prod_dir = ROOT / "output/models/cn/lightgbm/alpha_zoo_hk"
    man = json.load(open(prod_dir / "model_manifest.json"))
    vcols = man["feature_columns"]
    frame = labeled[labeled["trade_date"].between(pd.Timestamp(man["validation_start"]), pd.Timestamp(man["validation_end"]))].copy()
    proc = dict(man["preprocessing"].get("config") or {"mode": "qlib_robust"})
    mode = proc.pop("mode", "qlib_robust")
    X, _ = preprocess_features_by_date(frame, vcols, mode=mode, **proc)
    import lightgbm as lgb
    booster = lgb.Booster(model_file=str(prod_dir / "model.txt"))
    score = pd.Series(booster.predict(X[vcols].to_numpy(dtype=float)), index=frame.index)
    s, d = evaluate(frame, score, label, "prod_admitted", rng)
    summaries.append(s); decile_frame = d.to_frame()
    print(f"[prod_admitted] IC={s['ic']:+.5f} t={s['ic_t']} Top20={s['top20_excess_%']:+.3f}% "
          f"vs随机={s['vs_random20_%']:+.3f}% (t={s['vs_random20_t']}, 胜率 {s['hit_vs_random']})", flush=True)

    # 2) 备选目标函数
    train = labeled[labeled["trade_date"] < valid_start]
    valid = frame
    train_p, _ = preprocess_features_by_date(train, cols)
    target_rank = train_p.groupby("trade_date")[label].rank(pct=True)
    params = {"n_estimators": int(lg["n_estimators"]), "learning_rate": float(lg["learning_rate"]),
              "num_leaves": int(lg["num_leaves"]), "max_depth": int(lg["max_depth"]),
              "min_child_samples": int(lg["min_child_samples"]), "reg_lambda": float(lg["reg_lambda"]),
              "subsample": 0.9, "colsample_bytree": 0.9, "random_state": 42, "n_jobs": -1, "verbosity": -1}
    t0 = time.time()
    top_weight = (1.0 + 3.0 * (target_rank >= 0.8)).to_numpy()
    model_topw = lgb.LGBMRegressor(objective="regression", **params)
    model_topw.fit(train_p[cols], train_p[label], sample_weight=top_weight)
    Xv, _ = preprocess_features_by_date(valid, cols)
    score = pd.Series(model_topw.predict(Xv[cols]), index=valid.index)
    s, d = evaluate(valid, score, label, "topw", rng)
    summaries.append(s); decile_frame["topw"] = d
    print(f"[topw]         IC={s['ic']:+.5f} t={s['ic_t']} Top20={s['top20_excess_%']:+.3f}% "
          f"vs随机={s['vs_random20_%']:+.3f}% (t={s['vs_random20_t']}, 胜率 {s['hit_vs_random']}) {time.time()-t0:.0f}s", flush=True)

    t0 = time.time()
    bucket = (target_rank * 20).clip(upper=19).astype(int).to_numpy()
    train_sorted = train_p.sort_values("trade_date")
    groups = train_sorted.groupby("trade_date").size().to_numpy()
    bucket_sorted = (train_sorted.groupby("trade_date")[label].rank(pct=True) * 20).clip(upper=19).astype(int).to_numpy()
    ranker = lgb.LGBMRanker(objective="lambdarank", label_gain=list(range(20)), **params)
    ranker.fit(train_sorted[cols], bucket_sorted, group=groups)
    score = pd.Series(ranker.predict(Xv[cols]), index=valid.index)
    s, d = evaluate(valid, score, label, "lambdarank", rng)
    summaries.append(s); decile_frame["lambdarank"] = d
    print(f"[lambdarank]   IC={s['ic']:+.5f} t={s['ic_t']} Top20={s['top20_excess_%']:+.3f}% "
          f"vs随机={s['vs_random20_%']:+.3f}% (t={s['vs_random20_t']}, 胜率 {s['hit_vs_random']}) {time.time()-t0:.0f}s", flush=True)

    # 3) 准入因子等权合成（方向对齐 + 逐日 z-score）
    series = pd.read_csv(PROTOCOL / "feature_ic_series.csv", index_col=0)
    in_months = [m for m in series.columns if str(m) < "2026-06"]
    sub = series[in_months]
    in_icir = (sub.mean(axis=1) / sub.std(axis=1).replace(0, np.nan)).abs()
    picks = [c for c in in_icir.sort_values(ascending=False).index if c in cols and c in admit_names][:20]
    z = valid[picks].groupby(valid["trade_date"]).transform(lambda s: (s.rank(pct=True) - 0.5))
    signs = np.array([direction.get(c, 1) or 1 for c in picks], dtype=float)
    composite = (z.to_numpy() * signs).mean(axis=1)
    score = pd.Series(composite, index=valid.index)
    s, d = evaluate(valid, score, label, "composite", rng)
    summaries.append(s); decile_frame["composite"] = d
    print(f"[composite]    IC={s['ic']:+.5f} t={s['ic_t']} Top20={s['top20_excess_%']:+.3f}% "
          f"vs随机={s['vs_random20_%']:+.3f}% (t={s['vs_random20_t']}, 胜率 {s['hit_vs_random']})", flush=True)

    table = pd.DataFrame(summaries)
    table.to_csv(OUT / "metrics.csv", index=False)
    decile_frame.to_csv(OUT / "deciles.csv")
    print("\n" + table.to_string(index=False), flush=True)
    print("\n[十分位均值（%，即 20 日收益百分点）]\n"
          + decile_frame.mul(100).round(2).to_string(), flush=True)
    order = "、".join(f"{k}:{v:+.2f}" for k, v in decile_frame["prod_admitted"].mul(100).items())
    (OUT / "REPORT.md").write_text(
        "# IC → 头部收益转化实验\n\n"
        f"口径：验证窗口 {valid_start.date()}~{pd.Timestamp(dates[-1]).date()}（最后 60 个交易日），"
        "标签 = 原始 20 日前瞻收益（单位：百分点）；随机对照 = 同日同规模抽 200 组；\n"
        "Top-k 超额 = Top-k 等权 20 日收益 − 当日全池均值。\n\n"
        + table.to_markdown(index=False) + "\n\n## 十分位均值（×100）\n\n"
        + decile_frame.mul(100).round(3).to_markdown() + f"\n\nprod_admitted 十分位: {order}\n", encoding="utf-8")
    print(f"\n[done] {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
