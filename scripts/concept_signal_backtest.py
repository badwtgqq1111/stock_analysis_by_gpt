#!/usr/bin/env python3
"""概念观察信号的组合回测（验证「10-20 选中宇树」这类信号的真实期望）。

信号（与 concept_watchlist.py 同口径）：
  概念：广度>=65% 且 前一日>=60%（升温）或 广度>=80%（强势）或 冰点翻转
  个股：量比>=1.5、当日涨幅>=4%、成交额概念内前5、上市>=20日、非涨停
执行：t 收盘出信号 → t+1 开盘买（gap<5%），持有 5 日或 -8% 止损，最多 5 只，双边 15.5bp
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--max-pos", type=int, default=5)
    ap.add_argument("--hold", type=int, default=5)
    ap.add_argument("--regime", default="none", choices=["none", "ma20", "breadth"])
    ap.add_argument("--max-ret20", type=float, default=9.9)
    ap.add_argument("--mf-min", type=float, default=-9.9)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    files = sorted(glob.glob(str(ROOT / "assets/data/clean/ohlcv/market=CN/exchange=*/"
                                    "asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet")))
    bars = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "open", "close", "amount"]).to_pandas()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"]).dt.normalize()
    bars = bars[bars["trade_date"] >= pd.Timestamp(args.start)].drop_duplicates(
        ["trade_date", "stock_code"], keep="last").sort_values(["stock_code", "trade_date"])
    g = bars.groupby("stock_code", sort=False)
    bars["n"] = g.cumcount() + 1
    bars["ret"] = g["close"].pct_change()
    bars["amt20"] = g["amount"].transform(lambda s: s.rolling(20, min_periods=10).mean())
    bars["vr"] = bars["amount"] / bars["amt20"]
    bars["ret20"] = g["close"].transform(lambda s: s / s.shift(20) - 1)

    frames = []
    m = pd.read_parquet(ROOT / "assets/data/meta/cn_board_membership.parquet")
    m = m[m.taxonomy == "sina_concept"][["board", "code"]].rename(columns={"board": "concept"})
    m["stock_code"] = m["code"].map(lambda x: x + (".SH" if x.startswith(("6", "9")) else ".SZ"))
    frames.append(m[["concept", "stock_code"]])
    f = ROOT / "assets/data/derived/cn_concept_members_ifind.parquet"
    if f.exists():
        d = pd.read_parquet(f)
        frames.append(d[d.snapshot_date == d.snapshot_date.max()][["concept", "stock_code"]])
    mem = pd.concat(frames, ignore_index=True).drop_duplicates()

    panel = bars.merge(mem, on="stock_code", how="inner")
    c = panel.groupby(["concept", "trade_date"]).agg(
        breadth=("ret", lambda s: float((s > 0).mean())), chg=("ret", "mean"), n=("ret", "size")).reset_index()
    c = c.sort_values(["concept", "trade_date"])
    c["prev"] = c.groupby("concept")["breadth"].shift(1)
    c["ret5"] = c.groupby("concept")["chg"].transform(lambda s: (1 + s).rolling(5, min_periods=3).apply(np.prod, raw=True) - 1)
    c["flip"] = (c["prev"] < 0.45) & (c["breadth"] > 0.65)
    c["strong"] = (c["breadth"] >= 0.80) & (c["ret5"] > 0)
    c["warm"] = (c["breadth"] >= 0.65) & (c["prev"] >= 0.60) & (c["ret5"] > -0.02)
    sig = c[(c["flip"] | c["strong"] | c["warm"]) & (c["n"] >= 10)][["concept", "trade_date"]]

    cand = panel.merge(sig, on=["concept", "trade_date"], how="inner")
    cand["limit"] = cand["stock_code"].map(lambda x: 0.195 if x.startswith(("300", "301", "688")) else 0.095)
    cand = cand[(cand["n"] >= 20) & (cand["vr"] >= 1.5) & (cand["ret"] >= 0.04)
                & (cand["ret"] < cand["limit"] - 0.003)].dropna(subset=["vr"])
    cand["amt_rank"] = cand.groupby(["concept", "trade_date"])["amount"].rank(ascending=False)
    cand = cand[cand["amt_rank"] <= 5]
    cand = cand[cand["ret20"] <= args.max_ret20]
    if args.mf_min > -9:
        mf = pd.read_parquet(ROOT / "assets/data/derived/cn_moneyflow_features.parquet",
                             columns=["trade_date", "stock_code", "moneyflow_net_z_5d"])
        cand = cand.merge(mf, on=["trade_date", "stock_code"], how="left")
        cand = cand[cand["moneyflow_net_z_5d"] >= args.mf_min]
    if args.regime != "none":
        b = pd.read_parquet(ROOT / "assets/data/meta/cn_benchmark_daily.parquet", columns=["ts_code", "trade_date", "close"])
        b = b[b.ts_code == "000905.SH"].copy(); b["trade_date"] = pd.to_datetime(b["trade_date"])
        b = b.sort_values("trade_date"); b["ma20"] = b["close"].rolling(20).mean()
        if args.regime == "ma20":
            ok = set(b[b["close"] > b["ma20"]]["trade_date"])
        else:
            br = pd.read_parquet(ROOT / "assets/data/derived/cn_market_breadth.parquet", columns=["trade_date", "breadth"])
            ok = set(br[br["breadth"] > 0.40]["trade_date"])
        cand = cand[cand["trade_date"].isin(ok)]
    cand["score"] = (cand["vr"].clip(0, 5) * 0.5 + cand["ret"].clip(0, 0.2) * 10 * 0.5)

    dates = sorted(bars["trade_date"].unique())
    by_date = {d: g2 for d, g2 in bars.groupby("trade_date")}
    day_sig = {d: g2 for d, g2 in cand.groupby("trade_date")}
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
                                   "ret": px / pos["entry"] - 1 - 2 * 0.000155, "days": pos["days"],
                                   "date": day})
                    positions.remove(pos)
        pending_sell.clear()
        for order in pending_buy:
            if len(positions) >= args.max_pos:
                break
            px = opens.get(order["code"])
            if not px or not np.isfinite(px) or px / order["prev"] - 1 >= 0.05:
                continue
            slice_cash = cash / max(1, args.max_pos - len(positions))
            if slice_cash <= 0:
                continue
            cash -= slice_cash
            positions.append({"code": order["code"], "entry": px, "shares": slice_cash / px, "days": 0})
        pending_buy = []
        for pos in positions:
            pos["days"] += 1
            px = closes.get(pos["code"], pos["entry"])
            if pos["days"] >= args.hold or px / pos["entry"] - 1 <= -0.08:
                pending_sell.add(pos["code"])
        if i + 1 < len(dates) and day in day_sig:
            gs = day_sig[day].nlargest(args.max_pos, "score")
            slots = args.max_pos - len(positions) + len(pending_sell)
            for _, r in gs.head(max(0, slots)).iterrows():
                pending_buy.append({"code": r["stock_code"], "prev": r["close"]})
        eq = cash + sum(p["shares"] * closes.get(p["code"], p["entry"]) for p in positions)
        curve.append({"trade_date": day, "nav": eq, "n": len(positions)})

    cv = pd.DataFrame(curve)
    b = pd.read_parquet(ROOT / "assets/data/meta/cn_benchmark_daily.parquet", columns=["ts_code", "trade_date", "close"])
    b = b[b.ts_code == "000905.SH"].copy(); b["trade_date"] = pd.to_datetime(b["trade_date"])
    cv = cv.merge(b[["trade_date", "close"]].rename(columns={"close": "bench"}), on="trade_date", how="left")
    cv["bench_nav"] = cv["bench"] / cv["bench"].iloc[0]
    r = cv.nav.pct_change().dropna(); rb = cv.bench_nav.pct_change().dropna()
    yrs = len(r) / 252
    tr = pd.DataFrame(trades)
    summ = {"start": str(cv.trade_date.min().date()), "end": str(cv.trade_date.max().date()),
            "cagr": round((cv.nav.iloc[-1] ** (1 / yrs) - 1) * 100, 1),
            "dd": round((cv.nav / cv.nav.cummax() - 1).min() * 100, 1),
            "sharpe": round(r.mean() / r.std() * np.sqrt(252), 2) if r.std() else None,
            "bench_cagr": round((cv.bench_nav.iloc[-1] ** (1 / yrs) - 1) * 100, 1),
            "trades": len(tr), "win": round(float((tr.ret > 0).mean()) * 100, 1) if len(tr) else None,
            "avg": round(float(tr.ret.mean()) * 100, 2) if len(tr) else None,
            "yushu_trades": tr[tr.code == "688836.SH"].to_dict("records") if len(tr) else []}
    cv.to_csv(OUT / "watch_backtest_nav.csv", index=False)
    tr.to_csv(OUT / "watch_backtest_trades.csv", index=False)
    (OUT / "watch_backtest.json").write_text(json.dumps(summ, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(summ, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
