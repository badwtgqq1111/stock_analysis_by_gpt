#!/usr/bin/env python
"""按"月频秩 IC 合格标准"审计整个因子池与模型（A 股全 A，20 交易日前瞻）。

判定口径（非重叠月、样本外、秩 IC）：
  |IC| < 0.02            无效
  0.02 ~ 0.04            合格（只能进多因子池）
  0.04 ~ 0.07            优质单因子
  >= 0.07                很强（重点查过拟合）
  ICIR < 0.3 不稳定 / 0.3~0.5 合格 / 0.5~0.8 很好 / > 0.8 很强
模型（LightGBM 合成）合格线：|IC| >= 0.03，ICIR >= 0.5。

输出：output/research/ic_rubric_audit/
  feature_ic_series.csv      月 × 全特征列 的秩 IC
  feature_scorecard.csv      每列 mean/std/ICIR/t/正号月占比/近 6 月均值/等级
  family_scorecard.csv       族级汇总
  decay.csv                  短名单在 5 日 / 20 日标签上的衰减
  universe_split.csv         短名单在 大额/小额 两半截面上的 IC
  model_audit.csv            已保存模型在各自样本外窗口的非重叠月 IC / ICIR / t
  REPORT.md
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
import re  # noqa: E402

OUT = ROOT / "output/research/ic_rubric_audit"
UPPER = re.compile(r"^[A-Z]+[0-9]*[A-Z0-9]*$")
LIQUIDITY_PROXY = "pv_log_amount_clean"
KEY_FACTORS = ["MA60_clean", "QTLD60_clean", "ROC60_clean", "RANK60_clean", "TA_RSI_clean",
               "pb_ind_pct_clean", "pe_ind_pct_clean", "valuation_pb_clean", "valuation_market_cap_log_clean",
               "moneyflow_net_z_5d_clean", "turnover_rate_clean", "vol_volatility_20_clean"]


def family(column: str) -> str:
    b = column[:-11] if column.endswith("_is_missing") else (column[:-6] if column.endswith("_clean") else column)
    for pattern, name in ((r"^GTJA\d+", "GTJA191"), (r"^ALPHA101_", "Alpha101"), (r"^TA_", "TA-Lib"),
                          (r"^moneyflow|^flow_second_wave", "moneyflow"),
                          (r"^valuation|^pe_|^pb_|^roe_|^gross_margin|^quality_value|^growth_quality", "valuation"),
                          (r"^financial", "financial"), (r"^liquidity", "liquidity"), (r"^payoff", "payoff"),
                          (r"^vol_", "vol"), (r"^vr_", "vr"), (r"^tr_|^turnover", "tr"), (r"^pv_", "pv"),
                          (r"^academic", "academic")):
        if re.match(pattern, b):
            return name
    return "Alpha158" if UPPER.match(b) else "other"


def monthly_ic(panel: pd.DataFrame, cols: list[str], label: str, block: int = 120,
               split_column: str | None = None) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    """月内逐日横截面 rank IC 的等权平均（按整月聚合，月≈20 交易日，与非重叠口径接近）。"""
    month_key = panel["trade_date"].dt.to_period("M").astype(str)
    result: dict[str, dict[str, float]] = {}
    split_res: dict[str, dict[str, float]] = {}
    for month, frame in panel.groupby(month_key):
        if len(frame) < 500:
            continue
        dates = frame["trade_date"].to_numpy()
        lc_all = pd.Series(frame[label].to_numpy()).groupby(dates).rank(pct=True).to_numpy(dtype="float64") - 0.5
        masks = {"all": np.ones(len(frame), dtype=bool)}
        if split_column and split_column in frame.columns:
            proxy = pd.to_numeric(frame[split_column], errors="coerce").to_numpy()
            median = np.nanmedian(proxy)
            masks["high_amount"] = proxy >= median
            masks["low_amount"] = proxy < median
        for start in range(0, len(cols), block):
            chunk = cols[start:start + block]
            raw = frame[chunk].to_numpy(dtype="float64", na_value=np.nan)
            ranks = pd.DataFrame(raw).groupby(dates).rank(pct=True).to_numpy(dtype="float64") - 0.5
            for tag, mask in masks.items():
                valid = mask & np.isfinite(lc_all)
                if valid.sum() < 300:
                    continue
                prod = np.where(np.isfinite(ranks[valid]), ranks[valid] * lc_all[valid, None], 0.0)
                counts = np.isfinite(ranks[valid]).sum(axis=0)
                with np.errstate(invalid="ignore", divide="ignore"):
                    ic = 12.0 * prod.sum(axis=0) / np.maximum(counts, 1)
                target = result if tag == "all" else split_res
                for column, value in zip(chunk, ic):
                    target.setdefault(column, {})[str(month)] = float(value) if np.isfinite(value) else np.nan
    split_frame = pd.DataFrame(split_res).T if split_res else None
    return pd.DataFrame(result).T, split_frame


def grade(mean_ic: float, icir: float) -> str:
    a = abs(mean_ic)
    if a < 0.02:
        return "无效"
    if a < 0.04:
        return "合格(入池)"
    if a < 0.07:
        return "优质"
    return "很强/查过拟合"


def scorecard(series: pd.DataFrame, months: list[str]) -> pd.DataFrame:
    sub = series[months]
    mean = sub.mean(axis=1)
    std = sub.std(axis=1)
    icir = mean / std.replace(0, np.nan)
    n = sub.notna().sum(axis=1)
    t = icir * np.sqrt(n)
    out = pd.DataFrame({"ic_mean": mean, "ic_std": std, "icir": icir, "t": t, "months": n,
                        "positive_share": (sub > 0).sum(axis=1) / n.replace(0, np.nan),
                        "last6_mean": sub[months[-6:]].mean(axis=1)})
    out["grade"] = [grade(m, i) for m, i in zip(out["ic_mean"], out["icir"])]
    out["family"] = [family(c) for c in out.index]
    return out.sort_values("icir", key=lambda s: -s.abs())


def main() -> int:
    import tomllib
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = tomllib.load(open(ROOT / "config/cn_pipeline.toml", "rb"))
    lg = cfg["lightgbm"]
    svc = MarketDataService(base_dir=str(ROOT / "assets/data"))

    def build(horizon, days):
        panel, features, label = svc._clean_panel_training_data(
            market="CN", factor_set=cfg["pipeline"]["factor_set"], frequency="daily", adjust="qfq",
            days=days, label_horizon=int(horizon), cleaning_version=cfg["clean_panel"]["cleaning_version"],
            label_mode="forward_return", startup_only=False, label_path_horizon=60, preserve_startup_context=False,
            min_stock_count=int(cfg["pipeline"]["min_training_stocks"]), end_date=None, show_progress=False)
        panel["trade_date"] = pd.to_datetime(panel["trade_date"])
        return panel.loc[panel[label].notna()], list(features), label

    t0 = time.time()
    panel, features, label20 = build(20, 756)
    months = sorted(panel["trade_date"].dt.to_period("M").astype(str).unique())
    print(f"[panel] 20 日前瞻 {pd.Timestamp(panel.trade_date.min()).date()}~{pd.Timestamp(panel.trade_date.max()).date()} "
          f"| {len(panel):,} 行 | {len(features):,} 列 | {len(months)} 个月 | {time.time()-t0:.0f}s", flush=True)

    series, split_series = monthly_ic(panel, list(features), label20, split_column=LIQUIDITY_PROXY)
    series = series.reindex(columns=months)
    series.to_csv(OUT / "feature_ic_series.csv")
    card = scorecard(series, months)
    card.to_csv(OUT / "feature_scorecard.csv")
    fam = card.groupby("family").agg(features=("ic_mean", "size"), ic_mean=("ic_mean", "mean"),
                                     ic_abs=("ic_mean", lambda s: s.abs().mean()), icir_abs=("icir", lambda s: s.abs().median()),
                                     pass_rate=("grade", lambda s: float((s != "无效").mean()))).round(4)
    fam = fam.sort_values("ic_abs", ascending=False)
    fam.to_csv(OUT / "family_scorecard.csv")
    print("\n[族级] 月频秩 IC 审计（全 A，20 日）:\n" + fam.to_string(), flush=True)

    bucket = card["grade"].value_counts()
    print("\n[列级] 等级分布:\n" + bucket.to_string(), flush=True)
    print(f"\n[列级] |IC|>=0.02 且 |ICIR|>=0.3（真正可用）的列数: "
          f"{int(((card.ic_mean.abs() >= 0.02) & (card.icir.abs() >= 0.3)).sum())} / {len(card)}", flush=True)
    print("\n[列级] |ICIR| 前 20 列:\n"
          + card.head(20)[["family", "ic_mean", "icir", "t", "positive_share", "grade"]].round(4).to_string(), flush=True)

    # ---- 衰减（5 日 vs 20 日标签）与截面分层 ----
    shortlist = list(dict.fromkeys(list(card.head(30).index) + [c for c in KEY_FACTORS if c in features]))
    panel5, features5, label5 = build(5, 756)
    months5 = sorted(panel5["trade_date"].dt.to_period("M").astype(str).unique())
    series5, _ = monthly_ic(panel5, shortlist, label5)
    card5 = scorecard(series5, months5)
    decay = pd.DataFrame({"ic_5d": card5["ic_mean"], "ic_20d": card.reindex(card5.index)["ic_mean"],
                          "decay_ratio": card5["ic_mean"] / card.reindex(card5.index)["ic_mean"],
                          "family": [family(c) for c in card5.index]})
    decay.sort_values("ic_20d", key=lambda s: -s.abs()).round(4).to_csv(OUT / "decay.csv")
    print("\n[衰减] 短名单 5 日 vs 20 日 IC（ratio<1 表示信号在 20 日尺度更强）:\n"
          + decay.reindex(decay["ic_20d"].abs().sort_values(ascending=False).index).head(15).round(4).to_string(), flush=True)

    if split_series is not None and not split_series.empty:
        half = scorecard(split_series.reindex(columns=months).dropna(how="all"), months)
        keep = [c for c in shortlist if c in half.index]
        split_view = pd.DataFrame({"ic_all": card.reindex(keep)["ic_mean"],
                                   "icir_all": card.reindex(keep)["icir"]}).join(
            half.loc[[c for c in keep if c in half.index], ["ic_mean", "icir"]].rename(
                columns={"ic_mean": "ic_all_rows", "icir": "icir_all_rows"}))
        split_view.round(4).to_csv(OUT / "universe_split.csv")
        print("\n[分层] 短名单在全截面 vs 高成交额半边:\n" + split_view.round(4).head(12).to_string(), flush=True)

    # ---- 已保存模型在各自样本外窗口的非重叠月 IC / ICIR ----
    import lightgbm as lgb
    rows = []
    models = {
        "部署_lightgbm_9-24": ROOT / "output/models/cn/lightgbm/alpha_zoo_hk",
        "消融_full_9-26": ROOT / "output/research/family_ablation/models/full",
        "AB_only_MA60": ROOT / "output/research/ic_diagnosis/models/ab_only_MA60",
        "AB_top20": ROOT / "output/research/ic_diagnosis/models/ab_top20_by_train_ic",
        "重训_carve40": ROOT / "output/research/ic_diagnosis/models/carve40",
    }
    full_panel, full_features, label = build(20, 756)
    for tag, d in models.items():
        if not (d / "model.txt").is_file():
            continue
        man = json.load(open(d / "model_manifest.json"))
        cols = man["feature_columns"]
        frame = full_panel[(full_panel.trade_date >= pd.Timestamp(man["validation_start"])) &
                           (full_panel.trade_date <= pd.Timestamp(man["validation_end"]))].copy()
        if frame.empty:
            print(f"[model] {tag}: 面板窗口不含 {man['validation_start']}~{man['validation_end']}，跳过", flush=True)
            continue
        proc = dict(man["preprocessing"].get("config") or {"mode": "qlib_robust"})
        mode = proc.pop("mode", "qlib_robust")
        frame, _ = preprocess_features_by_date(frame, cols, mode=mode, **proc)
        booster = lgb.Booster(model_file=str(d / "model.txt"))
        frame = frame.assign(pred=booster.predict(frame[cols].to_numpy(dtype=float)))
        per = {}
        for date, group in frame.groupby("trade_date"):
            sub = group[["pred", label]].dropna()
            if len(sub) >= 50:
                per[pd.Timestamp(date)] = float(sub.pred.corr(sub[label], method="spearman"))
        daily = pd.Series(per).sort_index()
        monthly = daily.groupby(daily.index.to_period("M")).mean()
        icir = float(monthly.mean() / monthly.std()) if len(monthly) > 1 and monthly.std() > 0 else np.nan
        rows.append({"model": tag, "train_end": man["train_end"], "valid_start": man["validation_start"],
                     "valid_end": man["validation_end"], "features_used": int(man["feature_count"]),
                     "daily_ic_mean": round(float(daily.mean()), 4), "daily_ic_median": round(float(daily.median()), 4),
                     "daily_t": round(float(daily.mean() / daily.std() * np.sqrt(len(daily))), 2),
                     "daily_ic_std": round(float(daily.std()), 4), "neg_days": int((daily < 0).sum()), "days": len(daily),
                     "monthly_ic_mean": round(float(monthly.mean()), 4), "monthly_icir": round(icir, 3) if np.isfinite(icir) else None,
                     "monthly_points": len(monthly),
                     "monthly": ", ".join(f"{str(k)[2:]}:{v:+.3f}" for k, v in monthly.items())})
        print(f"[model] {tag}: 月频(非重叠) IC={rows[-1]['monthly_ic_mean']:+.4f} ICIR={rows[-1]['monthly_icir']} "
              f"点数={rows[-1]['monthly_points']} | 日频 IC={rows[-1]['daily_ic_mean']:+.4f} t={rows[-1]['daily_t']} "
              f"负值天数={rows[-1]['neg_days']}/{rows[-1]['days']}", flush=True)
    pd.DataFrame(rows).to_csv(OUT / "model_audit.csv", index=False)
    print(f"\n[done] {OUT} | 用时 {time.time()-t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
