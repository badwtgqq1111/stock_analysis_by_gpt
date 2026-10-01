#!/usr/bin/env python
"""「低位反弹确认」状态的事件研究：事件密度 + 状态边缘 + 子集内可排序性。

回答的问题是："单独训一个路径标签模型" vs "做一个信号函数（规则）"哪个更合适。
判据：
  1) 事件密度：每天有多少个事件（决定模型有没有足够样本）；
  2) 状态边缘：事件发生后的 T+1 开盘、5/10/20 日**市场相对**超额是否为正（决定有没有东西可抓）；
  3) 子集内可排序性：在状态子集里，现有模型的横截面 IC 是多少（决定模型能不能加值）。

定义（仓库已有 factor_engine/ml/strategy_labels.py）：
  startup_price_eligible : dist_from_120d_low∈[0.05,0.30] 且 return_60d≤35% 且 dist_from_60d_high≤−5%
  bottom_rebound_candidate: drawdown_60d≤−20% 且 close>ma20 且 return_1d>0
输出：output/research/rebound_event_study/{summary.csv, by_year.csv, REPORT.md}
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from data.ingest.service import MarketDataService  # noqa: E402
from factor_engine.ml.strategy_labels import build_cn_strategy_labels  # noqa: E402

OUT = ROOT / "output/research/rebound_event_study"
OOS = ROOT / "output/oos_predictions/cn_lightgbm_oos_predictions.csv"
STATES = ("startup_price_eligible", "bottom_rebound_candidate")
HORIZONS = (5, 10, 20)


def tstat(series: pd.Series, overlap: int = 1) -> float:
    v = series.dropna().to_numpy()
    if len(v) < 5 or v.std() == 0:
        return float("nan")
    return float(v.mean() / v.std() * np.sqrt(len(v)) / np.sqrt(max(overlap, 1)))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    svc = MarketDataService(base_dir=str(ROOT / "assets/data"))
    end = pd.Timestamp("2026-09-30")
    start = end - pd.Timedelta(days=800)
    bars = svc.warehouse.read_ohlcv(market="CN", asset_type="equity", frequency="daily", adjust="qfq",
                                    start_date=start.strftime("%Y-%m-%d"), end_date=end.strftime("%Y-%m-%d"),
                                    columns=["stock_code", "trade_date", "open", "high", "low", "close", "volume"])
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    print(f"[bars] {len(bars):,} 行 / {bars.stock_code.nunique()} 只 / "
          f"{bars.trade_date.min().date()}~{bars.trade_date.max().date()}", flush=True)
    labels = build_cn_strategy_labels(bars, path_horizon=60, entry_delay=1, exec_horizons=HORIZONS,
                                      startup_low_window=120)
    labels["trade_date"] = pd.to_datetime(labels["trade_date"])
    print(f"[labels] {len(labels):,} 行；列 {[c for c in labels.columns if c.startswith(('startup','bottom','forward_excess'))][:8]}", flush=True)

    rows, yearly = [], []
    for state in STATES:
        if state not in labels.columns:
            continue
        sub = labels[labels[state].fillna(False).astype(bool)]
        per_day = labels.groupby("trade_date")[state].sum()
        active_days = int((per_day > 0).sum())
        row = {"state": state, "events": int(len(sub)), "event_days": active_days,
               "events_per_active_day": round(float(per_day[per_day > 0].mean()), 1) if active_days else 0.0,
               "events_per_day": round(float(per_day.mean()), 1)}
        base = {"events": int(len(labels))}
        for horizon in HORIZONS:
            column = f"forward_excess_return_{horizon}d"
            if column not in sub.columns:
                continue
            row[f"excess_{horizon}d_%"] = round(float(sub[column].mean()) * 100, 3)
            row[f"median_{horizon}d_%"] = round(float(sub[column].median()) * 100, 3)
            row[f"t_{horizon}d"] = round(tstat(sub[column], overlap=horizon), 2)
            row[f"hit_{horizon}d"] = round(float((sub[column] > 0).mean()), 3)
            all_events = labels[column].dropna()
            row[f"market_mean_{horizon}d_%"] = round(float(all_events.mean()) * 100, 3)
        rows.append(row)
        by_year = (sub.assign(year=sub["trade_date"].dt.year)
                   .groupby("year")
                   .agg(events=("stock_code", "size"),
                        excess_5d=("forward_excess_return_5d", "mean"),
                        excess_20d=("forward_excess_return_20d", "mean"),
                        hit_20d=("forward_excess_return_20d", lambda s: float((s > 0).mean()))))
        by_year["state"] = state
        yearly.append(by_year.reset_index())
    summary = pd.DataFrame(rows)
    summary.to_csv(OUT / "summary.csv", index=False)
    yearly_frame = pd.concat(yearly, ignore_index=True) if yearly else pd.DataFrame()
    yearly_frame.to_csv(OUT / "by_year.csv", index=False)
    print("\n" + summary.to_string(index=False), flush=True)

    # 子集内可排序性：现有 OOS 模型分在状态子集里的 IC
    extra = ""
    if OOS.is_file() and "startup_price_eligible" in labels.columns:
        oos = pd.read_csv(OOS, parse_dates=["trade_date"]).dropna(subset=["forward_return_20d", "model_score_raw"])
        merged = oos.merge(labels[["stock_code", "trade_date", "startup_price_eligible", "bottom_rebound_candidate"]],
                           on=["stock_code", "trade_date"], how="inner")
        for state in STATES:
            if state not in merged.columns:
                continue
            sub = merged[merged[state].fillna(False).astype(bool)]
            ics = [float(g["model_score_raw"].corr(g["forward_return_20d"], method="spearman"))
                   for _, g in sub.groupby("trade_date") if len(g) >= 30]
            all_ics = [float(g["model_score_raw"].corr(g["forward_return_20d"], method="spearman"))
                       for _, g in merged.groupby("trade_date") if len(g) >= 30]
            if ics:
                extra += (f"\n- 子集内模型 IC（{state}）：{np.mean(ics):+.4f}（{len(ics)} 个交易日）；"
                          f"全截面 IC {np.mean(all_ics):+.4f}；子集日均可排序标的 "
                          f"{float(sub.groupby('trade_date').size().mean()):.0f} 只")
    (OUT / "REPORT.md").write_text(
        "# 「低位反弹确认」事件研究\n\n"
        f"样本：{bars.trade_date.min().date()}~{bars.trade_date.max().date()}，{bars.stock_code.nunique()} 只；"
        "收益口径 = T+1 开盘进场、市场等权相对超额（仓库 strategy_labels 自带）。\n\n"
        + summary.to_markdown(index=False) + "\n\n## 逐年\n\n" + yearly_frame.round(4).to_markdown(index=False)
        + "\n\n## 子集内可排序性\n" + (extra or "（缺 OOS 打分或列）") + "\n", encoding="utf-8")
    print(extra, flush=True)
    print(f"\n[done] {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
