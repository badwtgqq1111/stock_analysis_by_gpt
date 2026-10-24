#!/usr/bin/env python3
"""概念观察池：找出「概念广度翻转 + 成分股放量」的标的（只提示，不自动交易）。

规则（2026-10-23 定稿）：
  概念层：广度 = 当日上涨成员占比
          * 翻转：昨日广度 < 45% 且 今日 > 65%（冰点转强）
          * 或 强势：今日广度 ≥ 80% 且 5 日概念涨幅 > 0
  个股层：属于上述概念，且
          * 当日涨幅 ≥ 5%（20cm 板 ≥ 8%）
          * 量比（成交额 / 20 日均额）≥ 2
          * 成交额在概念内排前 5
          * 非涨停（避免追板）
          * 上市 ≥ 20 个交易日（次新股放宽，宇树这类才进得来）
  输出：按 概念热度 × 个股量比 排序，附理由。

为什么只提示：全样本检验里「放量突破」5 日超额 −1.1%、胜率 36%，
点火后回踩也只有 +0.10%，都跑不赢全样本 +0.15%。追涨是负期望，
所以本脚本只产出观察池，不生成买卖指令。

用法：
  uv run python scripts/concept_watchlist.py --date 2026-10-19
  uv run python scripts/concept_watchlist.py --date 2026-10-20 --top 30
"""
from __future__ import annotations

import argparse
import glob
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
MEMBERSHIP = ROOT / "assets/data/meta/cn_board_membership.parquet"
IFIND = ROOT / "assets/data/derived/cn_concept_members_ifind.parquet"
OUT = ROOT / "output/results_cn/concept_watchlist"
FAMILY = {
    "人形机器人": "机器人", "机器人概念": "机器人", "具身智能": "机器人", "智能机器": "机器人",
    "CPO": "光通信", "光模块": "光通信", "存储芯片": "半导体", "华为海思": "半导体",
}


def load_members() -> pd.DataFrame:
    frames = []
    m = pd.read_parquet(MEMBERSHIP)
    m = m[m.taxonomy == "sina_concept"][["board", "code"]].rename(columns={"board": "concept"})
    m["stock_code"] = m["code"].map(lambda x: x + (".SH" if x.startswith(("6", "9")) else ".SZ"))
    frames.append(m[["concept", "stock_code"]])
    if IFIND.exists():
        f = pd.read_parquet(IFIND)
        f = f[f.snapshot_date == f.snapshot_date.max()][["concept", "stock_code"]]
        frames.append(f)
    out = pd.concat(frames, ignore_index=True).drop_duplicates()
    out["family"] = out["concept"].map(lambda c: FAMILY.get(c, c))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="")
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    files = sorted(glob.glob(str(ROOT / "assets/data/clean/ohlcv/market=CN/exchange=*/"
                                    "asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet")))
    bars = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "close", "amount"]).to_pandas()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"]).dt.normalize()
    day = pd.Timestamp(args.date) if args.date else bars["trade_date"].max()
    bars = bars[bars["trade_date"] <= day].drop_duplicates(["trade_date", "stock_code"], keep="last")
    bars = bars.sort_values(["stock_code", "trade_date"])
    g = bars.groupby("stock_code", sort=False)
    bars["n"] = g.cumcount() + 1
    bars["ret"] = g["close"].pct_change()
    bars["amt20"] = g["amount"].transform(lambda s: s.rolling(20, min_periods=10).mean())
    bars["vr"] = bars["amount"] / bars["amt20"]
    bars["ret20"] = g["close"].transform(lambda s: s / s.shift(20) - 1)

    members = load_members()
    panel = bars.merge(members, on="stock_code", how="inner")
    concept = panel.groupby(["concept", "trade_date"]).agg(
        breadth=("ret", lambda s: float((s > 0).mean())), chg=("ret", "mean"),
        n=("ret", "size"), amount=("amount", "sum")).reset_index()
    concept = concept.sort_values(["concept", "trade_date"])
    concept["prev_breadth"] = concept.groupby("concept")["breadth"].shift(1)
    concept["ret5"] = concept.groupby("concept")["chg"].transform(
        lambda s: (1 + s).rolling(5, min_periods=3).apply(np.prod, raw=True) - 1)
    today = concept[concept["trade_date"] == day].copy()
    today["flip"] = (today["prev_breadth"] < 0.45) & (today["breadth"] > 0.65)
    today["strong"] = (today["breadth"] >= 0.80) & (today["ret5"] > 0)
    # 升温：广度连续站上 65%（10-16/10-19 的人形机器人就是这种，尚未到 80%）
    today["warming"] = (today["breadth"] >= 0.65) & (today["prev_breadth"] >= 0.60) & (today["ret5"] > -0.02)
    hot = today[(today["flip"] | today["strong"] | today["warming"]) & (today["n"] >= 10)].copy()

    today_bars = panel[panel["trade_date"] == day].copy()
    today_bars["limit"] = today_bars["stock_code"].map(
        lambda c: 0.195 if c.startswith(("300", "301", "688")) else 0.095)
    today_bars["is_limit"] = today_bars["ret"] >= today_bars["limit"] - 0.003
    picked = []
    for _, row in hot.iterrows():
        cand = today_bars[today_bars["concept"] == row["concept"]].copy()
        cand = cand[(cand["n"] >= 20) & (~cand["is_limit"]) & (cand["vr"] >= 1.5) & (cand["ret"] >= 0.04)]
        if cand.empty:
            continue
        cand = cand.nlargest(5, "amount")
        for _, c in cand.iterrows():
            picked.append({
                "trade_date": day.date(), "concept": row["concept"], "stock_code": c["stock_code"],
                "pct": round(float(c["ret"]) * 100, 2), "vr": round(float(c["vr"]), 2),
                "listed_days": int(c["n"]), "concept_breadth": round(float(row["breadth"]), 3),
                "concept_ret5": round(float(row["ret5"]) * 100, 2),
                "signal": "flip" if row["flip"] else ("strong" if row["strong"] else "warming"),
            })
    out = pd.DataFrame(picked)
    if out.empty:
        print(f"[watchlist] {day.date()} 无信号")
        return 0
    out["score"] = out["concept_breadth"] * 0.5 + out["vr"].clip(0, 5) * 0.25 + out["pct"].clip(0, 20) * 0.01
    out = out.sort_values(["score", "vr"], ascending=False).drop_duplicates("stock_code")
    path = OUT / f"{day.date()}.csv"
    out.to_csv(path, index=False)
    print(out.head(args.top)[["concept", "stock_code", "pct", "vr", "listed_days", "concept_breadth", "signal"]].to_string(index=False))
    print(f"→ {path}（{len(out)} 只）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
