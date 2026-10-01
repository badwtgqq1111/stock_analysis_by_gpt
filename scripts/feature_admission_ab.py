#!/usr/bin/env python
"""因子准入集 A/B：用"验证窗口之前"的 IC 统计选列，再在同一切分下与全集对比。

关键：准入只用到 2026-05 及之前的月度 IC（`feature_ic_series.csv`），验证窗口是最后 60 个交易日，
因此**选列没有看到验证窗口**（否则就是自证）。三个臂：
  admitted : |IC|≥0.02 且 |ICIR|≥0.30 且 BH-FDR 显著 且 前后半段同号（不做簇去冗余，保守版）
  top20    : 准入期 |ICIR| 最大的 20 列（对照"少而强"）
  full     : 全部 1,366 列（生产现状）
评价：同切分下的验证集日频秩 IC / 月度 IC / Top-20 超额。
输出：output/research/factor_ic_protocol/admission_ab.csv
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
from factor_ic_protocol import bh_significant, newey_west_t  # noqa: E402

OUT = ROOT / "output/research/factor_ic_protocol"


def main() -> int:
    import tomllib
    cfg = tomllib.load(open(ROOT / "config/cn_pipeline.toml", "rb"))
    lg = cfg["lightgbm"]
    svc = MarketDataService(base_dir=str(ROOT / "assets/data"))
    panel, features, label = svc._clean_panel_training_data(
        market="CN", factor_set=cfg["pipeline"]["factor_set"], frequency="daily", adjust="qfq",
        days=756, label_horizon=int(lg["label_horizon"]), cleaning_version=cfg["clean_panel"]["cleaning_version"],
        label_mode=lg["label_mode"], startup_only=False, label_path_horizon=60, preserve_startup_context=False,
        min_stock_count=int(cfg["pipeline"]["min_training_stocks"]), end_date=None, show_progress=False)
    panel["trade_date"] = pd.to_datetime(panel["trade_date"])
    labeled = panel.loc[panel[label].notna()].copy()
    dates = np.array(sorted(labeled["trade_date"].unique()))
    valid_start = pd.Timestamp(dates[-60])

    series = pd.read_csv(OUT / "feature_ic_series.csv", index_col=0)
    in_months = [m for m in series.columns if str(m) < "2026-06"]
    print(f"[admission] 准入期 {in_months[0]}~{in_months[-1]}（验证窗口从 {valid_start.date()} 开始，未参与选列）", flush=True)
    sub = series[in_months]
    in_mean = sub.mean(axis=1)
    in_icir = in_mean / sub.std(axis=1).replace(0, np.nan)
    in_t = pd.Series([newey_west_t(sub.loc[c]) for c in sub.index], index=sub.index)
    from scipy import stats as sps
    in_p = pd.Series(2 * (1 - sps.norm.cdf(in_t.abs())), index=sub.index)
    in_fdr = bh_significant(in_p, q=0.05)
    stable = np.sign(sub[in_months[: len(in_months) // 2]].mean(axis=1)) == np.sign(sub[in_months[len(in_months) // 2:]].mean(axis=1))
    pick = (in_mean.abs() >= 0.02) & (in_icir.abs() >= 0.30) & in_fdr & stable
    admitted = [c for c in features if c in set(sub.index[pick])]
    top20 = in_icir.abs().sort_values(ascending=False).index[:20].tolist()
    print(f"[admission] 准入 {len(admitted)} 列 | top20 |ICIR| {len(top20)} 列", flush=True)

    rows = []
    for name, cols in (("admitted", admitted), ("top20", [c for c in top20 if c in features]), ("full", list(features))):
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
        vcols = man["feature_columns"]
        frame = labeled[labeled["trade_date"].between(pd.Timestamp(man["validation_start"]), pd.Timestamp(man["validation_end"]))].copy()
        proc = dict(man["preprocessing"].get("config") or {"mode": "qlib_robust"})
        mode = proc.pop("mode", "qlib_robust")
        X, _ = preprocess_features_by_date(frame, vcols, mode=mode, **proc)
        import lightgbm as lgb
        booster = lgb.Booster(model_file=res["artifact"]["model_path"])
        X = X.assign(pred=booster.predict(X[vcols].to_numpy(dtype=float)))
        per, excess = [], []
        for date, group in X.groupby("trade_date"):
            s = group[["pred", label]].dropna()
            if len(s) >= 50:
                per.append((pd.Timestamp(date), float(s.pred.corr(s[label], method="spearman"))))
            top = group.nlargest(20, "pred")[label].mean()
            excess.append(float(top - group[label].mean()))
        ic = pd.Series(dict(per)).sort_index()
        monthly = ic.groupby(ic.index.to_period("M")).mean()
        rows.append({"arm": name, "features_in": len(cols), "features_used": int(man["feature_count"]),
                     "reported_valid_ic": (res.get("validation_ic") or {}).get("value"),
                     "daily_ic_mean": round(float(ic.mean()), 5), "daily_ic_median": round(float(ic.median()), 5),
                     "daily_t": round(float(ic.mean() / ic.std() * np.sqrt(len(ic))), 2),
                     "neg_days": int((ic < 0).sum()), "days": len(ic),
                     "monthly_points": len(monthly),
                     "monthly_ic": ", ".join(f"{str(k)[2:]}:{v:+.4f}" for k, v in monthly.items()),
                     "top20_excess": round(float(np.mean(excess)), 5), "seconds": round(time.time() - t0, 1)})
        pd.DataFrame(rows).to_csv(OUT / "admission_ab.csv", index=False)
        print(f"[{name:<9}] in={len(cols):>5} used={man['feature_count']:>5} 日频IC={rows[-1]['daily_ic_mean']:+.5f} "
              f"t={rows[-1]['daily_t']:+.2f} 负值天={rows[-1]['neg_days']}/{rows[-1]['days']} "
              f"Top20超额={rows[-1]['top20_excess']:+.4f} {time.time()-t0:.0f}s", flush=True)
    print("\n" + pd.DataFrame(rows)[["arm", "features_in", "features_used", "daily_ic_mean", "daily_t", "neg_days",
                                     "top20_excess"]].to_string(index=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
