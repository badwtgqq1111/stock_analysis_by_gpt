#!/usr/bin/env python3
"""按 P1.24b 设计规格训练单个 sleeve（W3 起）。

流程（与设计规格 §2/§3/§4/§7 一一对应）：
  1. 特征：`factor_engine.sleeve_design.SLEEVE_SPECS[<sleeve>].features`（自动补 `_clean` 后缀），
     并把 `*_is_missing` 作为额外特征（设计 §3.3）；
  2. 标签：`r = close[t+1+h] / open[t+1] − 1`，行业中性化，域内截面 rank → [-1,1]（设计 §3.1/§3.2）；
  3. 可交易域：成交额 ≥ 下限、次日有开盘价、进场日非一字涨停（设计 §2）；
  4. 切分：Purged K-Fold(5) + purge=h + embargo=5（设计 §3.4）；
  5. 模型：LightGBM（参数取自规格），按 fold 训练/预测，输出 OOS 指标：
     逐日 Spearman IC、IC 均值/标准差/ICIR、NW t 值、十分位价差、top-k 组合收益 vs 域内等权（设计 §7）。

用法：
  uv run python scripts/train_sleeve_model.py --sleeve model_valuation --folds 5 --horizon 20
  uv run python scripts/train_sleeve_model.py --sleeve model_reversal --horizon 10
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factor_engine.sleeve_design import SLEEVE_SPECS, purge_embargo_split  # noqa: E402


def load_features(spec, start: str, end: str) -> pd.DataFrame:
    import pyarrow.dataset as ds

    files = sorted(str(p) for p in (ROOT / "assets/data/feature/clean_feature_panel/market=CN").glob("**/*.parquet"))
    want = []
    for feat in spec.features:
        want += [f"{feat}_clean", f"{feat}_is_missing"]
    dataset = ds.dataset(files, format="parquet")
    available = set(dataset.schema.names)
    cols = ["trade_date", "stock_code"] + [c for c in dict.fromkeys(want) if c in available]
    table = dataset.to_table(
        columns=cols,
        filter=(ds.field("trade_date") >= pd.Timestamp(start)) & (ds.field("trade_date") <= pd.Timestamp(end)),
    )
    frame = table.to_pandas()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    missing_flags = [c for c in frame.columns if c.endswith("_is_missing")]
    if missing_flags:
        frame["missing_count"] = frame[missing_flags].fillna(1).sum(axis=1)
    return frame


def load_bars(start: str, end: str) -> pd.DataFrame:
    import pyarrow.dataset as ds

    years = list(range(pd.Timestamp(start).year, pd.Timestamp(end).year + 1))
    files = [str(p) for p in (ROOT / "assets/data/clean/ohlcv/market=CN").glob(
        "exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet")
        if any(f"year={y}/" in str(p) for y in years)]
    table = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "open", "high", "low", "close", "amount"])
    frame = table.to_pandas()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame = frame[(frame["trade_date"] >= pd.Timestamp(start)) & (frame["trade_date"] <= pd.Timestamp(end))]
    # 幸存者偏差修复：并入退市股（否则长期样本里"活下来的赢家"会系统性抬高结果）
    delisted_path = ROOT / "assets/data/meta/cn_delisted_daily.parquet"
    if delisted_path.exists():
        extra = pd.read_parquet(delisted_path, columns=["trade_date", "stock_code", "open", "high", "low", "close", "amount"])
        extra["trade_date"] = pd.to_datetime(extra["trade_date"])
        extra = extra[(extra["trade_date"] >= pd.Timestamp(start)) & (extra["trade_date"] <= pd.Timestamp(end))]
        frame = pd.concat([frame, extra[frame.columns]], ignore_index=True).drop_duplicates(
            ["trade_date", "stock_code"], keep="first")
    return frame


def load_external_features(path: Path, start: str, end: str) -> pd.DataFrame:
    """外部特征表（如长样本估值面板）：除了键列，其余数值列全部作为特征。"""
    frame = pd.read_parquet(path)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame["stock_code"] = frame["stock_code"].astype(str)
    frame = frame[(frame["trade_date"] >= pd.Timestamp(start)) & (frame["trade_date"] <= pd.Timestamp(end))]
    keep = ["trade_date", "stock_code"] + [c for c in frame.columns
                                            if c not in ("trade_date", "stock_code") and pd.api.types.is_numeric_dtype(frame[c])]
    return frame[keep]


def load_industry() -> pd.DataFrame:
    files = sorted(str(p) for p in (ROOT / "assets/data/meta/stock_info_registry").glob("**/*.parquet"))
    if not files:
        return pd.DataFrame(columns=["stock_code", "industry_l1"])
    import pyarrow.dataset as ds

    dataset = ds.dataset(files, format="parquet")
    cols = set(dataset.schema.names)
    if "industry_l1" not in cols:
        return pd.DataFrame(columns=["stock_code", "industry_l1"])
    frame = dataset.to_table(columns=["stock_code", "industry_l1"]).to_pandas()
    return frame.dropna(subset=["industry_l1"]).drop_duplicates("stock_code")


# PIT 财务特征映射：clean panel 里的 financial_* 列（覆盖仅 19%）→ 回补的 fina_indicator 字段
FINANCIAL_PIT_MAP = {
    "financial_roe": "roe",
    "financial_roa": "roa",
    "financial_gross_margin": "grossprofit_margin",
    "financial_net_margin": "netprofit_margin",
    "financial_debt_to_assets": "debt_to_assets",
    "financial_eps_yoy": "netprofit_yoy",
    "financial_net_profit_yoy": "netprofit_yoy",
}


def attach_financial_pit(merged: pd.DataFrame) -> pd.DataFrame:
    """把回补的 PIT 财务数据接上：按 ann_date 做 merge_asof（只用已公告的报告）。

    这是**唯一正确**的用法：`ann_date <= trade_date`，绝不能用 `end_date`（那是报告期，公告前不可知）。
    """
    path = ROOT / "assets/data/meta/cn_financial_indicators.parquet"
    if not path.exists():
        print(f"[pit] 未找到 {path}，跳过财务特征")
        return merged
    fin = pd.read_parquet(path)
    fin = fin[fin["ann_date"].notna()].copy()
    fin["ann_date"] = pd.to_datetime(fin["ann_date"])
    fin = fin.rename(columns={"ts_code": "stock_code"})
    keep_src = sorted(set(FINANCIAL_PIT_MAP.values()))
    fin = fin[["stock_code", "ann_date"] + keep_src].dropna(subset=keep_src, how="all")
    fin = fin.sort_values(["ann_date", "stock_code"]).drop_duplicates(["stock_code", "ann_date"], keep="last")
    # merge_asof 要求左右键 dtype 完全一致（left 来自 pandas 对象列、right 来自 Arrow 字符串列）
    fin["stock_code"] = fin["stock_code"].astype(str)
    fin["ann_date"] = pd.to_datetime(fin["ann_date"])
    left = merged.copy()
    left["stock_code"] = left["stock_code"].astype(str)
    left["trade_date"] = pd.to_datetime(left["trade_date"])
    # merge_asof 要求两侧都按 `on` 键（日期）排序
    left = left.sort_values(["trade_date", "stock_code"])
    fin = fin.sort_values(["ann_date", "stock_code"])
    out = pd.merge_asof(
        left, fin,
        left_on="trade_date", right_on="ann_date", by="stock_code",
        direction="backward", allow_exact_matches=True,
    )
    # 重命名成 clean panel 的列名，供 SLEEVE_SPECS.features 直接匹配
    rename = {v: k for k, v in FINANCIAL_PIT_MAP.items()}
    out = out.rename(columns=rename)
    out["financial_pit_age_days"] = (out["trade_date"] - out["ann_date"]).dt.days
    covered = out[[c for c in rename.values() if c in out.columns]].notna().any(axis=1).mean()
    print(f"[pit] 财务特征已接入：覆盖 {covered:.1%} 的样本行（按 ann_date 回看，公告后才可见）", flush=True)
    return out


def build_dataset(spec, args) -> pd.DataFrame:
    bars = load_bars(args.start, args.end)
    if getattr(args, "external_features", None):
        features = load_external_features(Path(args.external_features), args.start, args.end)
    else:
        features = load_features(spec, args.start, args.end)
    merged = bars.merge(features, on=["trade_date", "stock_code"], how="inner")
    merged = merged.sort_values(["stock_code", "trade_date"])
    h = args.horizon
    grp = merged.groupby("stock_code", sort=False)
    merged["entry_px"] = grp["open"].shift(-1)
    merged["exit_px"] = grp["close"].shift(-(1 + h))
    merged["prev_close"] = grp["close"].shift(1)
    merged["fwd"] = merged["exit_px"] / merged["entry_px"] - 1.0

    # 可交易域
    liquid = merged["amount"] >= args.min_amount
    has_entry = merged["entry_px"].notna() & (merged["entry_px"] > 0)
    limit_up = (merged["high"] == merged["low"]) & (merged["entry_px"] >= merged["prev_close"] * 1.095)
    merged["tradable"] = liquid & has_entry & (~limit_up.fillna(False))
    merged.loc[~merged["tradable"], "fwd"] = np.nan

    industry = load_industry()
    if not industry.empty:
        merged = merged.merge(industry, on="stock_code", how="left")
    if "industry_l1" not in merged.columns:
        merged["industry_l1"] = "__ALL__"
    # 关键修复（2026-10-06）：退市股不在 registry 里 → industry_l1 为 NaN，
    # 若直接 groupby([trade_date, industry_l1])，NaN 键会把整组丢掉、transform 返回 NaN，
    # 于是 fwd 被整列污染成 NaN，退市股被**静默排除**在训练之外（幸存者偏差又回来了）。
    merged["industry_l1"] = merged["industry_l1"].fillna("__UNKNOWN__")
    merged["fwd"] = merged["fwd"] - merged.groupby(["trade_date", "industry_l1"])["fwd"].transform("mean")

    def rank_transform(group: pd.Series) -> pd.Series:
        valid = group.dropna()
        if len(valid) < 20:
            return pd.Series(np.nan, index=group.index)
        ranks = valid.rank(pct=True)
        return ((ranks - 0.5) * 2).reindex(group.index)

    merged["label"] = merged.groupby("trade_date")["fwd"].transform(rank_transform)
    if getattr(args, "financial_pit", False):
        merged = attach_financial_pit(merged)
    return merged


def newey_west_t(series: pd.Series, lag: int) -> float:
    """IC 序列的 Newey–West t 值（滞后 lag 期）。"""
    x = series.dropna().to_numpy()
    n = len(x)
    if n < 10:
        return float("nan")
    mean = x.mean()
    demeaned = x - mean
    gamma0 = float((demeaned ** 2).sum() / n)
    var = gamma0
    for k in range(1, min(lag, n - 1) + 1):
        w = 1 - k / (lag + 1)
        gamma = float((demeaned[k:] * demeaned[:-k]).sum() / n)
        var += 2 * w * gamma
    se = np.sqrt(max(var, 1e-12) / n)
    return float(mean / se)


def main() -> int:
    parser = argparse.ArgumentParser(description="按设计规格训练单个 sleeve")
    parser.add_argument("--sleeve", default="model_valuation")
    parser.add_argument("--start", default="2025-03-20")
    parser.add_argument("--end", default="2026-09-30")
    parser.add_argument("--horizon", type=int, default=0, help="0 = 用规格里的 horizon")
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--min-amount", type=float, default=2e7)
    parser.add_argument("--out", type=Path, default=ROOT / "output/research")
    parser.add_argument("--financial-pit", action="store_true",
                        help="接入回补的 PIT 财务数据（ann_date 回看），供质量/成长类 sleeve 使用")
    parser.add_argument("--external-features", type=Path, default=None,
                        help="外部特征表（parquet，键列 trade_date/stock_code，其余数值列作特征）；用于长样本复验")
    parser.add_argument("--exclude-features", default="",
                        help="逗号分隔：从外部特征表中排除的列（如 size 暴露、空覆盖列）")
    parser.add_argument("--feature-cols", default="",
                        help="逗号分隔：显式指定使用的外部特征列（优先于 --exclude-features）")
    args = parser.parse_args()

    if args.sleeve not in SLEEVE_SPECS:
        print(f"未知 sleeve：{args.sleeve}；可选 {list(SLEEVE_SPECS)}")
        return 2
    spec = SLEEVE_SPECS[args.sleeve]
    args.horizon = args.horizon or spec.label.horizon

    import lightgbm as lgb

    dataset = build_dataset(spec, args)
    usable = dataset[dataset["label"].notna()].copy()
    # 外部特征表模式：直接用表内数值列（可用 --exclude-features 剔除）
    if getattr(args, "external_features", None):
        excluded = {c.strip() for c in (args.exclude_features or "").split(",") if c.strip()}
        explicit = [c.strip() for c in (args.feature_cols or "").split(",") if c.strip()]
        if explicit:
            feature_cols = [c for c in explicit if c in usable.columns and pd.api.types.is_numeric_dtype(usable[c])]
            missing = [c for c in explicit if c not in usable.columns]
            if missing:
                print(f"[warn] 指定的特征列不存在，已跳过：{missing}", flush=True)
        else:
            feature_cols = [c for c in usable.columns
                            if c not in ("trade_date", "stock_code", "open", "high", "low", "close", "amount",
                                         "entry_px", "exit_px", "prev_close", "fwd", "label", "tradable",
                                         "industry_l1", "ann_date")
                            and c not in excluded
                            and pd.api.types.is_numeric_dtype(usable[c])]
    # 否则特征清单严格来自 SLEEVE_SPECS。优先级：
    #   --financial-pit 打开时优先用**回补的 PIT 版本**（`<feat>`，覆盖 99%），否则用 clean panel 版本
    #   （`<feat>_clean`，财报类只有 19% 覆盖）；两者都缺则跳过。
    else:
        feature_cols = []
        for feat in spec.features:
            order = (feat, f"{feat}_clean") if args.financial_pit else (f"{feat}_clean", feat)
            for candidate in order:
                if candidate in usable.columns:
                    feature_cols.append(candidate)
                    break
        for extra in ("missing_count", "financial_pit_age_days"):
            if extra in usable.columns:
                feature_cols.append(extra)
    print(f"[{spec.sleeve_id}] 样本 {len(usable):,} 行 / {usable['stock_code'].nunique()} 只 / "
          f"{usable['trade_date'].nunique()} 个交易日 | 特征 {len(feature_cols)} 个 | horizon {args.horizon}d", flush=True)

    splits = purge_embargo_split(usable["trade_date"], spec, n_folds=args.folds)
    params = {k: v for k, v in spec.lgb_params.items() if k not in ("early_stopping", "n_estimators")}
    n_estimators = int(spec.lgb_params.get("n_estimators", 400))

    all_preds, fold_metrics = [], []
    for fold, (train_dates, test_dates) in enumerate(splits, start=1):
        train = usable[usable["trade_date"].isin(train_dates)]
        test = usable[usable["trade_date"].isin(test_dates)]
        if len(train) < 5000 or len(test) < 500:
            continue
        model = lgb.LGBMRegressor(n_estimators=n_estimators, **params, verbose=-1)
        model.fit(train[feature_cols], train["label"],
                  sample_weight=None if "missing_count" not in train else None)
        pred = model.predict(test[feature_cols])
        test = test.assign(pred=pred)
        all_preds.append(test[["trade_date", "stock_code", "label", "pred", "fwd", "industry_l1"]])

        ic = test.groupby("trade_date").apply(
            lambda g: g["pred"].corr(g["label"], method="spearman") if len(g) > 20 else np.nan,
            include_groups=False).dropna()
        topk = test.groupby("trade_date").apply(
            lambda g: g.nlargest(args.top_k, "pred")["fwd"].mean() - g["fwd"].mean(), include_groups=False).dropna()
        decile = test.groupby("trade_date").apply(
            lambda g: (g.nlargest(max(1, len(g) // 10), "pred")["fwd"].mean()
                       - g.nsmallest(max(1, len(g) // 10), "pred")["fwd"].mean()) if len(g) > 50 else np.nan,
            include_groups=False).dropna()
        fold_metrics.append({
            "fold": fold, "train_days": len(train_dates), "test_days": len(test_dates),
            "train_rows": int(len(train)), "test_rows": int(len(test)),
            "ic_mean": float(ic.mean()), "ic_std": float(ic.std()),
            "icir": float(ic.mean() / ic.std()) if ic.std() > 0 else np.nan,
            "nw_t": newey_west_t(ic, lag=args.horizon),
            "topk_excess_mean": float(topk.mean()),
            "decile_spread_mean": float(decile.mean()),
        })
        print(f"[{spec.sleeve_id}] fold{fold}: IC {fold_metrics[-1]['ic_mean']:+.4f} "
              f"ICIR {fold_metrics[-1]['icir']:+.3f} NW-t {fold_metrics[-1]['nw_t']:+.2f} "
              f"top{args.top_k}超额 {fold_metrics[-1]['topk_excess_mean']:+.3%} "
              f"十分位价差 {fold_metrics[-1]['decile_spread_mean']:+.3%}", flush=True)

    if not fold_metrics:
        print("没有可用 fold")
        return 1
    metrics = pd.DataFrame(fold_metrics)
    pooled = pd.concat(all_preds, ignore_index=True)
    pooled_ic = pooled.groupby("trade_date").apply(
        lambda g: g["pred"].corr(g["label"], method="spearman") if len(g) > 20 else np.nan,
        include_groups=False).dropna()
    pooled_topk = pooled.groupby("trade_date").apply(
        lambda g: g.nlargest(args.top_k, "pred")["fwd"].mean() - g["fwd"].mean(), include_groups=False).dropna()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = args.out / f"sleeve_train_{spec.sleeve_id}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 生产需要给**最新截面**打分，因此除了 OOS 预测外还要落盘模型本身：
    #   output/model_scores/sleeve_<id>.txt（LightGBM booster）+ meta.json（特征清单/标签期/训练窗口/指标）
    model_dir = ROOT / "output/model_scores"
    model_dir.mkdir(parents=True, exist_ok=True)
    final_model = lgb.LGBMRegressor(n_estimators=n_estimators, **params, verbose=-1)
    final_model.fit(usable[feature_cols], usable["label"])
    model_path = model_dir / f"sleeve_{spec.sleeve_id.replace('model_', '')}.txt"
    final_model.booster_.save_model(str(model_path))
    (model_dir / f"sleeve_{spec.sleeve_id.replace('model_', '')}.meta.json").write_text(
        json.dumps({
            "sleeve_id": spec.sleeve_id, "horizon": args.horizon, "feature_cols": feature_cols,
            "train_rows": int(len(usable)), "train_days": int(usable["trade_date"].nunique()),
            "train_window": [str(usable["trade_date"].min().date()), str(usable["trade_date"].max().date())],
            "lgb_params": spec.lgb_params, "n_estimators": n_estimators,
            "pooled_ic": float(pooled_ic.mean()),
            "pooled_icir": float(pooled_ic.mean() / pooled_ic.std()) if pooled_ic.std() > 0 else None,
            "pooled_nw_t": newey_west_t(pooled_ic, lag=args.horizon),
            "trained_at": datetime.now().isoformat(timespec="seconds"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[{spec.sleeve_id}] 模型已落盘：{model_path}", flush=True)
    try:
        out_rel = str(out_dir.resolve().relative_to(ROOT))
    except ValueError:
        out_rel = str(out_dir)
    # 试验台账（设计规格 §4.3）：每次训练都登记，供 DSR 的试验次数校正与事后审计
    ledger = args.out / "sleeve_trials.csv"
    row = {
        "logged_at": datetime.now().isoformat(timespec="seconds"),
        "sleeve": spec.sleeve_id, "horizon": args.horizon, "folds": len(metrics),
        "financial_pit": bool(getattr(args, "financial_pit", False)),
        "n_features": len(feature_cols), "features": "|".join(feature_cols),
        "rows": int(len(usable)), "days": int(usable["trade_date"].nunique()),
        "pooled_ic": float(pooled_ic.mean()), "pooled_icir": float(pooled_ic.mean() / pooled_ic.std()) if pooled_ic.std() > 0 else None,
        "pooled_nw_t": newey_west_t(pooled_ic, lag=args.horizon),
        "pooled_topk_excess": float(pooled_topk.mean()),
        "folds_ic_positive": int((metrics["ic_mean"] > 0).sum()),
        "out_dir": out_rel,
    }
    pd.DataFrame([row]).to_csv(ledger, mode="a", header=not ledger.exists(), index=False)
    metrics.to_csv(out_dir / "fold_metrics.csv", index=False)
    pooled.to_parquet(out_dir / "oos_predictions.parquet", index=False)
    (out_dir / "summary.json").write_text(json.dumps({
        "sleeve": spec.sleeve_id, "horizon": args.horizon, "folds": len(metrics),
        "feature_cols": feature_cols, "rows": int(len(usable)),
        "pooled_ic_mean": float(pooled_ic.mean()), "pooled_ic_std": float(pooled_ic.std()),
        "pooled_icir": float(pooled_ic.mean() / pooled_ic.std()) if pooled_ic.std() > 0 else None,
        "pooled_nw_t": newey_west_t(pooled_ic, lag=args.horizon),
        "pooled_topk_excess_mean": float(pooled_topk.mean()),
        "fold_metrics": fold_metrics,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    positive = int((metrics["ic_mean"] > 0).sum())
    lines = [f"# Sleeve 训练报告：{spec.sleeve_id}", "",
             f"- 样本：{len(usable):,} 行 / {usable['stock_code'].nunique()} 只 / {usable['trade_date'].nunique()} 个交易日",
             f"- 特征 {len(feature_cols)} 个：{', '.join(feature_cols)}",
             f"- 标签：{args.horizon} 日域内 rank（行业中性，次日开盘进场）；Purged K-Fold({args.folds}) + "
             f"purge={spec.label.purge} + embargo={spec.label.embargo}", "",
             "| fold | 训练日 | 测试日 | IC | ICIR | NW t | top-k 超额 | 十分位价差 |",
             "|---|---|---|---|---|---|---|---|"]
    for r in metrics.itertuples():
        lines.append(f"| {r.fold} | {r.train_days} | {r.test_days} | {r.ic_mean:+.4f} | {r.icir:+.3f} | "
                     f"{r.nw_t:+.2f} | {r.topk_excess_mean:+.3%} | {r.decile_spread_mean:+.3%} |")
    lines += ["", f"**汇总（pooled OOS）**：IC {pooled_ic.mean():+.4f}、ICIR "
                  f"{(pooled_ic.mean()/pooled_ic.std() if pooled_ic.std()>0 else float('nan')):+.3f}、"
                  f"NW t {newey_west_t(pooled_ic, lag=args.horizon):+.2f}、"
                  f"top-{args.top_k} 超额 {pooled_topk.mean():+.3%}",
              "", f"**门槛判定**：{positive}/{len(metrics)} 折 IC 为正"
                  f"（要求 ≥4/5）；NW t ≥ 1.5 {'✅' if newey_west_t(pooled_ic, lag=args.horizon) >= 1.5 else '❌'}",
              "", "## 口径说明", "",
              "1. 标签为域内 rank（等权域 = 当日可交易样本，成交额 ≥ %.0f 万）；行业中性用 registry 的 industry_l1；"
              % (args.min_amount / 1e4),
              "2. 未计交易成本（万 2.5 佣金 + 千 1 印花税 + 万 3 滑点）——top-k 超额是毛口径；",
              "3. 与 W2 随机基线对照时，需用同一批决策日与同一 horizon（`sleeve_random_baseline.py`）。",
              f"4. 输出目录：`{out_rel}`"]
    (out_dir / "SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)
    print(f"[sleeve-train] 输出 {out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
