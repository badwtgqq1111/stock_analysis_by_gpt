#!/usr/bin/env python3
"""P2.77-C/E/F：主题生命周期、概念网络溢出、拥挤度。

输出：
  output/research/p2_77/lifecycle.json   阶段 → 未来 5/20 日概念收益
  output/research/p2_77/network.json     邻居热度 → 概念未来收益 IC
  output/research/p2_77/crowding.json    拥挤分 → 未来 20 日收益/回撤
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
NON_THEME = ("基金重仓", "社保重仓", "QFII", "融资融券", "深股通", "沪股通", "MSCI", "转债标的",
             "预盈预增", "参股金融", "参股券商", "次新股", "超大盘", "大盘", "中盘", "小盘",
             "基金", "重仓", "央企50", "国企50", "上证50", "沪深300", "中证100", "行业龙头")


def build_panel(start: str = "2024-01-01") -> pd.DataFrame:
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
        n=("ret", "size"), amount=("amount", "sum"),
        limit_ratio=("ret", lambda s: float((s >= 0.098).mean()))).reset_index()
    c = c[c["n"] >= 30].copy()
    c["amount_share"] = c["amount"] / c.groupby("trade_date")["amount"].transform("sum")
    zs = lambda s: (s - s.mean()) / (s.std(ddof=0) + 1e-9)
    c["heat"] = (0.40 * c.groupby("trade_date")["chg"].transform(zs)
                 + 0.30 * c.groupby("trade_date")["breadth"].transform(zs)
                 + 0.20 * c.groupby("trade_date")["amount_share"].transform(zs)
                 + 0.10 * c.groupby("trade_date")["limit_ratio"].transform(zs))
    c = c.sort_values(["concept", "trade_date"])
    g = c.groupby("concept", sort=False)
    c["rise"] = c["heat"] - g["heat"].transform(lambda s: s.shift(1).rolling(5, min_periods=3).mean())
    c["ret5"] = g["chg"].transform(lambda s: (1 + s).rolling(5, min_periods=3).apply(np.prod, raw=True) - 1)
    # 概念未来收益（用概念日收益链）
    c["fwd5"] = g["chg"].transform(lambda s: (1 + s).rolling(5).apply(np.prod, raw=True).shift(-5) - 1)
    c["fwd20"] = g["chg"].transform(lambda s: (1 + s).rolling(20).apply(np.prod, raw=True).shift(-20) - 1)
    c["breadth_3d"] = g["breadth"].transform(lambda s: s.rolling(3).mean())
    c["heat_pct"] = c.groupby("trade_date")["heat"].rank(pct=True)
    return c


def stages(c: pd.DataFrame) -> pd.DataFrame:
    c = c.copy()
    c["stage"] = "neutral"
    accel = (c["breadth"].between(0.40, 0.78)) & (c["rise"] >= 0.8) & (c["heat_pct"] < 0.95)
    crowd = (c["limit_ratio"] >= 0.15) | (c["ret5"] >= 0.25) | (c["heat_pct"] >= 0.95)
    decay = (c["breadth"] < c.groupby("concept")["breadth"].shift(1)) & (c["breadth"] < c["breadth_3d"])
    c.loc[accel, "stage"] = "acceleration"
    c.loc[crowd, "stage"] = "crowding"
    c.loc[decay & ~accel & ~crowd, "stage"] = "decay"
    c.loc[(c["breadth"] < 0.30) & (c["rise"] < 0), "stage"] = "cold"
    return c


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    c = stages(build_panel())
    # P3 生命周期
    life = (c.dropna(subset=["fwd5"]).groupby("stage")
             .agg(n=("fwd5", "size"), fwd5=("fwd5", "mean"), fwd20=("fwd20", "mean"),
                  hit5=("fwd5", lambda s: float((s > 0).mean())))
             .round(4))
    life["fwd5"] = (life["fwd5"] * 100).round(2)
    life["fwd20"] = (life["fwd20"] * 100).round(2)
    (OUT / "lifecycle.json").write_text(json.dumps(life.reset_index().to_dict("records"), ensure_ascii=False, indent=2), encoding="utf-8")
    print("== lifecycle"); print(life.to_string())

    # P5 网络：共成员 Jaccard → 邻居热度
    m = pd.read_parquet(ROOT / "assets/data/meta/cn_board_membership.parquet")
    m = m[m.taxonomy == "sina_concept"][["board", "code"]]
    m.columns = ["concept", "code"]
    sets = m.groupby("concept")["code"].apply(set).to_dict()
    names = list(sets)[:200]
    neigh = {}
    for a in names:
        sims = []
        for b in names:
            if a == b:
                continue
            inter = len(sets[a] & sets[b])
            if inter:
                sims.append((b, inter / len(sets[a] | sets[b])))
        neigh[a] = [b for b, s in sorted(sims, key=lambda x: -x[1])[:5]]

    heat = c.pivot_table(index="trade_date", columns="concept", values="heat")
    fwd = c.pivot_table(index="trade_date", columns="concept", values="fwd5")
    rows = []
    for con, ns in neigh.items():
        if con not in heat.columns:
            continue
        nheat = heat[[n for n in ns if n in heat.columns]].mean(axis=1)
        pair = pd.concat([nheat.rename("nh"), fwd[con].rename("fw")], axis=1).dropna()
        if len(pair) > 100:
            ic = pair["nh"].corr(pair["fw"], method="spearman")
            rows.append({"concept": con, "ic": round(float(ic), 4), "n": len(pair)})
    net = pd.DataFrame(rows)
    net_summary = {"concepts": int(len(net)), "mean_ic": round(float(net["ic"].mean()), 4) if len(net) else None,
                   "t": round(float(net["ic"].mean() / net["ic"].std() * np.sqrt(len(net))), 2) if len(net) > 1 else None,
                   "top": net.nlargest(5, "ic").to_dict("records") if len(net) else []}
    (OUT / "network.json").write_text(json.dumps(net_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("== network", net_summary["mean_ic"], "t", net_summary["t"])

    # P6 拥挤度
    c2 = c.dropna(subset=["fwd20"]).copy()
    c2["crowd"] = (c2.groupby("trade_date")["limit_ratio"].transform(lambda s: s.rank(pct=True))
                   + c2.groupby("trade_date")["amount_share"].transform(lambda s: s.rank(pct=True))
                   + c2.groupby("trade_date")["ret5"].transform(lambda s: s.rank(pct=True))) / 3
    c2["q"] = c2.groupby("trade_date")["crowd"].transform(lambda s: pd.qcut(s.rank(method="first"), 5, labels=False, duplicates="drop"))
    crowd = c2.groupby("q").agg(n=("fwd20", "size"), fwd20=("fwd20", "mean"), hit=("fwd20", lambda s: float((s > 0).mean()))).round(4)
    crowd["fwd20"] = (crowd["fwd20"] * 100).round(2)
    (OUT / "crowding.json").write_text(json.dumps(crowd.reset_index().to_dict("records"), ensure_ascii=False, indent=2), encoding="utf-8")
    print("== crowding"); print(crowd.to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
