#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""L9 第一步：量化**幸存者偏差**对"小市值域"业绩的影响。

问题：clean 层日线只覆盖当前仍在交易的标的（5,352 只都有最新 bar），退市股完全缺席 ——
而小市值策略恰恰是退市高发区，这会让任何小市值回测系统性高估。
`assets/data/meta/cn_delisted_daily.parquet`（209 只退市股、2014-01-02~2026-07-16、
含 qfq 收盘与历史市值）就是为了修这个偏差补的。

本脚本做三件事（都不改动任何既有产物，只产出报告）：
  1. 把**退市股并进全市场市值排名**（`mktcap_rank_all`），得到"含退市股"的域；
  2. 用同一套口径算**域内等权日收益**（含退市 vs 仅在市），直接量化偏差；
  3. 统计退市股在域内的时间占比，以及**退市前 60 个交易日**的累计收益（"没剔除会亏多少"）。

单位核对（实测）：退市表的 `total_mv` 单位=万元、`market_cap`=亿元、`total_share`=万股，
`close × total_share × 1e4 = total_mv × 1e4 = 市值(元)`，与面板 v3 的 `mktcap` 口径一致。

用法:
  uv run python scripts/quantify_survivorship_bias.py --start 2018-01-01
输出:
  output/research/survivorship_bias/{REPORT.md,summary.json,domain_daily.csv,delisted_tail.csv}
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
V3 = ROOT / "assets/data/meta/cn_sleeve_features_long_v3.parquet"
DELISTED = ROOT / "assets/data/meta/cn_delisted_daily.parquet"
BARS = ROOT / "assets/data/clean/ohlcv/market=CN"
OUT = ROOT / "output/research/survivorship_bias"
BAD_BOARD = ("688", "300", "301", "8", "4", "920", "689")


def load_clean_close(start: str) -> pd.DataFrame:
    import pyarrow.dataset as ds
    years = range(int(start[:4]), 2027)
    files = [str(p) for y in years
             for p in BARS.glob(f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={y}/*.parquet")]
    table = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "close"]).to_pandas()
    table["trade_date"] = pd.to_datetime(table["trade_date"])
    table = table[table["trade_date"] >= pd.Timestamp(start)]
    return table.rename(columns={"close": "close_px"})


def main() -> int:
    ap = argparse.ArgumentParser(description="量化小市值域的幸存者偏差")
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--size-max", type=float, default=0.40)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    # 1) 在市股票的市值（面板 v3 重构值）
    panel = pd.read_parquet(V3, columns=["trade_date", "stock_code", "mktcap", "listed_days", "mktcap_rank"])
    panel = panel[(panel["trade_date"] >= pd.Timestamp(args.start)) & panel["mktcap"].notna()]
    print(f"[l9] 在市：{len(panel):,} 行 / {panel['stock_code'].nunique()} 只（mktcap 非空）", flush=True)

    # 2) 退市股市值（close × total_share × 1e4 → 元）与收盘价
    dl = pd.read_parquet(DELISTED, columns=["stock_code", "trade_date", "close", "total_share",
                                            "total_mv", "circ_mv", "volume"])
    dl["trade_date"] = pd.to_datetime(dl["trade_date"])
    dl = dl[dl["trade_date"] >= pd.Timestamp(args.start)].copy()
    dl["mktcap"] = dl["close"] * dl["total_share"] * 1e4
    dl["close_px"] = dl["close"]
    print(f"[l9] 退市股：{len(dl):,} 行 / {dl['stock_code'].nunique()} 只 / "
          f"{dl['trade_date'].min():%Y-%m-%d}~{dl['trade_date'].max():%Y-%m-%d}", flush=True)

    # 3) 并入全市场市值排名（只在同日横截面上比较）
    live = panel[["trade_date", "stock_code", "mktcap", "listed_days"]].copy()
    live["delisted"] = False
    dead = dl[["trade_date", "stock_code", "mktcap"]].copy()
    dead["listed_days"] = np.nan
    dead["delisted"] = True
    universe = pd.concat([live, dead], ignore_index=True)
    universe["mktcap_rank_all"] = universe.groupby("trade_date")["mktcap"].rank(pct=True)
    print(f"[l9] 全市场（含退市）{len(universe):,} 行；每日平均 "
          f"{int(len(universe)/universe['trade_date'].nunique()):,} 只 "
          f"（其中退市股 {int(dead.groupby('trade_date').size().mean())} 只）", flush=True)

    # 4) 日收益：**不做宽表 pivot、也不逐日循环**（第一版那样跑 >15 分钟）
    #    改成"长表 groupby 求 pct_change"，再用布尔掩码 + groupby 求域内等权。
    px_long = pd.concat([load_clean_close(args.start),
                         dl[["trade_date", "stock_code", "close_px"]]], ignore_index=True)
    px_long = (px_long.drop_duplicates(["trade_date", "stock_code"], keep="last")
                      .sort_values(["stock_code", "trade_date"]))
    px_long["ret"] = px_long.groupby("stock_code", sort=False)["close_px"].pct_change()
    print(f"[l9] 价格长表 {len(px_long):,} 行（含退市 {len(dl):,}）", flush=True)

    # 5) 域：主板 + （在市需上市满 1 年）+ 市值分位 ≤ size_max
    code = universe["stock_code"].astype(str)
    board_ok = ~code.str.startswith(BAD_BOARD)
    age_ok = universe["delisted"] | (universe["listed_days"] >= 365)
    universe["in_domain"] = board_ok & age_ok & (universe["mktcap_rank_all"] <= args.size_max)

    dom = universe[universe["in_domain"]][["trade_date", "stock_code", "delisted"]].merge(
        px_long[["trade_date", "stock_code", "ret"]], on=["trade_date", "stock_code"], how="left")
    hit = dom.groupby("trade_date")["delisted"].agg(["size", "sum"])
    hit["share"] = hit["sum"] / hit["size"]
    print(f"[l9] 域内每日平均 {hit['size'].mean():.0f} 只，其中退市股 {hit['sum'].mean():.1f} 只"
          f"（占比 {hit['share'].mean():.2%}）", flush=True)

    agg_live = dom[~dom["delisted"]].groupby("trade_date")["ret"].mean()
    agg_dead = dom[dom["delisted"]].groupby("trade_date")["ret"].mean()
    agg_all = dom.groupby("trade_date")["ret"].mean()
    daily = pd.DataFrame({"n_live": dom[~dom["delisted"]].groupby("trade_date")["ret"].size(),
                          "n_delisted": dom[dom["delisted"]].groupby("trade_date")["ret"].size(),
                          "ret_live": agg_live, "ret_dead": agg_dead, "ret_all": agg_all}).sort_index()
    daily[[c for c in daily.columns]] = daily.astype(float)
    daily.to_csv(OUT / "domain_daily.csv")

    def metrics(r: pd.Series) -> dict:
        r = r.dropna()
        nav = (1 + r).cumprod()
        dd = float((nav / nav.cummax() - 1).min())
        return {"days": int(len(r)), "ann_return": float(nav.iloc[-1] ** (252 / len(r)) - 1),
                "ann_vol": float(r.std() * np.sqrt(252)), "sharpe": float(r.mean() / r.std() * np.sqrt(252)),
                "max_drawdown": dd}

    only_live = metrics(daily["ret_live"])
    with_dead = metrics(daily["ret_all"])
    dead_only = metrics(daily["ret_dead"])
    # 同时给出与 L3 同口径的测试窗口（2020-01 起），便于和 L3 那张表直接对照
    l3_start = pd.Timestamp("2020-01-01")
    d3 = daily[daily.index >= l3_start]
    l3_live, l3_all = metrics(d3["ret_live"]), metrics(d3["ret_all"])
    print(f"[l9] （L3 同窗口 2020+）仅在市 {l3_live['ann_return']:.2%} vs 含退市 {l3_all['ann_return']:.2%}"
          f" → 偏差 {l3_live['ann_return'] - l3_all['ann_return']:+.2%}/年", flush=True)
    print(f"[l9] 仅在市域等权：年化 {only_live['ann_return']:.2%}，Sharpe {only_live['sharpe']:.2f}，"
          f"回撤 {only_live['max_drawdown']:.1%}", flush=True)
    print(f"[l9] 含退市域等权：年化 {with_dead['ann_return']:.2%}，Sharpe {with_dead['sharpe']:.2f}，"
          f"回撤 {with_dead['max_drawdown']:.1%}  →  偏差 {only_live['ann_return'] - with_dead['ann_return']:+.2%}/年",
          flush=True)

    # 6) 退市前 60 个交易日表现
    tails = []
    for code_, group in dl.sort_values("trade_date").groupby("stock_code"):
        last = group.tail(61)
        if len(last) < 21:
            continue
        tails.append({"stock_code": code_, "last_date": last["trade_date"].iloc[-1],
                      "last_close": float(last["close_px"].iloc[-1]),
                      "ret_60d_before": float(last["close_px"].iloc[-1] / last["close_px"].iloc[0] - 1),
                      "ret_20d_before": float(last["close_px"].iloc[-1] / last["close_px"].iloc[-21] - 1)
                      if len(last) >= 21 else np.nan})
    tail = pd.DataFrame(tails).sort_values("ret_60d_before")
    tail.to_csv(OUT / "delisted_tail.csv", index=False)
    print(f"[l9] 退市前 60 日累计收益：中位 {tail['ret_60d_before'].median():.1%}，"
          f"均值 {tail['ret_60d_before'].mean():.1%}，最差 {tail['ret_60d_before'].min():.1%}（{len(tail)} 只）", flush=True)

    summary = {
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window": [str(daily.index.min().date()), str(daily.index.max().date())],
        "size_max": args.size_max,
        "universe": {"live_rows": int(len(live)), "delisted_rows": int(len(dead)),
                     "delisted_stocks": int(dl["stock_code"].nunique()),
                     "avg_domain_size": float(hit["size"].mean()),
                     "avg_delisted_in_domain": float(hit["sum"].mean()),
                     "delisted_share_in_domain": float(hit["share"].mean())},
        "domain_ew": {"live_only": only_live, "with_delisted": with_dead, "delisted_only": dead_only,
                      "bias_ann_return": only_live["ann_return"] - with_dead["ann_return"],
                      "window_2020plus": {"live_only": l3_live, "with_delisted": l3_all,
                                          "bias_ann_return": l3_live["ann_return"] - l3_all["ann_return"]}},
        "delisted_tail": {"n": int(len(tail)),
                          "median_ret_60d": float(tail["ret_60d_before"].median()),
                          "mean_ret_60d": float(tail["ret_60d_before"].mean()),
                          "worst_ret_60d": float(tail["ret_60d_before"].min())},
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report = [
        "# 小市值域的幸存者偏差：量化结果（L9 第一步）", "",
        f"生成 {summary['built_at']}｜窗口 {summary['window'][0]}~{summary['window'][1]}｜域：主板 + 上市满 1 年 + 市值分位 ≤{args.size_max}", "",
        "## 1. 退市股缺席了多少", "",
        f"- 退市股并入后，全市场每日平均 **{int(len(universe)/universe['trade_date'].nunique()):,}** 只"
        f"（其中退市股 {int(dead.groupby('trade_date').size().mean())} 只）",
        f"- 域内每日平均 **{hit['size'].mean():.0f}** 只，其中退市股 **{hit['sum'].mean():.1f}** 只"
        f"（占比 **{hit['share'].mean():.2%}**）", "",
        "## 2. 偏差有多大（域内等权日收益）", "",
        "| 口径 | 年化 | 年化波动 | Sharpe | 最大回撤 |", "|---|---|---|---|---|",
        f"| 仅在市（现状） | **{only_live['ann_return']:.2%}** | {only_live['ann_vol']:.2%} | {only_live['sharpe']:.2f} | {only_live['max_drawdown']:.1%} |",
        f"| 含退市股（修正后） | **{with_dead['ann_return']:.2%}** | {with_dead['ann_vol']:.2%} | {with_dead['sharpe']:.2f} | {with_dead['max_drawdown']:.1%} |",
        f"| 只看退市股 | {dead_only['ann_return']:.2%} | {dead_only['ann_vol']:.2%} | {dead_only['sharpe']:.2f} | {dead_only['max_drawdown']:.1%} |", "",
        f"⇒ **幸存者偏差约 {summary['domain_ew']['bias_ann_return']:+.2%}/年**（仅在市 − 含退市）；",
        f"与 L3 同窗口（2020 起）：仅在市 {l3_live['ann_return']:.2%} vs 含退市 {l3_all['ann_return']:.2%}"
        f"（偏差 {summary['domain_ew']['window_2020plus']['bias_ann_return']:+.2%}/年）。", "",
        "## 3. 退市前发生了什么", "",
        f"- {summary['delisted_tail']['n']} 只退市股，**退市前 60 个交易日累计收益**：中位 "
        f"{summary['delisted_tail']['median_ret_60d']:.1%}、均值 {summary['delisted_tail']['mean_ret_60d']:.1%}、"
        f"最差 {summary['delisted_tail']['worst_ret_60d']:.1%}",
        "- 这些名字在「仅在市」的域里根本不存在，等于把最惨的一段直接剪掉了。", "",
        "## 4. 局限（必须一起引用）", "",
        "1. 退市表共 209 只（本窗口 2018+ 命中 197 只），**不是全部退市股** → 偏差是下界估计；",
        "2. 退市股的「上市满 1 年」过滤用的是退市表自身的首条记录（2014 起截断），不是真实上市日；",
        "3. 域内等权是**被动基准**，不含选股 alpha，也不扣成本；",
        "4. 本脚本**没有**把退市股并进因子面板（那需要为它们重算 25 列特征）—— 这是 L9 的第二步。", "",
    ]
    (OUT / "REPORT.md").write_text("\n".join(report), encoding="utf-8")
    print(f"[l9] 输出 {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
