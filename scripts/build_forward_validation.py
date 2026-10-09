#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把「选股台账」里**已经实现的**前瞻收益串成一条可看的前向记录（第三十二轮）。

解决的问题
----------
模拟盘（`output/paper_trading/account/`）只有 2026-09-09 起 10 个决策日 —— 因为最终名单
`cn_ensemble_selected.csv` 此前每天被覆盖、没有归档，更早的名单**永久丢失**。
但选股台账（`output/results_cn/selection_ledger.parquet`，由 scripts/build_selection_ledger.py 生成）
本来就把每天的候选池/最终名单连同**已实现的前瞻收益**（r1/r5/r10/r20、基准、超额）记下来了，
`preselected` 口径从 **2025-09-08** 就有 —— 20 个决策日、跨 13 个月。

所以这里做的是**另一条口径**，别和模拟盘混：
  * 模拟盘   = 最终名单 + 真实 T+1 开盘成交 + 成本 + 逐日盯市 → **账户净值**（样本短）
  * 前瞻记录 = 候选池等权 + 直接取已实现 r5/r1（不扣成本、不建模成交）→ **决策质量记录**（样本长）

口径全部写进输出的 metrics.json，前端也照实标。

用法:
  uv run python scripts/build_forward_validation.py
输出:
  output/results_cn/forward_validation/<source>_by_date.csv   每个决策日一行
  output/results_cn/forward_validation/<source>_metrics.json  口径 + 指标
  output/results_cn/forward_validation/summary.md             人读汇总
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "output/results_cn/selection_ledger.parquet"
BY_DATE = ROOT / "output/results_cn/selection_ledger_by_date.csv"
OUT_DIR = ROOT / "output/results_cn/forward_validation"
SOURCES = ("preselected", "notify", "arm", "survey_gate", "survey_raw")
CAVEAT = ("候选池等权、直接用已实现前瞻收益串联：不扣任何成本/滑点、不建模涨跌停与停牌、"
          "决策日间隔不固定（周频为主，少数更密），所以它是**决策质量的前瞻记录**，不是账户净值。"
          "账户口径见 output/paper_trading/account/（最终名单 + T+1 开盘成交 + 成本，但样本只有 2026-09 起）。")


def _chain(series: pd.Series) -> tuple[pd.Series, float, float]:
    """把每期等权收益串成净值曲线，返回 (曲线, 期末倍数, 最大回撤)。"""
    curve = (1.0 + series.fillna(0.0)).cumprod()
    peak = curve.cummax()
    dd = float((curve / peak - 1.0).min()) if len(curve) else 0.0
    return curve, float(curve.iloc[-1]) if len(curve) else float("nan"), dd


def build(source: str, ledger: pd.DataFrame, horizon: int) -> dict | None:
    frame = ledger[ledger["source"] == source].copy()
    col, bench = f"r{horizon}", f"bench_r{horizon}"
    if frame.empty or col not in frame.columns:
        return None
    frame["decision_date"] = pd.to_datetime(frame["decision_date"], errors="coerce")
    frame["ret"] = pd.to_numeric(frame[col], errors="coerce")
    frame["bench"] = pd.to_numeric(frame.get(bench), errors="coerce") if bench in frame.columns else np.nan
    usable = frame.dropna(subset=["decision_date", "ret"])
    if usable.empty:
        return None
    per_date = usable.groupby("decision_date").agg(
        n=("stock_code", "size"), ret=("ret", "mean"), bench=("bench", "mean")).reset_index()
    per_date["excess"] = per_date["ret"] - per_date["bench"]
    per_date = per_date.sort_values("decision_date")
    curve, total, dd = _chain(per_date["ret"])
    per_date["nav"] = curve.to_numpy()
    per_date["drawdown"] = (curve / curve.cummax() - 1.0).to_numpy()
    years = max((per_date["decision_date"].iloc[-1] - per_date["decision_date"].iloc[0]).days / 365.25, 1e-9)
    periods_per_year = len(per_date) / years
    metrics = {
        "source": source,
        "horizon_days": horizon,
        "decision_dates": int(len(per_date)),
        "window": [per_date["decision_date"].iloc[0].strftime("%Y-%m-%d"),
                   per_date["decision_date"].iloc[-1].strftime("%Y-%m-%d")],
        "span_years": round(years, 2),
        "picks_mean_per_date": round(float(per_date["n"].mean()), 1),
        "mean_period_return": round(float(per_date["ret"].mean()), 5),
        "mean_excess_return": round(float(per_date["excess"].mean()), 5),
        "median_period_return": round(float(per_date["ret"].median()), 5),
        "win_rate_periods": round(float((per_date["ret"] > 0).mean()), 4),
        "chained_total_return": round(total - 1.0, 4),
        "chained_total_return_bench": round(float((1 + per_date["bench"].fillna(0)).prod()) - 1.0, 4),
        "annualized_chained": round(float(total) ** (periods_per_year / max(len(per_date), 1)) - 1.0, 4)
        if total > 0 else None,
        "max_drawdown_chained": round(dd, 4),
        "caveats": CAVEAT,
        "costs_applied": False,
    }
    return {"per_date": per_date, "metrics": metrics}


def main() -> int:
    ap = argparse.ArgumentParser(description="前瞻收益记录（来自选股台账）")
    ap.add_argument("--horizon", type=int, default=5, help="串联用的前瞻期（默认 5 个交易日）")
    ap.add_argument("--out-dir", default=str(OUT_DIR.relative_to(ROOT)))
    args = ap.parse_args()
    out = ROOT / args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    ledger = pd.read_parquet(LEDGER)
    results, lines = {}, ["# 前瞻收益记录（从选股台账串出来）", "", f"串联期限：{args.horizon} 个交易日 ｜ 生成来源：{LEDGER.relative_to(ROOT)}", "",
                          "> " + CAVEAT, "", "| 口径 | 决策日 | 区间 | 每期等权收益 | 胜率 | 累计(不扣成本) | 基准累计 | 最大回撤 |",
                          "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for source in SOURCES:
        built = build(source, ledger, args.horizon)
        if not built:
            continue
        m = built["metrics"]
        built["per_date"].to_csv(out / f"{source}_by_date.csv", index=False)
        (out / f"{source}_metrics.json").write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
        results[source] = m
        lines.append(f"| {source} | {m['decision_dates']} | {m['window'][0]} ~ {m['window'][1]} | "
                     f"{m['mean_period_return']:+.2%} | {m['win_rate_periods']:.0%} | "
                     f"{m['chained_total_return']:+.1%} | {m['chained_total_return_bench']:+.1%} | "
                     f"{m['max_drawdown_chained']:.1%} |")
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "summary.json").write_text(json.dumps({"horizon_days": args.horizon, "sources": results},
                                                ensure_ascii=False, indent=2), encoding="utf-8")
    # 交叉核对：台账自带的 by_date 汇总（build_selection_ledger.py 的产物）应与这里一致
    if BY_DATE.exists() and "preselected" in results:
        ref = pd.read_csv(BY_DATE)
        ref = ref[(ref["source"] == "preselected")].dropna(subset=[f"mean_r{args.horizon}"])
        mine = results["preselected"]["mean_period_return"]
        their = float(pd.to_numeric(ref[f"mean_r{args.horizon}"], errors="coerce").mean())
        print(f"[核对] preselected 每期等权收益: 本脚本={mine:+.5f} 台账by_date={their:+.5f} "
              f"差={abs(mine - their):.2e}")
    print(json.dumps({k: {"决策日": v["decision_dates"], "区间": v["window"],
                          "每期等权": v["mean_period_return"], "胜率": v["win_rate_periods"],
                          "累计": v["chained_total_return"]} for k, v in results.items()},
                     ensure_ascii=False, indent=2))
    print(f"产物目录: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
