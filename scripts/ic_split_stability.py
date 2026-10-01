#!/usr/bin/env python
"""IC 符号诊断：因子环境的月级普查 + 模型样本外 IC 的切分稳定性。

背景（同一份数据、同一套代码，只差面板窗口/切分）：
  生产 manifest（9/24 训练，valid 2026-06-03~08-26）valid IC = +0.0140（IC 曲线峰值 +0.0231）
  族级消融 full（9/26 训练，valid 2026-06-04~08-27）valid IC = -0.0183
  更早一次 days=560                                  valid IC = -0.0354

P1  全部特征列的月级横截面 rank IC（一次面板构建，块内向量化）→ 看主因子是否在验证期反号
P2  部署中的 lightgbm（9/24 训练）在"修复后特征层"上的逐日 rank IC（分月 + t 值）
P3  固定验证窗口、只把训练集尾部再挖掉 20/40 个交易日重训 → IC 是否翻号

输出：output/research/ic_diagnosis/{feature_ic_by_month.csv, feature_ic_summary.csv,
                                   model_ic_stability.csv, SUMMARY.json}
"""
from __future__ import annotations

import argparse
import json
import re
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
UPPER = re.compile(r"^[A-Z]+[0-9]*[A-Z0-9]*$")


def base_name(column: str) -> str:
    return column[:-11] if column.endswith("_is_missing") else (column[:-6] if column.endswith("_clean") else column)


def family(column: str) -> str:
    b = base_name(column)
    for pattern, name in ((r"^GTJA\d+", "GTJA191"), (r"^ALPHA101_", "Alpha101"), (r"^TA_", "TA-Lib"),
                          (r"^moneyflow|^flow_second_wave", "moneyflow"),
                          (r"^valuation|^pe_|^pb_|^roe_|^gross_margin|^quality_value|^growth_quality", "valuation"),
                          (r"^financial", "financial"), (r"^liquidity", "liquidity"), (r"^payoff", "payoff"),
                          (r"^vol_", "vol"), (r"^vr_", "vr"), (r"^tr_|^turnover", "tr"), (r"^pv_", "pv"),
                          (r"^academic", "academic"), (r"^ip_", "ip")):
        if re.match(pattern, b):
            return name
    return "Alpha158" if UPPER.match(b) else "other"


def series_ic(frame: pd.DataFrame, col: str, label: str, min_n: int = 50) -> pd.Series:
    """逐日横截面 rank IC（DatetimeIndex，即使为空也保持日期索引）。"""
    out: dict = {}
    for date, group in frame.groupby("trade_date"):
        sub = group[[col, label]].dropna()
        if len(sub) >= min_n:
            value = float(sub[col].corr(sub[label], method="spearman"))
            if np.isfinite(value):
                out[pd.Timestamp(date)] = value
    index = pd.DatetimeIndex([k for k in out])
    return pd.Series([out[k] for k in out], index=index, dtype="float64").sort_index()


def stats(series: pd.Series) -> dict:
    n = int(len(series))
    if n == 0:
        return {"days": 0, "mean": None, "median": None, "t": None, "hit": None}
    mean, std = float(series.mean()), float(series.std())
    return {"days": n, "mean": round(mean, 5), "median": round(float(series.median()), 5),
            "t": round(mean / std * np.sqrt(n), 2) if std > 1e-12 else None,
            "hit": round(float((series > 0).mean()), 3)}


def monthly_ic_census(panel: pd.DataFrame, cols: list[str], label: str, block: int = 120) -> pd.DataFrame:
    """按月计算每个特征列的"月内逐日横截面 Spearman IC 的等权平均"。

    月内每个日期只取一次 rank（`rank(pct=True)`），用 `12 * mean(rc*lc)` 得到该日的
    rank IC（等距秩的方差恰为 1/12），再对月内日期取均值。把 groupby 次数从
    ~250 个交易日降到 12 个月，1,300+ 列的普查可以在分钟级完成。
    """
    month_key = panel["trade_date"].dt.to_period("M").astype(str)
    result: dict[str, dict[str, float]] = {}
    for month, month_frame in panel.groupby(month_key):
        if len(month_frame) < 500:
            continue
        dates = month_frame["trade_date"].to_numpy()
        label_values = pd.Series(month_frame[label].to_numpy())
        lc = label_values.groupby(dates).rank(pct=True).to_numpy(dtype="float64") - 0.5
        for start in range(0, len(cols), block):
            chunk = cols[start:start + block]
            raw = month_frame[chunk].to_numpy(dtype="float64", na_value=np.nan)
            ranks = pd.DataFrame(raw).groupby(dates).rank(pct=True).to_numpy(dtype="float64") - 0.5
            mask = np.isfinite(ranks) & np.isfinite(lc)[:, None]
            product = np.where(mask, ranks * np.where(mask, lc[:, None], 0.0), 0.0)
            counts = mask.sum(axis=0)
            with np.errstate(invalid="ignore", divide="ignore"):
                ic = 12.0 * product.sum(axis=0) / np.maximum(counts, 1)
            for column, value in zip(chunk, ic):
                result.setdefault(column, {})[str(month)] = float(value) if np.isfinite(value) else np.nan
        print(f"  [census] {month}: {len(month_frame):,} 行完成", flush=True)
    return pd.DataFrame(result).T


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel-days", type=int, default=365)
    ap.add_argument("--factors-only", action="store_true")
    ap.add_argument("--census", action="store_true", help="跑全部特征列的月级 IC 普查")
    ap.add_argument("--extra-carve", type=int, nargs="*", default=[20, 40])
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    import tomllib
    cfg = tomllib.load(open(ROOT / "config/cn_pipeline.toml", "rb"))
    lg = cfg["lightgbm"]

    t0 = time.time()
    print(f"[panel] days={args.panel_days} 构建中…", flush=True)
    svc = MarketDataService(base_dir=str(ROOT / "assets/data"))
    panel, features, label = svc._clean_panel_training_data(
        market="CN", factor_set=cfg["pipeline"]["factor_set"], frequency="daily", adjust="qfq",
        days=int(args.panel_days), label_horizon=int(lg["label_horizon"]),
        cleaning_version=cfg["clean_panel"]["cleaning_version"], label_mode=lg["label_mode"],
        startup_only=False, label_path_horizon=60, preserve_startup_context=False,
        min_stock_count=int(cfg["pipeline"]["min_training_stocks"]), end_date=None, show_progress=False,
    )
    panel["trade_date"] = pd.to_datetime(panel["trade_date"])
    print(f"[panel] {len(panel):,} 行 / {len(features):,} 特征 / 标签 {label} / {time.time()-t0:.0f}s", flush=True)
    labeled = panel.loc[panel[label].notna()]
    dates = np.array(sorted(labeled["trade_date"].unique()))
    valid_start, valid_end = pd.Timestamp(dates[-60]), pd.Timestamp(dates[-1])
    fam_count: dict = {}
    for column in features:
        fam_count[family(column)] = fam_count.get(family(column), 0) + 1
    print(f"[panel] 标签可用 {pd.Timestamp(dates[0]).date()}~{pd.Timestamp(dates[-1]).date()}；"
          f"验证窗口 {valid_start.date()}~{valid_end.date()}", flush=True)
    print(f"[panel] 特征族分布: {dict(sorted(fam_count.items(), key=lambda kv: -kv[1]))}", flush=True)

    summary: dict = {"panel_rows": int(len(panel)), "feature_count": int(len(features)), "label_column": label,
                     "valid_start": str(valid_start.date()), "valid_end": str(valid_end.date()),
                     "family_counts": fam_count, "factor_set": cfg["pipeline"]["factor_set"]}
    train_months = sorted({str(p) for p in pd.PeriodIndex(dates[dates < np.datetime64(valid_start)], freq="M").unique()})
    valid_months = sorted({str(p) for p in pd.PeriodIndex(dates[dates >= np.datetime64(valid_start)], freq="M").unique()})
    summary["train_months"] = train_months
    summary["valid_months"] = valid_months

    if args.census:
        print(f"[P1] 全部 {len(features)} 列月级 IC 普查…", flush=True)
        census = monthly_ic_census(panel, list(features), label)
        census = census.reindex(columns=sorted(set(census.columns)))
        census["family"] = [family(c) for c in census.index]
        census["train_mean"] = census[train_months].mean(axis=1)
        census["train_std"] = census[train_months].std(axis=1)
        census["valid_mean"] = census[valid_months].mean(axis=1)
        census["sign_flip"] = np.sign(census["train_mean"]) != np.sign(census["valid_mean"])
        census.sort_values("valid_mean", key=lambda s: -s.abs()).to_csv(OUT / "feature_ic_by_month.csv")
        fam = census.groupby("family").agg(features=("train_mean", "size"),
                                           train_ic=("train_mean", "mean"), valid_ic=("valid_mean", "mean"),
                                           valid_abs_ic=("valid_mean", lambda s: s.abs().mean()),
                                           flip_rate=("sign_flip", "mean")).round(4)
        fam = fam.sort_values("valid_ic")
        fam.to_csv(OUT / "family_ic_summary.csv")
        print("\n[P1] 族级 IC（train 月均 / valid 月均 / |IC| / 反号比例）:\n" + fam.to_string(), flush=True)
        top = census.reindex(census["valid_mean"].abs().sort_values(ascending=False).index).head(25)
        print("\n[P1] 验证期 |IC| 最大的 25 列:\n" +
              top[["family", "train_mean", "valid_mean"] + valid_months].round(4).to_string(), flush=True)
        summary["family_ic"] = fam.reset_index().to_dict("records")
        summary["census_top_valid"] = top[["family", "train_mean", "valid_mean"]].round(4).reset_index().to_dict("records")

    # ---- P2 部署中的模型在修复后特征层上的 IC ----
    prod_dir = ROOT / "output/models/cn/lightgbm/alpha_zoo_hk"
    man = json.load(open(prod_dir / "model_manifest.json"))
    import lightgbm as lgb
    frame = labeled[labeled["trade_date"].between(pd.Timestamp(man["validation_start"]), pd.Timestamp(man["validation_end"]))].copy()
    cols = man["feature_columns"]
    proc = dict(man["preprocessing"].get("config") or {"mode": "qlib_robust"})
    mode = proc.pop("mode", "qlib_robust")
    X, _ = preprocess_features_by_date(frame, cols, mode=mode, **proc)
    booster = lgb.Booster(model_file=str(prod_dir / "model.txt"))
    frame["prod_pred"] = booster.predict(X[cols].to_numpy(dtype=float))
    prod_ic = series_ic(frame, "prod_pred", label)
    print(f"\n[P2] 部署模型（{man['train_start']}~{man['train_end']} 训练，"
          f"{man['validation_start']}~{man['validation_end']} 验证）在修复后特征层上重算: {stats(prod_ic)}", flush=True)
    print("[P2] 逐月:\n" + prod_ic.groupby(prod_ic.index.to_period("M")).mean().round(4).to_string(), flush=True)
    summary["production_model"] = {"path": str(prod_dir / "model.txt"), "train": [man["train_start"], man["train_end"]],
                                   "validation": [man["validation_start"], man["validation_end"]],
                                   "recomputed_on_repaired_panel": stats(prod_ic),
                                   "monthly": {str(k): round(float(v), 4) for k, v in prod_ic.groupby(prod_ic.index.to_period("M")).mean().items()}}
    if args.factors_only:
        (OUT / "SUMMARY.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print("\n[done] factors-only", flush=True)
        return 0

    # ---- P3 固定验证窗口，只挪训练集尾部 ----
    rows: list[dict] = []
    for carve in [0, *[int(c) for c in args.extra_carve]]:
        pre = dates[dates < np.datetime64(valid_start)]
        carve_dates = {pd.Timestamp(d) for d in pre[-carve:]} if carve else set()
        sub = labeled[~labeled["trade_date"].isin(carve_dates)]
        model_dir = OUT / "models" / f"carve{carve}"
        t1 = time.time()
        res = train_lightgbm_panel(
            sub, features, model_dir=model_dir, label_column=label,
            validation_days=60, cleaning_version=cfg["clean_panel"]["cleaning_version"],
            factor_set=cfg["pipeline"]["factor_set"], embargo_days=lg.get("embargo_days"),
            min_feature_coverage=float(cfg["model_features"]["min_feature_coverage"]),
            drop_constant_features=bool(cfg["model_features"]["drop_constant_features"]),
            n_estimators=int(lg["n_estimators"]), learning_rate=float(lg["learning_rate"]),
            num_leaves=int(lg["num_leaves"]), max_depth=int(lg["max_depth"]),
            min_child_samples=int(lg["min_child_samples"]), reg_lambda=float(lg["reg_lambda"]),
            early_stopping_rounds=int(lg["early_stopping_rounds"]), min_trees=int(lg["min_trees"]),
            eval_metric=lg["eval_metric"], show_progress=False)
        m2 = json.load(open(res["artifact"]["manifest_path"]))
        vframe = labeled[labeled["trade_date"].between(pd.Timestamp(m2["validation_start"]), pd.Timestamp(m2["validation_end"]))].copy()
        c2 = m2["feature_columns"]
        p2 = dict(m2["preprocessing"].get("config") or {"mode": "qlib_robust"})
        m2_mode = p2.pop("mode", "qlib_robust")
        X2, _ = preprocess_features_by_date(vframe, c2, mode=m2_mode, **p2)
        b2 = lgb.Booster(model_file=res["artifact"]["model_path"])
        vframe["pred"] = b2.predict(X2[c2].to_numpy(dtype=float))
        ic = series_ic(vframe, "pred", label)
        months = ic.groupby(ic.index.to_period("M")).mean()
        rows.append({"carve_sessions": carve, "train_start": m2["train_start"], "train_end": m2["train_end"],
                     "valid_start": m2["validation_start"], "valid_end": m2["validation_end"],
                     "features_used": int(m2["feature_count"]),
                     "reported_valid_ic": (res.get("validation_ic") or {}).get("value"),
                     "reported_best_ic": (res.get("validation_ic") or {}).get("best"),
                     "top50_lift": None, **stats(ic),
                     **{f"ic_{str(k)}": round(float(v), 4) for k, v in months.items()}})
        pd.DataFrame(rows).to_csv(OUT / "model_ic_stability.csv", index=False)
        print(f"[P3] carve={carve:>3} 训练 {m2['train_start']}~{m2['train_end']} | 重算 IC={rows[-1]['mean']} "
              f"t={rows[-1]['t']} n={rows[-1]['days']} | 训练时报告={rows[-1]['reported_valid_ic']} | {time.time()-t1:.0f}s", flush=True)
    summary["split_stability"] = rows
    (OUT / "SUMMARY.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("\n[done]", OUT, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
