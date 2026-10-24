#!/usr/bin/env python3
"""概念「开始变热」选股（不是追当前最热）。

信号定义（全部用 t 日收盘及之前的数据）：
  概念热度 heat_t = 当日横截面 z 加权：涨幅 0.40 + 广度 0.30 + 成交额占比 0.20 + 涨停密度 0.10
  升温度 rise_t  = heat_t − mean(heat_{t-5..t-1})          # 热度加速度，而不是热度水平
  广度跳升        = breadth_t − breadth_{t-1}
  资金跳升        = amt_share_t − mean(amt_share_{t-5..t-1})
  emergence      = 0.50*rank(rise) + 0.30*rank(广度跳升) + 0.20*rank(资金跳升)
  过热惩罚        = 若 heat_t 已在全市场前 10%，emergence × 0.6（已经热透的不追）
  再剔除 5 日涨幅 > 20% 的概念（已经涨完）

选股（在 emergence top3 概念内，family 去重）：
  偏好「刚启动的滞涨股」：5 日涨幅低于概念中位数、量比≥1.2、收盘>MA20、
  上市≥20 日、20 日均额≥5000 万、非涨停、非 ST；
  打分 0.4*rank(-ret5) + 0.3*rank(vr) + 0.3*rank(amount20)，取 5 只。

产物：
  output/results_cn/concept_watch_arm_selection.csv
  output/results_cn/concept_watch_arm_history/<date>.csv
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
OUT = ROOT / "output/results_cn/concept_watch_arm_selection.csv"
HIST = ROOT / "output/results_cn/concept_watch_arm_history"
FAMILY = {"军工航天": "军工", "国防军工": "军工", "军民融合": "军工", "大飞机": "军工",
          "人形机器人": "机器人", "机器人概念": "机器人", "具身智能": "机器人", "智能机器": "机器人",
          "CPO": "光通信", "光模块": "光通信", "存储芯片": "半导体", "华为海思": "半导体",
          "固态电池": "新能源", "锂电池": "新能源", "光伏": "新能源"}
NON_THEME = ("基金重仓", "社保重仓", "QFII", "券商重仓", "保险重仓", "融资融券", "深股通", "沪股通",
             "MSCI", "标普", "富时", "转债标的", "预盈预增", "参股金融", "参股券商", "破净股",
             "次新股", "超大盘", "大盘", "中盘", "小盘", "基金", "重仓",
             "央企50", "国企50", "上证50", "沪深300", "中证100", "行业龙头", "MSCI中国")
INDEX_LIKE = ("科创50", "上证50", "沪深300", "中证500", "中证1000", "创业板指", "深证成指",
              "北证50", "上证指数", "深证综指", "中证全指", "科创板", "创业板综")


def members() -> pd.DataFrame:
    cal = ROOT / "assets/data/derived/cn_concept_members_scored.parquet"
    if cal.exists():
        return pd.read_parquet(cal)[["concept", "stock_code"]].drop_duplicates()
    frames = []
    m = pd.read_parquet(ROOT / "assets/data/meta/cn_board_membership.parquet")
    m = m[m.taxonomy == "sina_concept"][["board", "code"]].rename(columns={"board": "concept"})
    m["stock_code"] = m["code"].map(lambda x: x + (".SH" if str(x).startswith(("6", "9")) else ".SZ"))
    frames.append(m[["concept", "stock_code"]])
    f = ROOT / "assets/data/derived/cn_concept_members_ifind.parquet"
    if f.exists():
        d = pd.read_parquet(f)
        frames.append(d[d.snapshot_date == d.snapshot_date.max()][["concept", "stock_code"]])
    return pd.concat(frames, ignore_index=True).drop_duplicates()


def zs(s: pd.Series) -> pd.Series:
    return (s - s.mean()) / (s.std(ddof=0) + 1e-9)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="")
    ap.add_argument("--top-concepts", type=int, default=3)
    ap.add_argument("--top-stocks", type=int, default=10)
    ap.add_argument("--per-concept", type=int, default=4)
    ap.add_argument("--concept-mode", default="strong", choices=["rise", "strong"],
                    help="rise=热度加速度（默认）；strong=最强概念 + 不涨太高")
    ap.add_argument("--max-concept-ret5", type=float, default=0.15, help="strong 模式：概念 5 日涨幅上限")
    ap.add_argument("--max-stock-ret5", type=float, default=0.25, help="个股 5 日涨幅上限（不追太高）")
    ap.add_argument("--ma-window", type=int, default=10, help="个股趋势门槛：收盘 > N 日均线（默认 10）")
    ap.add_argument("--stock-mode", default="laggard", choices=["laggard", "leader", "ignition", "cross"],
                    help="laggard=买概念内滞涨（默认，历史更稳）；leader=买概念内最强（会选中宇树这类）")
    args = ap.parse_args()
    HIST.mkdir(parents=True, exist_ok=True)

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
    bars["ret5"] = g["close"].transform(lambda s: s / s.shift(5) - 1)
    bars["ma20"] = g["close"].transform(lambda s: s.rolling(args.ma_window, min_periods=5).mean())
    bars["ma_prev"] = g["ma20"].shift(1)
    bars["close_prev"] = g["close"].shift(1)
    bars["cross_ma"] = (bars["close"] > bars["ma20"]) & (bars["close_prev"] <= bars["ma_prev"])
    bars["amount20"] = g["amount"].transform(lambda s: s.rolling(20, min_periods=10).mean())
    bars["vr"] = bars["amount"] / bars["amount20"]

    m = members()
    panel = bars.merge(m, on="stock_code", how="inner")
    c = panel.groupby(["concept", "trade_date"]).agg(
        chg=("ret", "mean"), breadth=("ret", lambda s: float((s > 0).mean())),
        n=("ret", "size"), amount=("amount", "sum"),
        limit_ratio=("ret", lambda s: float((s >= 0.098).mean()))).reset_index()
    c = c[(c["n"] >= 10) & (~c["concept"].isin(INDEX_LIKE))].copy()
    c = c[~c["concept"].astype(str).str.contains("|".join(NON_THEME), na=False)]
    c = c.sort_values(["concept", "trade_date"])
    c["amount_share"] = c["amount"] / c.groupby("trade_date")["amount"].transform("sum")
    # 每日横截面 z → 热度
    for col in ("chg", "breadth", "amount_share"):
        c[f"z_{col}"] = c.groupby("trade_date")[col].transform(zs)
    c["z_limit"] = c.groupby("trade_date")["limit_ratio"].transform(zs)
    c["heat"] = (0.40 * c["z_chg"] + 0.30 * c["z_breadth"]
                 + 0.20 * c["z_amount_share"] + 0.10 * c["z_limit"])
    gb = c.groupby("concept", sort=False)
    c["ret5"] = gb["chg"].transform(lambda s: (1 + s).rolling(5, min_periods=3).apply(np.prod, raw=True) - 1)
    c["heat_ma5_prev"] = gb["heat"].transform(lambda s: s.shift(1).rolling(5, min_periods=3).mean())
    c["amt_ma5_prev"] = gb["amount_share"].transform(lambda s: s.shift(1).rolling(5, min_periods=3).mean())
    c["breadth_prev"] = gb["breadth"].shift(1)
    c["rise"] = c["heat"] - c["heat_ma5_prev"]
    c["breadth_jump"] = c["breadth"] - c["breadth_prev"]
    c["amt_jump"] = c["amount_share"] - c["amt_ma5_prev"]

    today = c[c["trade_date"] == day].copy()
    # 「开始变热」而不是「已经最热」：看热度加速度 rise + 广度回升，且要求广度≥50%（真有参与度）
    today["breadth_jump"] = today["breadth"] - today["breadth_prev"]
    today["amt_jump"] = today["amount_share"] - today["amt_ma5_prev"]
    if args.concept_mode == "strong":
        # 最强概念，但排除已经涨太多的；广度门槛放宽到 35%，再不行退回 20%（标记 weak）
        base = today[(today["n"] >= 30) & (today["ret5"] <= args.max_concept_ret5)
                     & (today["amount_share"] >= 0.005)]
        # 最强概念 + 不涨太高；广度≥30% 才算"有参与度"，否则宁可不选（避免朝鲜改革这类噪声概念）
        eligible = base[base["breadth"] >= 0.30]
        eligible = eligible.sort_values("heat", ascending=False)
    else:
        eligible = today[(today["n"] >= 20) & (today["breadth"] >= 0.50) & (today["rise"] >= 0.8)
                         & (today["ret5"] <= 0.25)]
    if eligible.empty:
        # 没有升温概念：不覆盖上一份有效名单，只写状态（前端显示"最近信号日"）
        status = ROOT / "output/results_cn/concept_watch_arm_status.json"
        status.write_text(json.dumps({"date": str(day.date()), "status": "no_signal",
                                      "reason": "无概念满足 广度≥50% 且 热度加速度 rise≥0.8"},
                                     ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[concept-arm] {day.date()} 没有概念在升温 → 保留上一份名单，写 status=no_signal")
        return 0
    eligible = eligible.copy()
    for col in ("rise", "breadth_jump", "amt_jump"):
        eligible[f"r_{col}"] = eligible[col].rank(pct=True)
    eligible["emergence"] = 0.60 * eligible["r_rise"] + 0.20 * eligible["r_breadth_jump"] + 0.20 * eligible["r_amt_jump"]
    eligible["family"] = eligible["concept"].map(lambda x: FAMILY.get(x, x))
    sort_col = "heat" if args.concept_mode == "strong" else "emergence"
    picked_concepts = eligible.sort_values(sort_col, ascending=False).drop_duplicates("family").head(args.top_concepts)

    tb = panel[panel["trade_date"] == day].copy()
    tb["limit"] = tb["stock_code"].map(lambda x: 0.195 if x.startswith(("300", "301", "688")) else 0.095)
    tb = tb[(tb["n"] >= 20) & (tb["amount20"] >= 5e7) & (tb["ret"] < tb["limit"] - 0.003)
            & (tb["close"] > tb["ma20"]) & (tb["ret5"] <= args.max_stock_ret5)]
    basic = pd.read_parquet(ROOT / "assets/data/meta/cn_stock_basic.parquet", columns=["ts_code", "name"])
    tb = tb.merge(basic.rename(columns={"ts_code": "stock_code"}), on="stock_code", how="left")
    tb = tb[~tb["name"].astype(str).str.contains("ST", na=False)]

    picks = []
    for _, row in picked_concepts.iterrows():
        sub = tb[tb["concept"] == row["concept"]].copy()
        if sub.empty:
            continue
        if args.stock_mode == "laggard":
            med = sub["ret5"].median()
            lag = sub[sub["ret5"] <= med]              # 只买概念内滞涨的
            if len(lag) < 2:
                lag = sub
        else:
            lag = sub
        lag["s_ret"] = lag["ret5"].rank(pct=True)
        lag["s_vr"] = lag["vr"].rank(pct=True)
        lag["s_amt"] = lag["amount20"].rank(pct=True)
        if args.stock_mode == "cross":
            lag = lag[(lag["cross_ma"]) & (lag["vr"] >= 1.5) & (lag["ret"] >= 0.02)]
            if lag.empty:
                continue
        if args.stock_mode == "cross":
            lag["s_day"] = lag["ret"].rank(pct=True)
            lag["stock_score"] = 0.40 * lag["s_vr"] + 0.35 * lag["s_day"] + 0.25 * lag["s_amt"]
        elif args.stock_mode == "ignition":
            lag["s_day"] = lag["ret"].rank(pct=True)
            lag["stock_score"] = 0.50 * lag["s_vr"] + 0.30 * lag["s_day"] + 0.20 * lag["s_amt"]
        else:
            dir_ = -1 if args.stock_mode == "laggard" else 1
            lag["stock_score"] = 0.40 * (dir_ * (lag["s_ret"] - 0.5) + 0.5) + 0.30 * lag["s_vr"] + 0.30 * lag["s_amt"]
        picks.append(lag.nlargest(args.per_concept, "stock_score"))
    if not picks:
        print(f"[concept-arm] {day.date()} 无合格标的")
        return 0
    out = (pd.concat(picks, ignore_index=True)
           .sort_values("stock_score", ascending=False)
           .drop_duplicates("stock_code")                 # 同一只票可能属于多个概念，只留分最高的一次
           .nlargest(args.top_stocks, "stock_score").reset_index(drop=True))

    b = pd.read_parquet(ROOT / "assets/data/meta/cn_benchmark_daily.parquet", columns=["ts_code", "trade_date", "close"])
    b = b[b.ts_code == "000905.SH"].copy(); b["trade_date"] = pd.to_datetime(b["trade_date"])
    b = b.sort_values("trade_date"); b["ma20"] = b["close"].rolling(20).mean()
    row = b[b["trade_date"] <= day].iloc[-1]
    regime_ok = bool(row["close"] > row["ma20"])

    out["trade_date"] = day.date()
    out["rank"] = out.index + 1
    out["name"] = out["name"].fillna("")
    # 与回测口径一致：中证500 在 MA20 下方时不开新仓（权重记 0，名单仍保留供观察）
    out["target_weight"] = 0.0 if not regime_ok else round(1.0 / max(len(out), 1), 4)
    out["concept_heat"] = out["concept"].map(picked_concepts.set_index("concept")["heat"]).round(3)
    out["concept_rise"] = out["concept"].map(picked_concepts.set_index("concept")["rise"]).round(3)
    out["regime_ok"] = regime_ok
    out = out.rename(columns={"stock_score": "score", "n": "listed_days"})
    keep = ["trade_date", "rank", "stock_code", "name", "concept", "industry", "score", "target_weight",
            "close", "ret5", "vr", "amount20", "listed_days", "concept_heat", "concept_rise", "regime_ok"]
    out["industry"] = out.get("industry", "")
    out[keep].to_csv(OUT, index=False)
    out[keep].to_csv(HIST / f"{day.date()}.csv", index=False)
    print("concepts:", picked_concepts[["concept", "heat", "rise", "emergence"]].round(3).to_dict("records"))
    print(out[["rank", "stock_code", "name", "concept", "ret5", "vr", "regime_ok"]].round(3).to_string(index=False))
    (ROOT / "output/results_cn/concept_watch_arm_status.json").write_text(
        json.dumps({"date": str(day.date()), "status": "ok" if regime_ok else "regime_off",
                    "regime_ok": regime_ok, "n": int(len(out))}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"→ {OUT}（{len(out)} 只，regime_ok={regime_ok}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
