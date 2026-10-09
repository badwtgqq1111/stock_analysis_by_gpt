#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把**长期可用的新数据**并进 sleeve 长面板，产出 v2（供 LLM 因子挖掘用）。

背景（2026-10-07 实测）：
  * v1 面板 `cn_sleeve_features_long.parquet`：2015-01~2026-09、3,325 只、25 个字段
    （量价 15 + 估值 4 + 财务 8），**没有市值分位、没有偿债/现金流/成长加速类字段**；
  * 其余数据源的**时间覆盖不足**，不能用于 2018+ 的月度评估，只登记为缺口：
      - 5min 分时 `raw/intraday_baostock`：2025-10-09 起（241 个交易日）
      - alpha zoo 宽特征 `feature/clean_feature_panel`（1,374 列）：2025-03-20 起
      - 资金流/筹码 `derived/cn_moneyflow_features`、`cn_capital_flow_locks_features`：2024-08-23 起
  * 因此本轮"补数据"补的是**有长期历史**的三块：
      1. `meta/cn_financial_indicators.parquet`（2012 起、PIT 按 ann_date）→ 12 个新财务字段
      2. `meta/cn_valuation_features_long.parquet` → `size_ind_pct`（行业内市值分位）、`ps_ind_pct`
      3. `meta/cn_stock_basic.parquet` → `listed_days`（上市天数）、`soe_flag`（国企/民企）
      + 额外派生 `fin_age_days`（财报新鲜度 = 交易日 − 公告日）

用法：
  uv run python scripts/panel_extend_v2.py                # 产出 v2 + manifest + 覆盖报告
  uv run python scripts/panel_extend_v2.py --start 2015-01-01
输出：
  assets/data/meta/cn_sleeve_features_long_v2.parquet
  assets/data/meta/cn_sleeve_features_long_v2.manifest.json （字段清单 + 覆盖率 + 缺口说明）
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
META = ROOT / "assets/data/meta"
V1 = META / "cn_sleeve_features_long.parquet"
FIN = META / "cn_financial_indicators.parquet"
VAL = META / "cn_valuation_features_long.parquet"
BASIC = META / "cn_stock_basic.parquet"
OUT = META / "cn_sleeve_features_long_v2.parquet"

# 财务表里 v1 已有的字段（不重复添加）
FIN_DUP = {"roe", "roa", "grossprofit_margin", "netprofit_margin", "debt_to_assets",
           "or_yoy", "netprofit_yoy", "ocfps"}


def main() -> int:
    ap = argparse.ArgumentParser(description="sleeve 长面板 v2：并入长期可用的新数据")
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    panel = pd.read_parquet(V1)
    panel["trade_date"] = pd.to_datetime(panel["trade_date"])
    panel = panel[panel["trade_date"] >= pd.Timestamp(args.start)].copy()
    v1_fields = [c for c in panel.columns if c not in ("trade_date", "stock_code")]
    print(f"[v2] v1 面板 {len(panel):,} 行 / {panel['stock_code'].nunique()} 只 / "
          f"{len(v1_fields)} 字段（{panel['trade_date'].min():%Y-%m-%d}~{panel['trade_date'].max():%Y-%m-%d}）", flush=True)
    coverage: dict[str, float] = {}

    # ---- 1) 财务指标（PIT：merge_asof 按 ann_date 取"最近一期已公告"） ----
    fin = pd.read_parquet(FIN)
    fin["ann_date"] = pd.to_datetime(fin["ann_date"])
    fin = fin.rename(columns={"ts_code": "stock_code"})
    new_fin = [c for c in fin.columns if c not in ("stock_code", "ann_date", "end_date") and c not in FIN_DUP]
    fin = (fin.dropna(subset=["ann_date"])
              .sort_values(["ann_date", "stock_code"])
              .drop_duplicates(["stock_code", "ann_date"], keep="last"))
    panel = panel.sort_values(["trade_date", "stock_code"])
    panel = pd.merge_asof(
        panel, fin[["stock_code", "ann_date"] + new_fin],
        left_on="trade_date", right_on="ann_date", by="stock_code", direction="backward",
    )
    panel["fin_age_days"] = (panel["trade_date"] - panel["ann_date"]).dt.days.astype("float64")
    panel = panel.drop(columns=["ann_date"])
    for c in new_fin + ["fin_age_days"]:
        coverage[c] = float(panel[c].notna().mean())
    print(f"[v2] 财务并入 {len(new_fin)} 列（PIT by ann_date）+ fin_age_days；"
          f"最新财报平均滞后 {panel['fin_age_days'].mean():.0f} 天", flush=True)

    # ---- 2) 估值长表：市值/市销分位 ----
    val_cols = ["trade_date", "stock_code", "size_ind_pct", "ps_ind_pct"]
    val = pd.read_parquet(VAL, columns=val_cols)
    val["trade_date"] = pd.to_datetime(val["trade_date"])
    val = val.drop_duplicates(["trade_date", "stock_code"], keep="last")
    panel = panel.merge(val, on=["trade_date", "stock_code"], how="left")
    for c in ("size_ind_pct", "ps_ind_pct"):
        coverage[c] = float(panel[c].notna().mean())
    # ps_ind_pct 在上游估值长表里全为空（实测覆盖 0.0%）→ 直接丢弃并登记为缺口，
    # 免得给 LLM 一个恒为空的字段（会浪费候选名额，也可能诱发"NaN 也算信号"的错觉）。
    empty_cols = [c for c in ("ps_ind_pct",) if coverage.get(c, 0.0) < 0.01]
    if empty_cols:
        panel = panel.drop(columns=empty_cols)
        for c in empty_cols:
            coverage.pop(c, None)
        print(f"[v2] 丢弃空列 {empty_cols}（上游覆盖 <1%，登记为数据缺口）", flush=True)
    print(f"[v2] 估值并入 size_ind_pct / ps_ind_pct；覆盖 "
          f"{coverage['size_ind_pct']:.1%}（ps_ind_pct 见上）", flush=True)

    # ---- 3) 基础信息：上市天数 + 国企/民企 ----
    basic = pd.read_parquet(BASIC)
    basic = basic.rename(columns={"ts_code": "stock_code"})
    basic["list_date"] = pd.to_datetime(basic["list_date"], errors="coerce")
    soe_tokens = ("国企", "国资", "央企", "地方国", "中央国")
    basic["soe_flag"] = basic["act_ent_type"].fillna("").map(
        lambda s: 1.0 if any(t in str(s) for t in soe_tokens) else 0.0)
    b = basic[["stock_code", "list_date", "soe_flag"]].drop_duplicates("stock_code")
    panel = panel.merge(b, on="stock_code", how="left")
    panel["listed_days"] = (panel["trade_date"] - panel["list_date"]).dt.days.astype("float64")
    panel = panel.drop(columns=["list_date"])
    for c in ("listed_days", "soe_flag"):
        coverage[c] = float(panel[c].notna().mean())
    print(f"[v2] 基础信息并入 listed_days / soe_flag；覆盖 "
          f"{coverage['listed_days']:.1%} / {coverage['soe_flag']:.1%}", flush=True)

    # ---- 4) 写出 ----
    out = Path(args.out)
    panel = panel.sort_values(["trade_date", "stock_code"]).reset_index(drop=True)
    panel.to_parquet(out, index=False)
    fields = [c for c in panel.columns if c not in ("trade_date", "stock_code")]
    new_fields = [c for c in fields if c not in v1_fields]
    manifest = {
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "bin": str(out),
        "rows": int(len(panel)),
        "stocks": int(panel["stock_code"].nunique()),
        "date_range": [str(panel["trade_date"].min().date()), str(panel["trade_date"].max().date())],
        "n_fields": len(fields),
        "fields_v1": v1_fields,
        "fields_new": new_fields,
        "coverage": {k: round(v, 4) for k, v in coverage.items()},
        "sources": {"v1": str(V1), "financial": str(FIN), "valuation": str(VAL), "basic": str(BASIC)},
        "data_gaps_not_used": {
            "intraday_5min": {"path": "assets/data/raw/intraday_baostock", "from": "2025-10-09",
                              "note": "仅 241 个交易日；baostock 分钟线可回溯 2015，但按实测速率约 0.13 只/秒 → 全市场回补约 7 小时，需单独排期"},
            "alpha_zoo_wide": {"path": "assets/data/feature/clean_feature_panel", "cols": 1374,
                               "from": "2025-03-20", "note": "1,374 列已有 PIT/清洗标记，但只覆盖 1.5 年；要用于长样本需重算到 2018"},
            "moneyflow_chips": {"path": "assets/data/derived/cn_moneyflow_features.parquet", "from": "2024-08-23",
                                "note": "资金流/筹码 61~213 列，覆盖 ~2 年，暂不入长面板"},
            "ps_ind_pct": {"path": "assets/data/meta/cn_valuation_features_long.parquet",
                           "note": "市销率分位在上游长表里 100% 为空（industry_missing 标记），需要原始 ps/市值数据才能算"},
            "size_ind_pct_coverage": {"note": "行业内市值分位实测覆盖 35.7%（行业分类齐全才有值），"
                                             "与既有 pe_ind_pct(15~34%) 同量级；缺原始总市值列，无法自算更全的分位"},
            "delisted_daily": {"path": "assets/data/meta/cn_delisted_daily.parquet",
                               "note": "退市股日线（生存偏差修正）；要并入需为其重算 25 列特征，属数据质量工单"},
        },
    }
    (out.with_suffix(".manifest.json")).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[v2] 写出 {out}：{len(panel):,} 行 / {len(fields)} 字段（新增 {len(new_fields)}）", flush=True)
    print(f"[v2] 新增字段：{new_fields}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
