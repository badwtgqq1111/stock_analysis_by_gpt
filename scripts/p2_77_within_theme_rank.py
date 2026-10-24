#!/usr/bin/env python3
"""P2.77-B：先选主题，再在主题内用 B4 分数排序（两阶段）。

对照：
  global_top20  = 全市场 B4 预测 Top20
  theme_top20   = 先取 top3 主题（热度最强、5日≤15%、广度≥30%），再在这些主题成员里按 B4 取 Top20
指标：每折平均 20 日超额、命中率、主题内 RankIC。
"""
from __future__ import annotations

import glob
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output/research/p2_77"
FAMILY = {"军工航天": "军工", "国防军工": "军工", "军民融合": "军工", "大飞机": "军工",
          "人形机器人": "机器人", "机器人概念": "机器人", "具身智能": "机器人", "智能机器": "机器人"}
NON_THEME = ("基金重仓", "社保重仓", "QFII", "融资融券", "深股通", "沪股通", "MSCI", "转债标的",
             "预盈预增", "参股金融", "参股券商", "次新股", "超大盘", "大盘", "中盘", "小盘",
             "基金", "重仓", "央企50", "国企50", "上证50", "沪深300", "中证100", "行业龙头")


def concept_table(start: str) -> pd.DataFrame:
    files = sorted(glob.glob(str(ROOT / "assets/data/clean/ohlcv/market=CN/exchange=*/"
                                    "asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet")))
    bars = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "close", "amount"]).to_pandas()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"]).dt.normalize()
    bars = bars[bars["trade_date"] >= pd.Timestamp(start)].drop_duplicates(["trade_date", "stock_code"], keep="last")
    bars = bars.sort_values(["stock_code", "trade_date"])
    bars["ret"] = bars.groupby("stock_code", sort=False)["close"].pct_change()
    m = pd.read_parquet(ROOT / "assets/data/meta/cn_board_membership.parquet")
    m = m[m.taxonomy == "sina_concept"][["board", "code"]].rename(columns={"board": "concept"})
    m["stock_code"] = m["code"].map(lambda x: x + (".SH" if str(x).startswith(("6", "9")) else ".SZ"))
    m = m[~m["concept"].astype(str).str.contains("|".join(NON_THEME), na=False)]
    panel = bars.merge(m[["concept", "stock_code"]], on="stock_code")
    c = panel.groupby(["concept", "trade_date"]).agg(
        chg=("ret", "mean"), breadth=("ret", lambda s: float((s > 0).mean())),
        n=("ret", "size"), amount=("amount", "sum")).reset_index()
    c = c[c["n"] >= 30].copy()
    c["amount_share"] = c["amount"] / c.groupby("trade_date")["amount"].transform("sum")
    zs = lambda s: (s - s.mean()) / (s.std(ddof=0) + 1e-9)
    c["heat"] = (0.45 * c.groupby("trade_date")["chg"].transform(zs)
                 + 0.35 * c.groupby("trade_date")["breadth"].transform(zs)
                 + 0.20 * c.groupby("trade_date")["amount_share"].transform(zs))
    c = c.sort_values(["concept", "trade_date"])
    c["ret5"] = c.groupby("concept")["chg"].transform(lambda s: (1 + s).rolling(5, min_periods=3).apply(np.prod, raw=True) - 1)
    c["family"] = c["concept"].map(lambda x: FAMILY.get(x, x))
    return c


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    c = concept_table("2024-01-01")
    elig = c[(c["breadth"] >= 0.30) & (c["ret5"] <= 0.15)]
    top = (elig.sort_values(["trade_date", "heat"], ascending=[True, False])
               .drop_duplicates(["trade_date", "family"]).groupby("trade_date").head(3))
    theme_map = top[["trade_date", "concept"]]

    rows = []
    for fold in ["F1_2023", "F2_2024", "F3_2025_26"]:
        p = pd.read_parquet(ROOT / f"output/research/p2_81/predictions/{fold}_B4a_current100.parquet")
        p["trade_date"] = pd.to_datetime(p["trade_date"])
        # 股票 → 当天所属概念（可能多个）
        m = pd.read_parquet(ROOT / "assets/data/meta/cn_board_membership.parquet")
        m = m[m.taxonomy == "sina_concept"][["board", "code"]].rename(columns={"board": "concept"})
        m["stock_code"] = m["code"].map(lambda x: x + (".SH" if str(x).startswith(("6", "9")) else ".SZ"))
        pm = p.merge(m[["concept", "stock_code"]], on="stock_code", how="left")
        pm = pm.merge(theme_map.rename(columns={"concept": "theme"}), left_on=["trade_date", "concept"],
                      right_on=["trade_date", "theme"], how="inner")
        pm["rk"] = pm.groupby("trade_date")["pred"].rank(ascending=False)
        global_top = p[p.groupby("trade_date")["pred"].rank(ascending=False) <= 20]
        theme_top = pm.sort_values(["trade_date", "pred"], ascending=[True, False]).groupby("trade_date").head(20)
        rows.append({
            "fold": fold,
            "global_excess": round(float(global_top["fwd_excess_20"].mean()) * 100, 3),
            "theme_excess": round(float(theme_top["fwd_excess_20"].mean()) * 100, 3),
            "global_hit": round(float((global_top["fwd_excess_20"] > 0).mean()) * 100, 1),
            "theme_hit": round(float((theme_top["fwd_excess_20"] > 0).mean()) * 100, 1),
            "theme_share": round(len(theme_top) / max(len(global_top), 1), 2),
        })
    df = pd.DataFrame(rows)
    df.to_csv(OUT / "within_theme_rank.csv", index=False)
    print(df.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
