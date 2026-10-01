#!/usr/bin/env python
"""标签完整性核对 + 特征取舍 A/B（单因子 / 前 20 列 / 去重后全集 / 全集）。

问题：
  1. `forward_return_20d` 是否真的等于 close(t+20)/close(t)-1（用原始日线独立复算抽样核对）；
  2. 主趋势列（MA60 一族）在验证期的 IC 是不是"数据错位"造成的假象（对照同一股票的 20 日后向收益）；
  3. 同一划分下，只喂 1 列 / 20 列 / 去重后的 667 列 / 全集 1,366 列，验证集 IC 各是多少 —— 直接量化"塞太多因子"的代价。

输出：output/research/ic_diagnosis/{label_integrity.csv, feature_ab.csv, LABEL_AB_REPORT.md}
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
from factor_engine.ml.model_training import train_lightgbm_panel  # noqa: E402
from factor_engine.ml.preprocessing import preprocess_features_by_date  # noqa: E402

OUT = ROOT / "output/research/ic_diagnosis"


def daily_ic(frame, column, label, min_n=50):
    per = {}
    for date, group in frame.groupby("trade_date"):
        sub = group[[column, label]].dropna()
        if len(sub) >= min_n:
            value = float(sub[column].corr(sub[label], method="spearman"))
            if np.isfinite(value):
                per[pd.Timestamp(date)] = value
    return pd.Series(per).sort_index()


def main() -> int:
    import tomllib
    cfg = tomllib.load(open(ROOT / "config/cn_pipeline.toml", "rb"))
    lg = cfg["lightgbm"]
    svc = MarketDataService(base_dir=str(ROOT / "assets/data"))
    panel, features, label = svc._clean_panel_training_data(
        market="CN", factor_set=cfg["pipeline"]["factor_set"], frequency="daily", adjust="qfq",
        days=365, label_horizon=int(lg["label_horizon"]), cleaning_version=cfg["clean_panel"]["cleaning_version"],
        label_mode=lg["label_mode"], startup_only=False, label_path_horizon=60, preserve_startup_context=False,
        min_stock_count=int(cfg["pipeline"]["min_training_stocks"]), end_date=None, show_progress=False)
    panel["trade_date"] = pd.to_datetime(panel["trade_date"])
    labeled = panel.loc[panel[label].notna()].copy()
    print("[label] 面板标签列", label, "| 取值分位:", labeled[label].quantile([0.01, 0.5, 0.99]).round(4).to_dict(), flush=True)

    # ---- 1. 标签独立复算 ----
    codes = sorted(labeled["stock_code"].astype(str).unique())
    rng = np.random.default_rng(7)
    sample = list(rng.choice(codes, size=40, replace=False))
    frames = []
    for code in sample:
        part = svc.warehouse.read_ohlcv(stock_code=code, market="CN", frequency="daily", adjust="qfq",
                                        start_date="2026-01-01", end_date="2026-09-30")
        if part is not None and len(part):
            frames.append(part)
    bars = pd.concat(frames, ignore_index=True)
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    bars = bars.sort_values(["stock_code", "trade_date"])
    bars["fwd20"] = bars.groupby("stock_code")["close"].shift(-20) / bars["close"] - 1.0
    merged = bars.merge(labeled[["stock_code", "trade_date", label]], on=["stock_code", "trade_date"], how="inner").dropna(subset=["fwd20", label])
    diff = (merged["fwd20"] - merged[label]).abs()
    print(f"[1] 标签复算：{len(merged):,} 行，最大绝对差 {diff.max():.3e}，中位差 {diff.median():.3e}，"
          f"相关系数 {merged['fwd20'].corr(merged[label]):.6f}", flush=True)
    pd.DataFrame([{"rows": int(len(merged)), "max_abs_diff": float(diff.max()), "median_abs_diff": float(diff.median()),
                   "corr": float(merged["fwd20"].corr(merged[label])),
                   "checked_stocks": len(sample)}]).to_csv(OUT / "label_integrity.csv", index=False)

    # ---- 2. 主趋势列：前向 vs 后向 IC（错位检测）----
    dates = np.array(sorted(labeled["trade_date"].unique()))
    valid_start = pd.Timestamp(dates[-60])
    labeled = labeled.sort_values(["stock_code", "trade_date"])
    labeled["_back20"] = labeled.groupby("stock_code")[label].shift(20)
    rows = []
    for column in [c for c in ["MA60_clean", "ROC60_clean", "QTLD60_clean", "RANK60_clean"] if c in labeled.columns]:
        fwd = daily_ic(labeled[labeled.trade_date >= valid_start], column, label)
        bwd = daily_ic(labeled[labeled.trade_date >= valid_start], column, "_back20")
        rows.append({"factor": column, "ic_forward20": round(float(fwd.mean()), 4),
                     "ic_backward20": round(float(bwd.mean()), 4)})
    table = pd.DataFrame(rows)
    print("\n[2] 主趋势列 前向/后向 IC（真动量应有同号后向 IC；错位泄漏会出现前向强、后向≈0）:\n"
          + table.to_string(index=False), flush=True)

    # ---- 3. 特征取舍 A/B ----
    masks = [c for c in features if c.endswith("_is_missing")]
    census = pd.read_csv(OUT / "feature_ic_by_month.csv", index_col=0)
    train_cols = [c for c in census.columns if c.startswith("2025-") or c.startswith("2026-0")]
    train_cols = [c for c in train_cols if c not in {"2026-06", "2026-07", "2026-08"}]
    ranked = census.loc[[c for c in census.index if c in features], train_cols].abs().mean(axis=1).sort_values(ascending=False)
    top20 = [c for c in ranked.index[:20] if not c.endswith("_is_missing")]
    dup = pd.read_csv(OUT / "feature_duplicates.csv")
    dup_columns = set()
    for members in dup["members"]:
        parts = [m for m in members.split(",") if m]
        dup_columns.update(parts[1:])
    dedup = [c for c in features if c not in dup_columns]
    arms = {"only_MA60": ["MA60_clean"], "top20_by_train_ic": top20,
            "dedup_minus_duplicates": dedup, "full": list(features)}
    rows = []
    for name, cols in arms.items():
        cols = [c for c in cols if c in labeled.columns]
        t0 = time.time()
        res = train_lightgbm_panel(
            labeled, cols, model_dir=OUT / "models" / f"ab_{name}", label_column=label,
            validation_days=60, cleaning_version=cfg["clean_panel"]["cleaning_version"],
            factor_set=cfg["pipeline"]["factor_set"], embargo_days=lg.get("embargo_days"),
            min_feature_coverage=float(cfg["model_features"]["min_feature_coverage"]),
            drop_constant_features=bool(cfg["model_features"]["drop_constant_features"]),
            n_estimators=int(lg["n_estimators"]), learning_rate=float(lg["learning_rate"]),
            num_leaves=int(lg["num_leaves"]), max_depth=int(lg["max_depth"]),
            min_child_samples=int(lg["min_child_samples"]), reg_lambda=float(lg["reg_lambda"]),
            early_stopping_rounds=int(lg["early_stopping_rounds"]), min_trees=int(lg["min_trees"]),
            eval_metric=lg["eval_metric"], show_progress=False)
        man = json.load(open(res["artifact"]["manifest_path"]))
        frame = labeled[labeled["trade_date"].between(pd.Timestamp(man["validation_start"]), pd.Timestamp(man["validation_end"]))].copy()
        vcols = man["feature_columns"]
        proc = dict(man["preprocessing"].get("config") or {"mode": "qlib_robust"})
        mode = proc.pop("mode", "qlib_robust")
        X, _ = preprocess_features_by_date(frame, vcols, mode=mode, **proc)
        import lightgbm as lgb
        booster = lgb.Booster(model_file=res["artifact"]["model_path"])
        frame["pred"] = booster.predict(X[vcols].to_numpy(dtype=float))
        ic = daily_ic(frame, "pred", label)
        stats = {"days": len(ic), "mean": round(float(ic.mean()), 5), "median": round(float(ic.median()), 5),
                 "t": round(float(ic.mean() / ic.std() * np.sqrt(len(ic))), 2), "hit": round(float((ic > 0).mean()), 3)}
        rows.append({"arm": name, "features_in": len(cols), "features_used": int(man["feature_count"]),
                     "reported_valid_ic": (res.get("validation_ic") or {}).get("value"), **stats,
                     "seconds": round(time.time() - t0, 1)})
        pd.DataFrame(rows).to_csv(OUT / "feature_ab.csv", index=False)
        print(f"[3] {name:<26} in={len(cols):>5} used={man['feature_count']:>5} "
              f"valid_IC={stats['mean']:+.5f} t={stats['t']:+.2f} hit={stats['hit']} {time.time()-t0:.0f}s", flush=True)
    print("\n完成", OUT, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
