#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""行业 veto 日报：把「行业层面是反转」这件事用在**已有生产名单**上（只读、不改权重）。

背景（2026-10-10，用户要求"把行业反转优化好，直到能进入生产为止"）
------------------------------------------------------------------
`scripts/industry_reversal_lab.py` 已经给了两个结论：
  1. 把行业反转做成**独立策略**跑不赢可交易等权基准 —— 2016-2021 训练期表现最好的 3 个设计，
     在 2022-2026 验证期**全部转负**（0/3 通过），所以它不该作为独立手臂进生产；
  2. 但"行业动量 → 未来收益"在 2016-2026 全样本上是**显著负**的（1 月形成期 IC -0.067，
     NW t -2.90；五分位 Q5-Q1 = -1.04%/月）。

结论 2 的正确用法不是"选行业"，而是**否决**：把已经选出来的票里、落在"最近最强/最热行业"
的那些点名出来（它们的行业被证明是反向的），而不去动其余仓位。这跟仓库里
`pk_guard_report.py`（只读、不改生产权重）是同一类工具。

判定口径
--------
对台账里每个"有前瞻收益"的决策日，按来源分组：
  * 名单等权收益（现状）
  * 剔除 veto 票后的等权收益（veto 后）
  * 差 = veto 后 - 现状，做**逐期配对符号检验**（同 promotion_check.py 口径）
  * veto 规则：票所属行业在决策日的 20 日动量进入全行业**最强 10%**，或涨停密度进**最热 10%**

用法：
  uv run python scripts/industry_veto_report.py
输出：
  output/results_cn/industry_veto/{latest.json,report.md}
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "output/results_cn/selection_ledger.parquet"
PANEL = ROOT / "output/research/industry_rotation/industry_panel.parquet"
OUT_DIR = ROOT / "output/results_cn/industry_veto"

FORWARD = ["r5", "r20"]
VETO_QUANTILE = 0.90


def sign_test_p(diff: pd.Series) -> float:
    values = diff.dropna()
    n = len(values)
    if n == 0:
        return 1.0
    better = int((values > 0).sum())
    k = min(better, n - better)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2 ** n)
    return float(min(1.0, 2 * tail))


def industry_signals() -> pd.DataFrame:
    """行业面板 → 决策日可用的行业信号（20 日动量 / 涨停密度），只用当日及之前数据。"""
    panel = pd.read_parquet(PANEL)
    panel = panel.sort_values(["industry", "trade_date"]).copy()
    grouped = panel.groupby("industry", sort=False)
    panel["ind_mom20"] = grouped["ret_mean"].transform(
        lambda s: (1 + s.fillna(0)).rolling(20, min_periods=10).apply(np.prod, raw=True) - 1)
    panel["ind_zt20"] = grouped["zt_density"].transform(
        lambda s: s.rolling(20, min_periods=10).mean())
    return panel[["trade_date", "industry", "ind_mom20", "ind_zt20"]]


def main() -> int:
    ap = argparse.ArgumentParser(description="行业 veto 日报（只读，不改生产权重）")
    ap.add_argument("--quantile", type=float, default=VETO_QUANTILE)
    args = ap.parse_args()

    ledger = pd.read_parquet(LEDGER)
    ledger["decision_date"] = pd.to_datetime(ledger["decision_date"])
    signals = industry_signals()
    signals["trade_date"] = pd.to_datetime(signals["trade_date"])

    # 台账里的 industry 是**证监会/申万**口径（如「C29橡胶和塑料制品业」），
    # 行业面板用的是 cn_stock_basic.industry（如「橡胶」）—— 两套口径对不上，
    # 必须按**股票代码**回填面板行业，不能按行业名 join（第一版就是这么错的）。
    basics = pd.read_parquet(ROOT / "assets/data/meta/cn_stock_basic.parquet",
                             columns=["ts_code", "industry"])
    basics["ts_code"] = basics["ts_code"].astype(str)
    panel_industry = basics.drop_duplicates("ts_code").set_index("ts_code")["industry"].astype(str)
    ledger["panel_industry"] = ledger["stock_code"].astype(str).map(panel_industry)
    missing = int(ledger["panel_industry"].isna().sum())
    if missing:
        print(f"[industry-veto] 注意：{missing} 行没有面板行业（用台账行业兜底）")
    ledger["panel_industry"] = ledger["panel_industry"].fillna(ledger["industry"].astype(str))

    merged = ledger.merge(signals, left_on=["decision_date", "panel_industry"],
                          right_on=["trade_date", "industry"], how="left")

    # 逐决策日判定行业阈值（横截面分位）：只用该日全行业的信号
    day_cut = signals.groupby("trade_date").agg(
        mom_cut=("ind_mom20", lambda s: s.quantile(args.quantile)),
        zt_cut=("ind_zt20", lambda s: s.quantile(args.quantile)))
    merged = merged.merge(day_cut, left_on="decision_date", right_index=True, how="left")
    merged["veto_mom"] = merged["ind_mom20"] >= merged["mom_cut"]
    merged["veto_zt"] = merged["ind_zt20"] >= merged["zt_cut"]
    merged["veto"] = merged["veto_mom"].fillna(False) | merged["veto_zt"].fillna(False)

    rows = []
    for source, group in merged.groupby("source"):
        for horizon in FORWARD:
            if horizon not in group.columns:
                continue
            sub = group.dropna(subset=[horizon])
            if sub.empty:
                continue
            per_date = []
            for date, day in sub.groupby("decision_date"):
                kept = day[~day["veto"]]
                if kept.empty or day["veto"].sum() == 0:
                    continue
                per_date.append({
                    "date": str(pd.Timestamp(date).date()),
                    "n_all": int(len(day)), "n_veto": int(day["veto"].sum()),
                    "ret_all": float(day[horizon].mean()),
                    "ret_kept": float(kept[horizon].mean()),
                })
            if len(per_date) < 3:
                continue
            table = pd.DataFrame(per_date)
            diff = table["ret_kept"] - table["ret_all"]
            rows.append({
                "source": source, "horizon": horizon, "periods": int(len(table)),
                "n_all": int(table["n_all"].sum()), "n_veto": int(table["n_veto"].sum()),
                "ret_all_mean": float(table["ret_all"].mean()),
                "ret_kept_mean": float(table["ret_kept"].mean()),
                "diff_mean": float(diff.mean()),
                "diff_median": float(diff.median()),
                "better": int((diff > 0).sum()), "worse": int((diff < 0).sum()),
                "sign_p": sign_test_p(diff),
                "periods_detail": table.to_dict("records"),
            })

    rows.sort(key=lambda r: -(r["diff_mean"] or -9))
    payload = {"generated_at": datetime.now().isoformat(timespec="seconds"),
               "veto_quantile": args.quantile,
               "n_ledger_rows": int(len(ledger)),
               "n_veto_rows": int(merged["veto"].sum()),
               "rows": rows,
               "note": "只读日报：不改生产权重。veto = 该票所属行业在决策日的 20 日动量进入最强 "
                       f"{int((1 - args.quantile) * 100)}% 或涨停密度进入最热 "
                       f"{int((1 - args.quantile) * 100)}%。判据是逐期配对符号检验。"}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                         encoding="utf-8")

    lines = ["# 行业 veto 日报（只读，不改生产权重）", "",
             f"生成 {payload['generated_at']}｜台账 {len(ledger)} 行，其中被 veto 命中 "
             f"{payload['n_veto_rows']} 行（{payload['n_veto_rows'] / max(len(ledger), 1):.1%}）", "",
             f"veto 规则：所属行业在决策日的 20 日动量 ≥ 全行业 {args.quantile:.0%} 分位，"
             f"或涨停密度 ≥ {args.quantile:.0%} 分位。", "",
             "| 来源 | 前瞻 | 可比期数 | 名单均收益 | veto 后 | 差 | 更好/更差 | 符号检验 p |",
             "|---|---|---|---|---|---|---|---|"]
    for row in rows:
        lines.append("| {} | {} | {} | {:+.2%} | {:+.2%} | {:+.2%} | {}/{} | {:.3f} |".format(
            row["source"], row["horizon"], row["periods"], row["ret_all_mean"],
            row["ret_kept_mean"], row["diff_mean"], row["better"], row["worse"], row["sign_p"]))
    lines += ["", "> 只读工具：不改生产权重，与 `pk_guard_report.py` 同类。",
              "> 若某来源的 diff 长期为正且符号检验 p<0.10，才考虑把它变成一个正式过滤。", ""]
    (OUT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:24]))
    print(f"[industry-veto] 输出 {(OUT_DIR / 'report.md').relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
