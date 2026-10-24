#!/usr/bin/env python3
"""概念轮动 v2：成分股自建概念指数 + 次日开盘执行回测。

数据现实（2026-10-23 实测）：
  * 概念名单/成分：新浪可用（175 概念 / 3,265 只），但**只有当前快照**，
    新票（301428/301313）和新概念（人形机器人/CPO）覆盖不到；
  * 同花顺 375 概念只有名单没有成分（成分页 403），东财 BK 历史本机 IP 被拒；
  * 因此本回测用**当前成分回溯 2016-2026** —— 存在幸存者/前视偏差，结果是"上界"，
    不能当实盘预期。2026 起的样本偏差最小。

策略（与答复里的 v2 口径一致）：
  板块分 = 0.45*rank(20日板块收益) + 0.25*rank(5日成交额占比变化)
         + 0.20*rank(板块广度) - 0.10*rank(5日板块收益)；5日涨幅>25% 直接剔除
  个股分（top3 概念内）= 0.4*rank(-5日涨幅) + 0.3*rank(5日资金流) + 0.3*rank(20日均额)
  过滤：上市≥60日、20日均额≥5000万、非ST、当日非涨停
  执行：t 日收盘出信号 → t+1 开盘买（gap<3%），持有 5 日或 -8% 止损（次日开盘卖）
  成本：双边 15.5bp；最多 5 只，单概念 ≤2

用法：
  uv run python scripts/concept_rotation_v2.py --start 2020-01-01
  uv run python scripts/concept_rotation_v2.py --start 2016-01-01 --max-pos 5
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
MEMBERSHIP = ROOT / "assets/data/meta/cn_board_membership.parquet"
NAMECHANGE = ROOT / "assets/data/meta/cn_stock_namechange.parquet"
MONEYFLOW = ROOT / "assets/data/derived/cn_moneyflow_features.parquet"
BENCH = ROOT / "assets/data/meta/cn_benchmark_daily.parquet"
OUT = ROOT / "output/research/p2_concept_v2"
LIMIT = {"main": 0.095, "gem": 0.195}


def board_of(code: str) -> str:
    if code.startswith(("300", "301")):
        return "gem"
    if code.startswith("688"):
        return "star"
    if code.startswith(("8", "4")):
        return "bj"
    return "main"


def load_bars(start: str) -> pd.DataFrame:
    files = sorted(glob.glob(str(ROOT / "assets/data/clean/ohlcv/market=CN/exchange=*/"
                                    "asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet")))
    df = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "open", "high", "low", "close", "amount", "turnover"]).to_pandas()
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.normalize()
    df = df[df["trade_date"] >= pd.Timestamp(start)]
    df = df.drop_duplicates(["trade_date", "stock_code"], keep="last").sort_values(["stock_code", "trade_date"])
    g = df.groupby("stock_code", sort=False)
    df["ret"] = g["close"].pct_change()
    df["ret5"] = g["close"].transform(lambda s: s / s.shift(5) - 1)
    df["amount20"] = g["amount"].transform(lambda s: s.rolling(20, min_periods=10).mean())
    df["amount5"] = g["amount"].transform(lambda s: s.rolling(5, min_periods=3).mean())
    df["n_obs"] = g.cumcount() + 1
    df["next_open"] = g["open"].shift(-1)
    df["next_date"] = g["trade_date"].shift(-1)
    return df


def load_st() -> pd.DataFrame:
    if not NAMECHANGE.exists():
        return pd.DataFrame(columns=["stock_code", "start_date", "end_date", "is_st"])
    nc = pd.read_parquet(NAMECHANGE)
    cols = {c.lower(): c for c in nc.columns}
    code = cols.get("stock_code") or cols.get("ts_code") or cols.get("code")
    name = cols.get("name") or cols.get("stock_name")
    start = cols.get("start_date") or cols.get("in_date")
    end = cols.get("end_date") or cols.get("out_date")
    out = pd.DataFrame({
        "stock_code": nc[code].astype(str),
        "start_date": pd.to_datetime(nc[start], errors="coerce"),
        "end_date": pd.to_datetime(nc[end], errors="coerce").fillna(pd.Timestamp("2099-12-31")),
        "is_st": nc[name].astype(str).str.contains("ST", case=False, na=False) if name else False,
    })
    return out[out["is_st"]]


def build_concept_index(bars: pd.DataFrame, members: pd.DataFrame) -> pd.DataFrame:
    panel = bars.assign(code=bars["stock_code"].str[:6]).merge(
        members[["code", "board"]], on="code", how="inner")
    panel = panel.dropna(subset=["ret"])
    g = panel.groupby(["board", "trade_date"], sort=True)
    agg = g.agg(ret_ew=("ret", "mean"), amount=("amount", "sum"),
                n=("ret", "size"), up=("ret", lambda s: float((s > 0).mean()))).reset_index()
    agg = agg.sort_values(["board", "trade_date"])
    gb = agg.groupby("board", sort=False)
    agg["ret20"] = gb["ret_ew"].transform(lambda s: (1 + s).rolling(20, min_periods=15).apply(np.prod, raw=True) - 1)
    agg["ret5"] = gb["ret_ew"].transform(lambda s: (1 + s).rolling(5, min_periods=3).apply(np.prod, raw=True) - 1)
    agg["amount20"] = gb["amount"].transform(lambda s: s.rolling(20, min_periods=10).mean())
    agg["amt_share"] = agg["amount"] / agg.groupby("trade_date")["amount"].transform("sum")
    agg["amt_share_chg"] = agg["amt_share"] - gb["amt_share"].transform(lambda s: s.rolling(5, min_periods=3).mean().shift(5))
    return agg


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--max-pos", type=int, default=5)
    ap.add_argument("--hold", type=int, default=5)
    ap.add_argument("--cost", type=float, default=0.000155)
    ap.add_argument("--stock-mode", default="laggard", choices=["laggard", "leader"])
    ap.add_argument("--tag", default="")
    ap.add_argument("--concept-mode", default="momentum", choices=["momentum", "reversal"])
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    bars = load_bars(args.start)
    m = pd.read_parquet(MEMBERSHIP)
    m = m[m.taxonomy == "sina_concept"][["board", "code"]].drop_duplicates()
    concept = build_concept_index(bars, m)
    concept.to_parquet(OUT / "concept_index.parquet", index=False)
    print(f"[v2] 概念指数 {concept.board.nunique()} 个 / {len(concept):,} 行")

    st = load_st()
    mf = pd.DataFrame()
    if MONEYFLOW.exists():
        mf = pd.read_parquet(MONEYFLOW, columns=["trade_date", "stock_code", "moneyflow_net_z_5d"]) \
            if "moneyflow_net_z_5d" in pd.read_parquet(MONEYFLOW, columns=["trade_date"]).columns else pd.DataFrame()

    # 信号：按日选板块与个股
    concept["rk20"] = concept.groupby("trade_date")["ret20"].rank(pct=True)
    concept["rk5"] = concept.groupby("trade_date")["ret5"].rank(pct=True)
    concept["rk_amt"] = concept.groupby("trade_date")["amt_share_chg"].rank(pct=True)
    concept["rk_breadth"] = concept.groupby("trade_date")["up"].rank(pct=True)
    concept["score"] = (0.45 * concept["rk20"] + 0.25 * concept["rk_amt"]
                        + 0.20 * concept["rk_breadth"] - 0.10 * concept["rk5"])
    concept.loc[concept["ret5"] > 0.25, "score"] = np.nan
    if args.concept_mode == "reversal":
        concept["score"] = -concept["score"]

    bars["code"] = bars["stock_code"].str[:6]
    bars = bars.merge(m[["code", "board"]].rename(columns={"board": "concept"}), on="code", how="left")
    bars["limit"] = bars["stock_code"].map(lambda c: LIMIT.get(board_of(c), LIMIT["main"]))
    bars["is_limit"] = bars["ret"] >= bars["limit"] - 0.003
    if not st.empty:
        bars = bars.merge(st[["stock_code", "start_date", "end_date"]], on="stock_code", how="left")
        bars["is_st"] = (bars["trade_date"] >= bars["start_date"]) & (bars["trade_date"] <= bars["end_date"])
        bars["is_st"] = bars["is_st"].fillna(False)
    else:
        bars["is_st"] = False

    dates = sorted(bars["trade_date"].unique())
    by_date = {d: g for d, g in bars.groupby("trade_date")}
    scores = concept.set_index(["trade_date", "board"])["score"]

    cash, positions, equity_curve, trades = 1.0, [], [], []
    pending_buys: list[dict] = []
    pending_sells: set[str] = set()

    for i, day in enumerate(dates):
        g = by_date[day]
        prices = dict(zip(g["stock_code"], g["next_open"]))
        opens = dict(zip(g["stock_code"], g["open"]))
        closes = dict(zip(g["stock_code"], g["close"]))

        # 1) 开盘卖出
        for pos in list(positions):
            if pos["code"] in pending_sells:
                px = opens.get(pos["code"])
                if px is None or not np.isfinite(px):
                    continue
                cash += pos["shares"] * px * (1 - args.cost)
                trades.append({"code": pos["code"], "entry": pos["entry"], "exit": px,
                               "days": pos["days"], "ret": px / pos["entry"] - 1 - 2 * args.cost,
                               "concept": pos["concept"], "exit_date": day})
                positions.remove(pos)
        pending_sells.clear()

        # 2) 开盘买入
        for order in pending_buys:
            code = order["code"]
            px = opens.get(code)
            if px is None or not np.isfinite(px) or len(positions) >= args.max_pos:
                continue
            if px / order["prev_close"] - 1 >= 0.03:      # gap 过滤
                continue
            slice_cash = cash / max(1, min(len(pending_buys), args.max_pos - len(positions)))
            if slice_cash <= 0:
                continue
            shares = slice_cash / px
            cash -= slice_cash
            positions.append({"code": code, "entry": px, "shares": shares, "days": 0,
                              "concept": order["concept"], "stop": False, "entry_date": day})
        pending_buys = []

        # 3) 收盘结算：持仓天数、止损
        for pos in positions:
            pos["days"] += 1
            px = closes.get(pos["code"])
            if px is not None and px / pos["entry"] - 1 <= -0.08:
                pos["stop"] = True
            if pos["days"] >= args.hold or pos["stop"]:
                pending_sells.add(pos["code"])

        # 4) 收盘后出信号（次日执行）
        if i + 1 < len(dates):
            nxt = dates[i + 1]
            day_concepts = concept[concept["trade_date"] == day].dropna(subset=["score"]).nlargest(3, "score")
            if len(day_concepts):
                cand = g[g["concept"].isin(day_concepts["board"])].copy()
                cand = cand[(cand["n_obs"] >= 60) & (cand["amount20"] >= 5e7) & (~cand["is_st"]) & (~cand["is_limit"])]
                cand = cand.dropna(subset=["ret5", "amount20"])
                if len(cand):
                    cand["s_ret"] = cand.groupby("concept")["ret5"].rank(pct=True)
                    cand["s_amt"] = cand["amount20"].rank(pct=True)
                    cand["s_flow"] = 0.5
                    if not mf.empty:
                        merged = cand.merge(mf, on=["trade_date", "stock_code"], how="left")
                        if "moneyflow_net_z_5d" in merged:
                            cand["s_flow"] = merged["moneyflow_net_z_5d"].rank(pct=True).fillna(0.5).values
                    dir_ = -1 if args.stock_mode == "laggard" else 1
                    cand["stock_score"] = 0.4 * (dir_ * (cand["s_ret"] - 0.5) + 0.5) + 0.3 * cand["s_flow"] + 0.3 * cand["s_amt"]
                    cand["c_rank"] = cand.groupby("concept")["stock_score"].rank(ascending=False, method="first")
                    cand = cand[cand["c_rank"] <= 2].nlargest(args.max_pos, "stock_score")
                    slots = args.max_pos - len(positions) + len([p for p in positions if p["code"] in pending_sells])
                    for _, row in cand.head(max(0, slots)).iterrows():
                        pending_buys.append({"code": row["stock_code"], "concept": row["concept"],
                                             "prev_close": row["close"]})
        equity = cash + sum(p["shares"] * closes.get(p["code"], p["entry"]) for p in positions)
        equity_curve.append({"trade_date": day, "nav": equity, "n_pos": len(positions)})

    curve = pd.DataFrame(equity_curve)
    bench = pd.read_parquet(BENCH, columns=["ts_code", "trade_date", "close"])
    bench = bench[bench["ts_code"] == "000905.SH"].copy()
    bench["trade_date"] = pd.to_datetime(bench["trade_date"])
    curve = curve.merge(bench[["trade_date", "close"]].rename(columns={"close": "bench"}), on="trade_date", how="left")
    curve["bench_nav"] = curve["bench"] / curve["bench"].iloc[0]

    r = curve["nav"].pct_change().dropna()
    rb = curve["bench_nav"].pct_change().dropna()
    yrs = len(r) / 252
    def stats(x, nav):
        return {"cagr": round((nav.iloc[-1] ** (1 / yrs) - 1) * 100, 1),
                "vol": round(x.std() * np.sqrt(252) * 100, 1),
                "dd": round((nav / nav.cummax() - 1).min() * 100, 1),
                "sharpe": round(x.mean() / x.std() * np.sqrt(252), 2) if x.std() else None}
    tr = pd.DataFrame(trades)
    summary = {"start": str(curve.trade_date.min().date()), "end": str(curve.trade_date.max().date()),
               "strategy": stats(r, curve["nav"]), "benchmark": stats(rb, curve["bench_nav"]),
               "trades": int(len(tr)),
               "win_rate": round(float((tr["ret"] > 0).mean()) * 100, 1) if len(tr) else None,
               "avg_trade": round(float(tr["ret"].mean()) * 100, 2) if len(tr) else None,
               "avg_days": round(float(tr["days"].mean()), 1) if len(tr) else None,
               "max_positions": args.max_pos, "hold_days": args.hold,
               "note": "新浪当前成分回溯 → 幸存者/前视偏差，结果为上界"}
    tag = args.tag or f"{args.start[:4]}_{args.concept_mode}_{args.stock_mode}_h{args.hold}"
    curve.to_csv(OUT / f"nav_{tag}.csv", index=False)
    tr.to_csv(OUT / f"trades_{tag}.csv", index=False)
    (OUT / f"summary_{tag}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["tag"] = tag
    print(json.dumps({k: summary[k] for k in ("tag", "strategy", "benchmark", "trades", "win_rate", "avg_trade")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
