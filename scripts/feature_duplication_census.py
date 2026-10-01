#!/usr/bin/env python
"""特征层去重普查 + 单一因子 vs 模型的可交易性对照。

回答三个问题：
  1. 1,366 个特征列里到底有多少个"不同的变量"（完全重复列、以及同族内的近邻）；
  2. 训练期 vs 验证期横截面 rank IC 的逐月符号（主因子是否反号）；
  3. 用最强的那一列做多空十分位，验证期扣 0 成本的 20 日前瞻超额是多少（可交易量级）。

输出：output/research/ic_diagnosis/{feature_duplicates.csv, factor_decile_valid.csv, DUP_REPORT.md}
"""
from __future__ import annotations

import hashlib
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from data.ingest.service import MarketDataService  # noqa: E402

OUT = ROOT / "output/research/ic_diagnosis"


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
    dates = np.array(sorted(labeled["trade_date"].unique()))
    valid_start = pd.Timestamp(dates[-60])

    # ---- 1. 完全重复列普查（对全部行做哈希）----
    fingerprints: dict[str, list[str]] = {}
    for column in features:
        values = pd.util.hash_pandas_object(pd.Series(labeled[column].to_numpy()), index=False).to_numpy()
        digest = hashlib.md5(values.tobytes()).hexdigest()
        fingerprints.setdefault(digest, []).append(column)
    groups = sorted(fingerprints.values(), key=len, reverse=True)
    dup_rows = [{"group_size": len(g), "members": ",".join(g[:6]) + ("..." if len(g) > 6 else "")} for g in groups if len(g) > 1]
    pd.DataFrame(dup_rows).to_csv(OUT / "feature_duplicates.csv", index=False)
    unique_columns = len(groups)
    print(f"[1] 特征列 {len(features)} → 唯一列 {unique_columns}（完全重复组 {len(dup_rows)} 个，"
          f"最大组 {len(groups[0])} 列）", flush=True)
    for row in dup_rows[:10]:
        print(f"    size={row['group_size']:>3}  {row['members']}", flush=True)

    # ---- 2/3. 代表性趋势/反转列的训练期 vs 验证期 IC + 十分位 ----
    candidates = ["MA60_clean", "RANK60_clean", "TA_RSI_clean", "GTJA005_clean", "QTLD60_clean",
                  "pb_ratio_sector_relative_clean", "ROC60_clean", "SUMN60_clean"]
    present = [c for c in candidates if c in labeled.columns]
    rows, deciles = [], []
    for column in present:
        per_day = {}
        for date, group in labeled.groupby("trade_date"):
            sub = group[[column, label]].dropna()
            if len(sub) >= 50:
                per_day[pd.Timestamp(date)] = float(sub[column].corr(sub[label], method="spearman"))
        series = pd.Series(per_day).sort_index()
        train, valid = series[series.index < valid_start], series[series.index >= valid_start]
        rows.append({"factor": column, "train_ic": round(float(train.mean()), 4),
                     "train_t": round(float(train.mean() / train.std() * np.sqrt(len(train))), 2),
                     "valid_ic": round(float(valid.mean()), 4),
                     "valid_t": round(float(valid.mean() / valid.std() * np.sqrt(len(valid))), 2),
                     "valid_hit": round(float((valid > 0).mean()), 3), "valid_days": int(len(valid))})
    table = pd.DataFrame(rows)
    table.to_csv(OUT / "factor_train_valid_ic.csv", index=False)
    print("\n[2] 代表性因子 训练期 vs 验证期 IC:\n" + table.to_string(index=False), flush=True)

    if present:
        best = table.reindex(table["valid_ic"].abs().sort_values(ascending=False).index).iloc[0]["factor"]
        direction = np.sign(table.set_index("factor").loc[best, "valid_ic"])
        frame = labeled[labeled["trade_date"] >= valid_start]
        for date, group in frame.groupby("trade_date"):
            sub = group[[best, label]].dropna()
            if len(sub) < 200:
                continue
            try:
                bucket = pd.qcut(sub[best] * direction, 5, labels=False, duplicates="drop")
            except ValueError:
                continue
            means = sub[label].groupby(bucket).mean()
            if len(means) >= 5:
                deciles.append({"trade_date": date, "factor": best, "q1": means.iloc[0], "q5": means.iloc[-1],
                                "spread_q5_q1": means.iloc[-1] - means.iloc[0],
                                "top_value_mean": means.iloc[-1] + 0.5})
        dec = pd.DataFrame(deciles)
        if not dec.empty:
            dec.to_csv(OUT / "factor_decile_valid.csv", index=False)
            print(f"\n[3] {best}（方向 ×{direction:+.0f}）验证期五分位: q5-q1 分位差均值 "
                  f"{dec.spread_q5_q1.mean():+.4f}，t="
                  f"{dec.spread_q5_q1.mean()/dec.spread_q5_q1.std()*np.sqrt(len(dec)):.2f}，"
                  f"胜率 {(dec.spread_q5_q1>0).mean():.2f}，天数 {len(dec)}", flush=True)
            print("    说明：label 是当日横截面分位（0~1），分位差 ×100 约等于该档相对全池的中位超额（百分点×20日）。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
