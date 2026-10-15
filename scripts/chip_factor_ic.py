#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""筹码分布因子的横截面 IC 检验（PIT）："筹码集中/获利盘"能不能当主力追踪参考？

用户（2026-10-14）："筹码分布 有作为主力追踪参考之一么？"
—— 有（陈浩《筹码分布》一脉 + 券商筹码结构研报），但**要分别验方向**：
    * 获利盘比例高：可能是"主力控盘"（看多）也可能是"人人浮盈要兑现"（看空），文献本身两种说法都有；
    * 筹码集中度（宽度收窄）：经典口径是**主力吸筹完成**（看多）；
    * 现价 vs 平均成本：高于成本太多=浮盈盘多，短期有兑现压力。
本脚本用 tushare `cyq_perf`（获利盘比例 + 成本分位）做 PIT IC 检验，把方向交给数据，而不是口号。

口径：筹码数据当日收盘后可得 ⇒ `available_from` = 下一交易日；前向 20 日 qfq 收益；
按 available_from 做截面 Spearman IC；NW t（lag=5，复用 `shareholder_factor_ic` 的实现）。
产物：`output/research/chips/factor_ic.{json,md}`（含与"股东户数"因子的相关性=正交性检查）。
"""
from __future__ import annotations

import argparse
import importlib.util
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

FEATS = ROOT / "assets/data/derived/cn_chip_features.parquet"
SHARE_FEATS = ROOT / "assets/data/derived/cn_shareholder_features.parquet"
OUT = ROOT / "output/research/chips"
HORIZON = 20
MIN_NAMES = 30


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_panel(horizon: int = HORIZON) -> pd.DataFrame:
    """向量化面板：筹码特征（available_from=次交易日）× 前向 horizon 日收益。

    第一版用 `.iterrows()` 逐行去 bars 的 dict 里查价（370 万行 × 2 次查找）——实测跑 10 分钟没出结果。
    改成 merge + `shift(-horizon)`：等价（available_from 一定是交易日；前向收益 = 该日收盘 → 20 个交易日后收盘）。
    """
    bars = ws.load_bars("2024-01-01")
    universe = ws._universe()
    if universe:
        bars = bars[bars["stock_code"].isin(universe)]
    bars = bars.sort_values(["stock_code", "trade_date"]).copy()
    bars["fwd_close"] = bars.groupby("stock_code", sort=False)["close"].shift(-horizon)
    bars = bars[["stock_code", "trade_date", "close", "fwd_close"]].rename(
        columns={"stock_code": "ts_code", "close": "px0"})
    feats = pd.read_parquet(FEATS)
    feats["available_from"] = pd.to_datetime(feats["available_from"])
    merged = feats.merge(bars, left_on=["ts_code", "available_from"], right_on=["ts_code", "trade_date"], how="inner")
    merged = merged[merged["px0"] > 0]
    merged["fwd20"] = merged["fwd_close"] / merged["px0"] - 1
    merged = merged.dropna(subset=["fwd20"])
    keep = ["ts_code", "available_from", "fwd20"] + [c for c in feats.columns if c.startswith("chip_")]
    out = merged[keep].copy()
    out["available_from"] = out["available_from"].dt.date
    return out


def attach_holder_num(panel: pd.DataFrame) -> pd.DataFrame:
    """把「股东户数下降」因子按**公告日 ≤ 可用日**并进来（merge_asof，正交性检查用）。

    第一版对每一行 panel 做一次 DataFrame 过滤（O(n²)）——同样实测跑不动；这里用 merge_asof 一次对齐。
    """
    if not SHARE_FEATS.exists() or panel.empty:
        return panel
    share = pd.read_parquet(SHARE_FEATS)[["ts_code", "ann_date", "holder_num_chg"]].copy()
    share["ann_date"] = pd.to_datetime(share["ann_date"].astype(str))
    share["holder_num_chg_inv"] = -pd.to_numeric(share["holder_num_chg"], errors="coerce")
    share["ann_date"] = share["ann_date"].astype("datetime64[ns]")
    share = share.dropna(subset=["ann_date"]).sort_values("ann_date")
    left = panel.copy()
    # 两边的时间键必须是**同一精度**（左是 date → 秒精度，右是 us；实测报 MergeError）
    left["avail_ts"] = pd.to_datetime(left["available_from"]).astype("datetime64[ns]")
    left = left.sort_values("avail_ts")
    out = pd.merge_asof(left, share[["ts_code", "ann_date", "holder_num_chg_inv"]].sort_values("ann_date"),
                        left_on="avail_ts", right_on="ann_date", by="ts_code", direction="backward")
    return out.drop(columns=["avail_ts", "ann_date"])


def main() -> int:
    ap = argparse.ArgumentParser(description="筹码分布因子 IC 检验（PIT）")
    ap.add_argument("--horizon", type=int, default=HORIZON)
    ap.add_argument("--print", action="store_true")
    args = ap.parse_args()
    if not FEATS.exists():
        print("[chip-ic] 缺 cn_chip_features.parquet（先跑 scripts/backfill_cn_cyq.py）")
        return 0
    share_mod = _load("shareholder_ic_under_test", "scripts/shareholder_factor_ic.py")
    panel = attach_holder_num(build_panel(args.horizon))
    if panel.empty:
        print("[chip-ic] 面板为空")
        return 0
    factors = [c for c in panel.columns if c.startswith("chip_")]
    reports = [share_mod.ic_report(panel, f) for f in factors]
    reports.append(share_mod.ic_report(panel, "holder_num_chg_inv"))
    # 正交性：筹码因子与户数因子的截面相关性（同一天同一批股票）
    ortho = {}
    if "holder_num_chg_inv" in panel.columns:
        for f in factors:
            sub = panel.dropna(subset=[f, "holder_num_chg_inv"])
            if len(sub) < 500:
                continue
            ortho[f] = round(float(sub[f].corr(sub["holder_num_chg_inv"], method="spearman")), 3)
    OUT.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": pd.Timestamp.now().isoformat(timespec="seconds"), "horizon": args.horizon,
               "panel_rows": int(len(panel)), "window": [str(panel["available_from"].min()), str(panel["available_from"].max())],
               "method": ("筹码（tushare cyq_perf）当日收盘后可得 ⇒ available_from=次交易日；前向 %d 日 qfq；"
                          "截面 Spearman IC；NW t(lag=5)；(20 日变化)口径为与自身 20 日前之差" % args.horizon),
               "factors": reports, "orthogonality_vs_holder_num": ortho,
               "data": "assets/data/derived/cn_chip_features.parquet（tushare cyq_perf 全市场按日补）"}
    (OUT / "factor_ic.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [f"# 筹码分布因子 IC 检验（PIT，前向 {args.horizon} 日）", "",
             f"面板 {len(panel):,} 行｜窗口 {payload['window'][0]} → {payload['window'][1]}", "",
             payload["method"], "",
             "| 因子 | 截面 | 样本 | 均值 IC | ICIR | NW t | 正号占比 | 结论 |", "|---|---|---|---|---|---|---|---|"]
    for r in reports:
        lines.append(f"| `{r['factor']}` | {r.get('n_cross_sections')} | {r.get('n_obs')} | {r.get('ic_mean')} | "
                     f"{r.get('icir')} | {r.get('nw_t')} | {r.get('positive_ratio')} | {r.get('verdict','')} |")
    if ortho:
        lines += ["", "## 与「股东户数下降」因子的截面相关性（正交性）", "",
                  "| 筹码因子 | Spearman ρ |", "|---|---|"]
        for k, v in ortho.items():
            lines.append(f"| `{k}` | {v} |")
    (OUT / "factor_ic.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if args.print:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
