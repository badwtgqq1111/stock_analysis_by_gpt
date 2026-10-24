#!/usr/bin/env python3
"""概念选股 strong 模式的组合回测（与 select_concept_watch_arm.py 同口径）。

规则：
  概念：热度 = 0.4z(涨幅)+0.3z(广度)+0.2z(成交额占比)+0.1z(涨停密度)；取最强、family 去重、
        剔除 5 日涨幅>15%、广度<30%、非主题标签；top3
  个股：概念内滞涨（默认）/最强（leader），上市≥20日、20日均额≥5000万、非涨停、非ST、
        close>MA20、5日涨幅≤25%；每概念≤4，合计 10 只
  执行：t 收盘选股 → t+1 开盘买（gap<5%），持有 5 日或 -8% 止损，双边 15.5bp
"""
from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output/research/p2_concept_v2"
FAMILY = {"军工航天": "军工", "国防军工": "军工", "军民融合": "军工", "大飞机": "军工",
          "人形机器人": "机器人", "机器人概念": "机器人", "具身智能": "机器人", "智能机器": "机器人",
          "CPO": "光通信", "光模块": "光通信", "存储芯片": "半导体", "华为海思": "半导体",
          "固态电池": "新能源", "锂电池": "新能源", "光伏": "新能源"}
NON_THEME = ("基金重仓", "社保重仓", "QFII", "券商重仓", "保险重仓", "融资融券", "深股通", "沪股通",
             "MSCI", "标普", "富时", "转债标的", "预盈预增", "参股金融", "参股券商", "破净股",
             "次新股", "超大盘", "大盘", "中盘", "小盘", "基金", "重仓", "央企50", "国企50",
             "上证50", "沪深300", "中证100", "行业龙头")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--top-concepts", type=int, default=3)
    ap.add_argument("--top-stocks", type=int, default=10)
    ap.add_argument("--per-concept", type=int, default=4)
    ap.add_argument("--hold", type=int, default=5)
    ap.add_argument("--max-pos", type=int, default=10)
    ap.add_argument("--stock-mode", default="laggard", choices=["laggard", "leader", "ignition", "cross"])
    ap.add_argument("--max-concept-ret5", type=float, default=0.15)
    ap.add_argument("--regime", default="none", choices=["none", "ma20"])
    ap.add_argument("--ma-window", type=int, default=20)
    ap.add_argument("--tag", default="strong")
    ap.add_argument("--weight-mode", default="hard", choices=["hard", "relevance"])
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    files = sorted(glob.glob(str(ROOT / "assets/data/clean/ohlcv/market=CN/exchange=*/"
                                    "asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet")))
    bars = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "open", "close", "amount"]).to_pandas()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"]).dt.normalize()
    bars = bars[bars["trade_date"] >= pd.Timestamp(args.start) - pd.Timedelta(days=120)]
    bars = bars.drop_duplicates(["trade_date", "stock_code"], keep="last").sort_values(["stock_code", "trade_date"])
    g = bars.groupby("stock_code", sort=False)
    bars["n"] = g.cumcount() + 1
    bars["ret"] = g["close"].pct_change()
    bars["ret5"] = g["close"].transform(lambda s: s / s.shift(5) - 1)
    bars["ma20"] = g["close"].transform(lambda s: s.rolling(args.ma_window, min_periods=5).mean())
    bars["ma_prev"] = g["ma20"].shift(1)
    bars["close_prev"] = g["close"].shift(1)
    bars["cross_ma"] = (bars["close"] > bars["ma20"]) & (bars["close_prev"] <= bars["ma_prev"])
    bars["amount20"] = g["amount"].transform(lambda s: s.rolling(20, min_periods=10).mean())
    bars["vr"] = bars["amount"] / bars["amount20"]

    _cal = ROOT / ("assets/data/derived/cn_concept_members_relevance_all.parquet"
                   if args.weight_mode == "relevance"
                   else "assets/data/derived/cn_concept_members_scored.parquet")
    if _cal.exists():
        _m = pd.read_parquet(_cal)
        mem = _m[["concept", "stock_code"]].drop_duplicates()
        if args.weight_mode == "relevance" and "relevance" in _m.columns:
            mem = _m[["concept", "stock_code", "relevance"]].drop_duplicates(["concept", "stock_code"])
    else:
        frames = []
        m = pd.read_parquet(ROOT / "assets/data/meta/cn_board_membership.parquet")
        m = m[m.taxonomy == "sina_concept"][["board", "code"]].rename(columns={"board": "concept"})
        m["stock_code"] = m["code"].map(lambda x: x + (".SH" if str(x).startswith(("6", "9")) else ".SZ"))
        frames.append(m[["concept", "stock_code"]])
        mem = pd.concat(frames, ignore_index=True).drop_duplicates()
    mem = mem[~mem["concept"].astype(str).str.contains("|".join(NON_THEME), na=False)]

    bars["limit"] = bars["stock_code"].map(lambda x: 0.195 if x.startswith(("300", "301", "688")) else 0.095)
    _basic = pd.read_parquet(ROOT / "assets/data/meta/cn_stock_basic.parquet", columns=["ts_code", "name"])
    bars["name"] = bars["stock_code"].map(dict(zip(_basic["ts_code"], _basic["name"]))).fillna("")
    panel = bars.merge(mem, on="stock_code", how="inner")
    if args.weight_mode == "relevance" and "relevance" in panel.columns:
        panel["w"] = panel["relevance"].fillna(0.3)
        panel["w_ret"] = panel["w"] * panel["ret"].fillna(0)
        panel["w_up"] = panel["w"] * (panel["ret"].fillna(0) > 0).astype(float)
        c = panel.groupby(["concept", "trade_date"]).agg(
            chg=("w_ret", "sum"), breadth=("w_up", "sum"), w_sum=("w", "sum"),
            n=("ret", "size"), amount=("amount", "sum"),
            limit_ratio=("ret", lambda s: float((s >= 0.098).mean()))).reset_index()
        c["chg"] = c["chg"] / c["w_sum"].replace(0, np.nan)
        c["breadth"] = c["breadth"] / c["w_sum"].replace(0, np.nan)
    else:
        c = panel.groupby(["concept", "trade_date"]).agg(
            chg=("ret", "mean"), breadth=("ret", lambda s: float((s > 0).mean())),
            n=("ret", "size"), amount=("amount", "sum"),
            limit_ratio=("ret", lambda s: float((s >= 0.098).mean()))).reset_index()
    c = c[c["n"] >= 30].copy()
    c["amount_share"] = c["amount"] / c.groupby("trade_date")["amount"].transform("sum")
    zs = lambda s: (s - s.mean()) / (s.std(ddof=0) + 1e-9)
    for col in ("chg", "breadth", "amount_share", "limit_ratio"):
        c[f"z_{col}"] = c.groupby("trade_date")[col].transform(zs)
    c["heat"] = 0.40 * c["z_chg"] + 0.30 * c["z_breadth"] + 0.20 * c["z_amount_share"] + 0.10 * c["z_limit_ratio"]
    c = c.sort_values(["concept", "trade_date"])
    c["ret5"] = c.groupby("concept")["chg"].transform(lambda s: (1 + s).rolling(5, min_periods=3).apply(np.prod, raw=True) - 1)
    c["family"] = c["concept"].map(lambda x: FAMILY.get(x, x))

    start = pd.Timestamp(args.start)
    sel = c[(c["trade_date"] >= start) & (c["breadth"] >= 0.30) & (c["ret5"] <= args.max_concept_ret5)]
    top = (sel.sort_values(["trade_date", "heat"], ascending=[True, False])
              .groupby("trade_date").head(args.top_concepts * 4)
              .drop_duplicates(["trade_date", "family"]).groupby("trade_date").head(args.top_concepts))

    # 概念 → 当日候选股
    day_concepts = {d: list(g2["concept"]) for d, g2 in top.groupby("trade_date")}
    if args.regime == "ma20":
        b = pd.read_parquet(ROOT / "assets/data/meta/cn_benchmark_daily.parquet", columns=["ts_code", "trade_date", "close"])
        b = b[b.ts_code == "000905.SH"].copy(); b["trade_date"] = pd.to_datetime(b["trade_date"])
        b = b.sort_values("trade_date"); b["ma20"] = b["close"].rolling(20).mean()
        ok_days = set(b[b["close"] > b["ma20"]]["trade_date"])
    else:
        ok_days = None

    basic = _basic

    dates = sorted(bars["trade_date"].unique())
    by_date = {d: g2 for d, g2 in bars.groupby("trade_date")}
    pby = {d: g2 for d, g2 in panel.groupby("trade_date")}
    cash, positions, curve, trades = 1.0, [], [], []
    pending_buy, pending_sell = [], set()

    for i, day in enumerate(dates):
        gd = by_date[day]
        opens = dict(zip(gd["stock_code"], gd["open"]))
        closes = dict(zip(gd["stock_code"], gd["close"]))
        for pos in list(positions):
            if pos["code"] in pending_sell:
                px = opens.get(pos["code"])
                if px and np.isfinite(px):
                    cash += pos["shares"] * px * (1 - 0.000155)
                    trades.append({"code": pos["code"], "entry": pos["entry"], "exit": px,
                                   "ret": px / pos["entry"] - 1 - 2 * 0.000155, "days": pos["days"], "date": day,
                                   "concept": pos["concept"]})
                    positions.remove(pos)
        pending_sell.clear()
        for order in pending_buy:
            if len(positions) >= args.max_pos:
                break
            px = opens.get(order["code"])
            if not px or not np.isfinite(px) or px / order["prev"] - 1 >= 0.05:
                continue
            slice_cash = cash / max(1, min(args.max_pos - len(positions), args.max_pos))
            if slice_cash <= 0:
                continue
            cash -= slice_cash
            positions.append({"code": order["code"], "entry": px, "shares": slice_cash / px, "days": 0,
                              "concept": order["concept"]})
        pending_buy = []
        for pos in positions:
            pos["days"] += 1
            px = closes.get(pos["code"], pos["entry"])
            if pos["days"] >= args.hold or px / pos["entry"] - 1 <= -0.08:
                pending_sell.add(pos["code"])
        eq = cash + sum(p["shares"] * closes.get(p["code"], p["entry"]) for p in positions)
        curve.append({"trade_date": day, "nav": eq, "n": len(positions)})

        nxt_day = dates[i + 1] if i + 1 < len(dates) else None
        if nxt_day is None or day not in day_concepts or (ok_days is not None and nxt_day not in ok_days):
            continue
        cons = day_concepts[day]
        cand = pby.get(day, gd.iloc[0:0]).copy()
        cand = cand[cand["concept"].isin(cons)]
        cand = cand[(cand["n"] >= 20) & (cand["amount20"] >= 5e7) & (cand["close"] > cand["ma20"])
                    & (cand["ret"] < cand["limit"] - 0.003) & (cand["ret5"] <= 0.25)
                    & (~cand["name"].str.contains("ST", na=False))].dropna(subset=["ret5", "vr"])
        if cand.empty:
            continue
        picked = []
        for con in cons:
            sub = cand[cand["concept"] == con]
            if sub.empty:
                continue
            if args.stock_mode == "laggard":
                med = sub["ret5"].median()
                sub = sub[sub["ret5"] <= med] if len(sub[sub["ret5"] <= med]) >= 2 else sub
            sub = sub.copy()
            sub["s_ret"] = sub["ret5"].rank(pct=True)
            sub["s_vr"] = sub["vr"].rank(pct=True)
            sub["s_amt"] = sub["amount20"].rank(pct=True)
            if args.stock_mode == "cross":
                sub = sub[(sub["cross_ma"]) & (sub["vr"] >= 1.5) & (sub["ret"] >= 0.02)]
                if sub.empty:
                    continue
            if args.stock_mode == "cross":
                sub["s_day"] = sub["ret"].rank(pct=True)
                sub["score"] = 0.40 * sub["s_vr"] + 0.35 * sub["s_day"] + 0.25 * sub["s_amt"]
            elif args.stock_mode == "ignition":
                # 量能异动：量比 + 当日涨幅 + 成交额（不看 5 日动量方向）
                sub["s_day"] = sub["ret"].rank(pct=True)
                sub["score"] = 0.50 * sub["s_vr"] + 0.30 * sub["s_day"] + 0.20 * sub["s_amt"]
            else:
                dir_ = -1 if args.stock_mode == "laggard" else 1
                sub["score"] = 0.40 * (dir_ * (sub["s_ret"] - 0.5) + 0.5) + 0.30 * sub["s_vr"] + 0.30 * sub["s_amt"]
            picked.append(sub.nlargest(args.per_concept, "score"))
        if not picked:
            continue
        out = (pd.concat(picked).sort_values("score", ascending=False)
               .drop_duplicates("stock_code").head(args.top_stocks))
        slots = args.max_pos - len(positions) + len(pending_sell)
        for _, r in out.head(max(0, slots)).iterrows():
            pending_buy.append({"code": r["stock_code"], "prev": r["close"], "concept": r["concept"]})

    cv = pd.DataFrame(curve)
    cv = cv[cv["trade_date"] >= start].reset_index(drop=True)
    b = pd.read_parquet(ROOT / "assets/data/meta/cn_benchmark_daily.parquet", columns=["ts_code", "trade_date", "close"])
    b = b[b.ts_code == "000905.SH"].copy(); b["trade_date"] = pd.to_datetime(b["trade_date"])
    cv = cv.merge(b[["trade_date", "close"]].rename(columns={"close": "bench"}), on="trade_date", how="left")
    cv["bench_nav"] = cv["bench"] / cv["bench"].iloc[0]
    r = cv["nav"].pct_change().dropna(); rb = cv["bench_nav"].pct_change().dropna()
    yrs = len(r) / 252
    tr = pd.DataFrame(trades)
    summ = {"tag": args.tag, "start": str(cv.trade_date.min().date()), "end": str(cv.trade_date.max().date()),
            "cagr": round((cv["nav"].iloc[-1] ** (1 / yrs) - 1) * 100, 1),
            "dd": round((cv["nav"] / cv["nav"].cummax() - 1).min() * 100, 1),
            "sharpe": round(r.mean() / r.std() * np.sqrt(252), 2) if r.std() else None,
            "bench_cagr": round((cv["bench_nav"].iloc[-1] ** (1 / yrs) - 1) * 100, 1),
            "trades": len(tr), "win": round(float((tr.ret > 0).mean()) * 100, 1) if len(tr) else None,
            "avg": round(float(tr.ret.mean()) * 100, 2) if len(tr) else None}
    cv.to_csv(OUT / f"strong_nav_{args.tag}.csv", index=False)
    tr.to_csv(OUT / f"strong_trades_{args.tag}.csv", index=False)
    (OUT / f"strong_{args.tag}.json").write_text(json.dumps(summ, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summ, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
