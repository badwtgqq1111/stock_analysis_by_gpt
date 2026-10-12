#!/usr/bin/env python3
"""条件 IC / 交互诊断（P2.46-5）：模型在**哪一种市场状态**下才有信息量？

动机：`P2.48 §6/§7` 两条负结果（lambdarank 更差、模型直出低于池前 10）都指向"模型信号本身"，
所以先回答"什么时候它有信息量"，再谈改模型。做法（全部用已有产物，不训练）：
  1. 读 OOS 预测（`model_score` + 前向收益标签）；
  2. 基准（中证500）算**市场状态**：20 日动量（趋势）× 20 日已实现波动（分三档）；
  3. 每个预测日算两个信息量指标：横截面 **秩 IC**（全截面）与 **top-10 组均值 − 全截面均值**（我们真正交易的那一档）；
  4. 按状态分组统计（均值 / 中位 / 胜率 / 样本数），并给出"哪一档可用"的判据。

用法：uv run python scripts/factor_state_ic.py --predictions output/oos_predictions_ab_baseline/cn_lightgbm_oos_predictions.csv --out output/research/model_arm_ab/state_ic_baseline.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--predictions", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--top-n", type=int, default=10)
    ap.add_argument("--label", default="forward_return_20d")
    args = ap.parse_args(argv)

    frame = pd.read_csv(args.predictions)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    if args.label not in frame.columns:
        raise SystemExit(f"预测文件里没有标签列 {args.label}")
    bench = pd.read_parquet(ROOT / "assets/data/meta/cn_benchmark_daily.parquet")
    bench = bench[bench.ts_code == "000905.SH"].copy()
    bench["trade_date"] = pd.to_datetime(bench["trade_date"])
    bench = bench.set_index("trade_date").close.sort_index()
    state = pd.DataFrame({
        "mom20": bench.pct_change(20),
        "vol20": bench.pct_change().rolling(20).std() * np.sqrt(252),
    }).dropna()

    rows = []
    for day, group in frame.groupby("trade_date"):
        if len(group) < 50 or day not in state.index:
            continue
        ic = group["model_score"].corr(group[args.label], method="spearman")
        top = group.nlargest(args.top_n, "model_score")[args.label].mean()
        rows.append({"date": day, "ic": ic, "top_gap": top - group[args.label].mean(),
                     "mom20": float(state.loc[day, "mom20"]), "vol20": float(state.loc[day, "vol20"]),
                     "cross_mean": float(group[args.label].mean())})
    table = pd.DataFrame(rows)
    if table.empty:
        raise SystemExit("没有可用的预测日")
    table["trend"] = np.where(table["mom20"] > 0, "上行", "下行")
    table["vol_bin"] = pd.qcut(table["vol20"], 3, labels=["低波动", "中波动", "高波动"])

    def stat(sub: pd.DataFrame) -> dict:
        return {"n_dates": int(len(sub)), "ic_mean": round(float(sub["ic"].mean()), 4),
                "ic_median": round(float(sub["ic"].median()), 4),
                "ic_win": round(float((sub["ic"] > 0).mean()), 3),
                "top_gap_mean": round(float(sub["top_gap"].mean()), 4),
                "top_gap_win": round(float((sub["top_gap"] > 0).mean()), 3)}

    overall = stat(table)
    by_vol = {str(key): stat(sub) for key, sub in table.groupby("vol_bin", observed=True)}
    by_trend = {str(key): stat(sub) for key, sub in table.groupby("trend")}
    cells = {f"{t}|{v}": stat(sub) for (t, v), sub in table.groupby(["trend", "vol_bin"], observed=True)}
    payload = {"predictions": str(args.predictions), "top_n": args.top_n, "n_dates": int(len(table)),
               "window": [str(table["date"].min().date()), str(table["date"].max().date())],
               "overall": overall, "by_vol": by_vol, "by_trend": by_trend, "cells": cells,
               "dates": [{"date": str(r["date"].date()), "ic": round(float(r["ic"]), 4),
                          "top_gap": round(float(r["top_gap"]), 4), "trend": r["trend"],
                          "vol_bin": str(r["vol_bin"])} for _, r in table.iterrows()]}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"[state-ic] {payload['window'][0]}~{payload['window'][1]} ｜ {payload['n_dates']} 个预测日")
    print(f"[state-ic] 全样本：IC 均值 {overall['ic_mean']:+.4f}（中位 {overall['ic_median']:+.4f}，IC>0 占比 {overall['ic_win']:.0%}）"
          f"｜top{args.top_n} 超额均值 {overall['top_gap_mean']:+.4f}（胜率 {overall['top_gap_win']:.0%}）")
    print(f"{'状态':<16}{'n':>4}{'IC均值':>10}{'IC中位':>10}{'IC>0':>8}{'top超额':>10}{'top胜率':>9}")
    for key, st in {**{f"波动:{k}": v for k, v in by_vol.items()}, **{f"趋势:{k}": v for k, v in by_trend.items()}}.items():
        print(f"{key:<16}{st['n_dates']:>4}{st['ic_mean']:>10.4f}{st['ic_median']:>10.4f}{st['ic_win']:>8.0%}"
              f"{st['top_gap_mean']:>10.4f}{st['top_gap_win']:>9.0%}")
    for key, st in cells.items():
        print(f"  {key:<16}{st['n_dates']:>4}{st['ic_mean']:>10.4f}{st['ic_median']:>10.4f}{st['ic_win']:>8.0%}"
              f"{st['top_gap_mean']:>10.4f}{st['top_gap_win']:>9.0%}")
    print(f"[state-ic] -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
