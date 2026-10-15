#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""股东结构因子的横截面 IC 检验（PIT）：文献说的"户数下降=筹码集中=超额"，在 A 股成立吗？

文献（`docs/reference/主力追踪文献综述.md` 收录）普遍主张：
  * **股东户数下降**（筹码集中）→ 未来超额为正；
  * **十大流通股东持股比例上升**（主力控盘）→ 未来超额为正；
  * **机构持仓占比上升** → 未来超额为正。
本脚本用刚补齐的股东数据（`cn_shareholder_features.parquet`，带 `ann_date`）做**可复现**的检验：

口径（每一步都能核对）：
  1. **PIT 对齐**：特征的 `ann_date` 公告后**第一个交易日**才可用（`available_from`）；
  2. 前向收益：从 `available_from` 收盘算未来 20 个交易日的 qfq 收益（`fwd20`）；
  3. 截面 IC：按 `available_from` 分组算 Spearman 秩相关（同一天披露的股票互相比较），
     再把"日度 IC 序列"汇总成 均值 IC / ICIR / NW t（Newey-West，lag=5）/ 正号占比；
  4. 因子方向统一成"值越大越看多"：户数变化取 **负号**（下降=看多）；比例类取**环比变化**（上升=看多）。

用法：`uv run python scripts/shareholder_factor_ic.py --print`
产物：`output/research/shareholders/factor_ic.json` + `.md`
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "scripts")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import wyckoff_study as ws  # noqa: E402

FEATS = ROOT / "assets/data/derived/cn_shareholder_features.parquet"
OUT = ROOT / "output/research/shareholders"
HORIZON = 20
MIN_NAMES = 30          # 一个截面至少这么多只才算一次 IC


def build_panel(horizon: int = HORIZON) -> pd.DataFrame:
    bars = ws.load_bars("2023-01-01")
    universe = ws._universe()
    if universe:
        bars = bars[bars["stock_code"].isin(universe)]
    feats = pd.read_parquet(FEATS)
    feats["ann_date"] = feats["ann_date"].astype(str)
    feats["end_date"] = feats["end_date"].astype(str)
    feats = feats[feats["ann_date"] >= "20230101"]

    # 环比：十大流通股东集中度与机构占比（同一只票上一期）
    for col, new in (("top10_float_ratio", "top10_float_ratio_chg"),
                     ("inst_holder_ratio", "inst_holder_ratio_chg")):
        if col in feats.columns:
            feats = feats.sort_values(["ts_code", "end_date"])
            feats[new] = feats.groupby("ts_code")[col].diff()
    # 户数下降 = 看多 ⇒ 取负号
    feats["holder_num_chg_inv"] = -pd.to_numeric(feats.get("holder_num_chg"), errors="coerce")

    days = pd.DatetimeIndex(sorted(bars["trade_date"].unique()))
    close = bars.set_index(["stock_code", "trade_date"])["close"].to_dict()

    rows = []
    for _, r in feats.iterrows():
        code = r["ts_code"]
        ann = pd.Timestamp(r["ann_date"])
        later = days[days >= ann]
        if len(later) <= horizon:
            continue
        entry_day, exit_day = later[0], later[horizon]
        p0, p1 = close.get((code, entry_day)), close.get((code, exit_day))
        if not p0 or not p1 or p0 <= 0:
            continue
        rows.append({"ts_code": code, "available_from": entry_day.date(), "ann_date": r["ann_date"],
                     "fwd20": float(p1) / float(p0) - 1,
                     **{k: r.get(k) for k in ("holder_num_chg_inv", "holder_num_chg",
                                              "top10_float_ratio_chg", "top10_float_ratio",
                                              "inst_holder_ratio_chg", "float_holder_new")}})
    return pd.DataFrame(rows)


def nw_t(series: pd.Series, lag: int = 5) -> float | None:
    x = pd.to_numeric(series, errors="coerce").dropna()
    if len(x) < 10:
        return None
    mean = x.mean()
    dev = x - mean
    var = float((dev ** 2).mean())
    for k in range(1, lag + 1):
        cov = float((dev.iloc[k:].to_numpy() * dev.iloc[:-k].to_numpy()).mean())
        var += 2 * (1 - k / (lag + 1)) * cov
    se = np.sqrt(max(var, 1e-12) / len(x))
    return float(mean / se) if se > 0 else None


def ic_report(panel: pd.DataFrame, factor: str) -> dict:
    sub = panel.dropna(subset=[factor, "fwd20"])
    ics = []
    for day, group in sub.groupby("available_from"):
        if len(group) < MIN_NAMES:
            continue
        ic = group[factor].corr(group["fwd20"], method="spearman")
        if pd.notna(ic):
            ics.append(float(ic))
    if not ics:
        return {"factor": factor, "n_cross_sections": 0, "reason": "截面不足"}
    s = pd.Series(ics)
    return {"factor": factor, "n_cross_sections": len(s), "n_obs": int(len(sub)),
            "ic_mean": round(float(s.mean()), 4), "ic_std": round(float(s.std()), 4),
            "icir": round(float(s.mean() / s.std()), 3) if s.std() > 0 else None,
            "nw_t": (round(nw_t(s), 2) if nw_t(s) is not None else None),
            "positive_ratio": round(float((s > 0).mean()), 3),
            "verdict": ("有方向性证据（|t|≥3）" if (nw_t(s) or 0) >= 3 else
                        "弱证据（|t|∈[1.5,3)）" if abs(nw_t(s) or 0) >= 1.5 else "无显著证据")}


def main() -> int:
    ap = argparse.ArgumentParser(description="股东结构因子 IC 检验（PIT）")
    ap.add_argument("--horizon", type=int, default=HORIZON)
    ap.add_argument("--print", action="store_true")
    args = ap.parse_args()
    panel = build_panel(args.horizon)
    if panel.empty:
        print("[shareholder-ic] 面板为空（先跑 scripts/backfill_cn_shareholders.py）")
        return 0
    factors = [f for f in ("holder_num_chg_inv", "top10_float_ratio_chg", "inst_holder_ratio_chg",
                           "top10_float_ratio", "float_holder_new") if f in panel.columns]
    reports = [ic_report(panel, f) for f in factors]
    OUT.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
               "horizon": args.horizon, "panel_rows": int(len(panel)),
               "window": [str(panel["available_from"].min()), str(panel["available_from"].max())],
               "method": ("PIT：ann_date 公告后第一个交易日可用；前向 %d 日 qfq 收益；按可用日做截面 Spearman IC；"
                          "NW t（lag=5）；户数变化已取负号（下降=看多）" % args.horizon),
               "factors": reports}
    (OUT / "factor_ic.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [f"# 股东结构因子 IC 检验（PIT，前向 {args.horizon} 日）", "",
             f"面板 {len(panel):,} 行｜窗口 {payload['window'][0]} → {payload['window'][1]}", "",
             payload["method"], "",
             "| 因子 | 截面数 | 样本 | 均值 IC | ICIR | NW t | 正号占比 | 结论 |", "|---|---|---|---|---|---|---|---|"]
    for r in reports:
        lines.append(f"| `{r['factor']}` | {r.get('n_cross_sections')} | {r.get('n_obs')} | {r.get('ic_mean')} | "
                     f"{r.get('icir')} | {r.get('nw_t')} | {r.get('positive_ratio')} | {r.get('verdict','')} |")
    (OUT / "factor_ic.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if args.print:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
