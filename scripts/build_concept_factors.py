#!/usr/bin/env python3
"""把概念信号做成「个股-日」因子，供 ML / 排序使用。

每个交易日、每只股票，取它所属概念里最强的那个（以及均值）：
  concept_heat     最强概念的热度（横截面 z 加权：涨幅/广度/成交额占比/涨停密度）
  concept_rise     最强概念的热度加速度（heat_t − 前5日均值）
  concept_breadth  最强概念的广度（上涨成员占比）
  concept_ret5     最强概念的 5 日涨幅
  concept_rank     最强概念当日热度分位（0~1）
  concept_family   最强概念所属 family（字符串，供分组/去重）
  n_concepts       该股票当天所属概念数

注意：概念成员是**当前快照**（新浪 + iFinD），历史回测有幸存者/前视偏差。

输出：assets/data/derived/cn_concept_factors.parquet
"""
from __future__ import annotations

import argparse
import glob
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets/data/derived/cn_concept_factors.parquet"
FAMILY = {"军工航天": "军工", "国防军工": "军工", "军民融合": "军工", "大飞机": "军工",
          "人形机器人": "机器人", "机器人概念": "机器人", "具身智能": "机器人", "智能机器": "机器人",
          "CPO": "光通信", "光模块": "光通信", "存储芯片": "半导体", "华为海思": "半导体",
          "固态电池": "新能源", "锂电池": "新能源", "光伏": "新能源"}
NON_THEME = ("基金重仓", "社保重仓", "QFII", "券商重仓", "保险重仓", "融资融券", "深股通", "沪股通",
             "MSCI", "标普", "富时", "转债标的", "预盈预增", "参股金融", "参股券商", "破净股",
             "次新股", "超大盘", "大盘", "中盘", "小盘", "基金", "重仓", "央企50", "国企50",
             "上证50", "沪深300", "中证100", "行业龙头")


def members() -> pd.DataFrame:
    frames = []
    m = pd.read_parquet(ROOT / "assets/data/meta/cn_board_membership.parquet")
    m = m[m.taxonomy == "sina_concept"][["board", "code"]].rename(columns={"board": "concept"})
    m["stock_code"] = m["code"].map(lambda x: x + (".SH" if str(x).startswith(("6", "9")) else ".SZ"))
    frames.append(m[["concept", "stock_code"]])
    f = ROOT / "assets/data/derived/cn_concept_members_ifind.parquet"
    if f.exists():
        d = pd.read_parquet(f)
        frames.append(d[d.snapshot_date == d.snapshot_date.max()][["concept", "stock_code"]])
    out = pd.concat(frames, ignore_index=True).drop_duplicates()
    out = out[~out["concept"].astype(str).str.contains("|".join(NON_THEME), na=False)]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2020-01-01")
    args = ap.parse_args()
    files = sorted(glob.glob(str(ROOT / "assets/data/clean/ohlcv/market=CN/exchange=*/"
                                    "asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet")))
    bars = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "close", "amount"]).to_pandas()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"]).dt.normalize()
    bars = bars[bars["trade_date"] >= pd.Timestamp(args.start)]
    bars = bars.drop_duplicates(["trade_date", "stock_code"], keep="last").sort_values(["stock_code", "trade_date"])
    g = bars.groupby("stock_code", sort=False)
    bars["ret"] = g["close"].pct_change()

    mem = members()
    panel = bars.merge(mem, on="stock_code", how="inner")
    c = panel.groupby(["concept", "trade_date"]).agg(
        chg=("ret", "mean"), breadth=("ret", lambda s: float((s > 0).mean())),
        n=("ret", "size"), amount=("amount", "sum"),
        limit_ratio=("ret", lambda s: float((s >= 0.098).mean()))).reset_index()
    c = c[c["n"] >= 10].copy()
    c["amount_share"] = c["amount"] / c.groupby("trade_date")["amount"].transform("sum")
    zs = lambda s: (s - s.mean()) / (s.std(ddof=0) + 1e-9)
    for col in ("chg", "breadth", "amount_share", "limit_ratio"):
        c[f"z_{col}"] = c.groupby("trade_date")[col].transform(zs)
    c["heat"] = 0.40 * c["z_chg"] + 0.30 * c["z_breadth"] + 0.20 * c["z_amount_share"] + 0.10 * c["z_limit_ratio"]
    c = c.sort_values(["concept", "trade_date"])
    c["ret5"] = c.groupby("concept")["chg"].transform(lambda s: (1 + s).rolling(5, min_periods=3).apply(np.prod, raw=True) - 1)
    c["heat_ma5_prev"] = c.groupby("concept")["heat"].transform(lambda s: s.shift(1).rolling(5, min_periods=3).mean())
    c["rise"] = c["heat"] - c["heat_ma5_prev"]
    c["rank_pct"] = c.groupby("trade_date")["heat"].rank(pct=True)
    c["family"] = c["concept"].map(lambda x: FAMILY.get(x, x))

    merged = panel[["trade_date", "stock_code", "concept"]].merge(
        c[["trade_date", "concept", "heat", "rise", "breadth", "ret5", "rank_pct", "family"]],
        on=["trade_date", "concept"], how="left")
    gb = merged.groupby(["trade_date", "stock_code"])
    out = pd.DataFrame({
        "concept_heat": gb["heat"].max(),
        "concept_rise": gb["rise"].max(),
        "concept_breadth": gb["breadth"].max(),
        "concept_ret5": gb["ret5"].max(),
        "concept_rank": gb["rank_pct"].max(),
        "n_concepts": gb["concept"].nunique(),
    }).reset_index()
    # family：取 heat 最大的那一行
    top_family = (merged.sort_values("heat", ascending=False)
                        .drop_duplicates(["trade_date", "stock_code"])[["trade_date", "stock_code", "family"]])
    out = out.merge(top_family, on=["trade_date", "stock_code"], how="left")
    out = out.rename(columns={"family": "concept_family"})
    out["concept_heat"] = out["concept_heat"].astype("float32")
    out["concept_rise"] = out["concept_rise"].astype("float32")
    out["concept_breadth"] = out["concept_breadth"].astype("float32")
    out["concept_ret5"] = out["concept_ret5"].astype("float32")
    out["concept_rank"] = out["concept_rank"].astype("float32")
    out.to_parquet(OUT, index=False)
    print(f"[concept-factor] {len(out):,} 行 {out.trade_date.min().date()} ~ {out.trade_date.max().date()}")
    print(out[["concept_heat", "concept_rise", "concept_breadth", "concept_ret5", "concept_rank"]].describe().round(3))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
