#!/usr/bin/env python3
"""P2.77-A：概念状态 → 总仓位（overlay）。

状态量（t 日收盘）：
  n_rising         = rise≥0.8 且 广度≥50% 的概念数
  breadth_median   = 全部概念广度中位数
  heat_concentration = 前 10 概念成交额占比
仓位（t+1 生效）：
  n_rising≥5 且 breadth_median>0.55 → 1.0
  1≤n_rising<5 或 breadth_median>0.45 → 0.7
  否则 → 0.3

应用于：概念策略日净值（strong 无 regime 版）、中证500。
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
          "人形机器人": "机器人", "机器人概念": "机器人", "具身智能": "机器人", "智能机器": "机器人",
          "CPO": "光通信", "光模块": "光通信", "存储芯片": "半导体", "华为海思": "半导体"}
NON_THEME = ("基金重仓", "社保重仓", "QFII", "券商重仓", "保险重仓", "融资融券", "深股通", "沪股通",
             "MSCI", "标普", "富时", "转债标的", "预盈预增", "参股金融", "参股券商", "破净股",
             "次新股", "超大盘", "大盘", "中盘", "小盘", "基金", "重仓", "央企50", "国企50",
             "上证50", "沪深300", "中证100", "行业龙头")


def members() -> pd.DataFrame:
    m = pd.read_parquet(ROOT / "assets/data/meta/cn_board_membership.parquet")
    m = m[m.taxonomy == "sina_concept"][["board", "code"]].rename(columns={"board": "concept"})
    m["stock_code"] = m["code"].map(lambda x: x + (".SH" if str(x).startswith(("6", "9")) else ".SZ"))
    f = ROOT / "assets/data/derived/cn_concept_members_ifind.parquet"
    frames = [m[["concept", "stock_code"]]]
    if f.exists():
        d = pd.read_parquet(f)
        frames.append(d[d.snapshot_date == d.snapshot_date.max()][["concept", "stock_code"]])
    out = pd.concat(frames, ignore_index=True).drop_duplicates()
    return out[~out["concept"].astype(str).str.contains("|".join(NON_THEME), na=False)]


def state_series(start: str) -> pd.DataFrame:
    files = sorted(glob.glob(str(ROOT / "assets/data/clean/ohlcv/market=CN/exchange=*/"
                                    "asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet")))
    bars = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "close", "amount"]).to_pandas()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"]).dt.normalize()
    bars = bars[bars["trade_date"] >= pd.Timestamp(start)].drop_duplicates(["trade_date", "stock_code"], keep="last")
    bars = bars.sort_values(["stock_code", "trade_date"])
    bars["ret"] = bars.groupby("stock_code", sort=False)["close"].pct_change()
    panel = bars.merge(members(), on="stock_code")
    c = panel.groupby(["concept", "trade_date"]).agg(
        chg=("ret", "mean"), breadth=("ret", lambda s: float((s > 0).mean())),
        n=("ret", "size"), amount=("amount", "sum")).reset_index()
    c = c[c["n"] >= 30].copy()
    c["amount_share"] = c["amount"] / c.groupby("trade_date")["amount"].transform("sum")
    zs = lambda s: (s - s.mean()) / (s.std(ddof=0) + 1e-9)
    c["z_chg"] = c.groupby("trade_date")["chg"].transform(zs)
    c["z_breadth"] = c.groupby("trade_date")["breadth"].transform(zs)
    c["z_amt"] = c.groupby("trade_date")["amount_share"].transform(zs)
    c["heat"] = 0.45 * c["z_chg"] + 0.35 * c["z_breadth"] + 0.20 * c["z_amt"]
    c = c.sort_values(["concept", "trade_date"])
    c["rise"] = c["heat"] - c.groupby("concept")["heat"].transform(lambda s: s.shift(1).rolling(5, min_periods=3).mean())
    c["family"] = c["concept"].map(lambda x: FAMILY.get(x, x))
    fam = (c.sort_values("heat", ascending=False).drop_duplicates(["trade_date", "family"]))
    st = fam.groupby("trade_date").apply(lambda x: pd.Series({
        "n_rising": int(((x["rise"] >= 0.8) & (x["breadth"] >= 0.50)).sum()),
        "breadth_median": float(x["breadth"].median()),
        "heat_concentration": float(x["amount_share"].nlargest(10).sum()),
    }), include_groups=False).reset_index()
    return st.sort_values("trade_date")


def exposure(st: pd.DataFrame) -> pd.Series:
    e = np.where((st["n_rising"] >= 5) & (st["breadth_median"] > 0.55), 1.0,
                 np.where((st["n_rising"] >= 1) | (st["breadth_median"] > 0.45), 0.7, 0.3))
    return pd.Series(e, index=st["trade_date"]).shift(1).fillna(0.7)


def stats(r: pd.Series) -> dict:
    nav = (1 + r).cumprod()
    yrs = len(r) / 252
    return {"cagr": round((nav.iloc[-1] ** (1 / yrs) - 1) * 100, 1),
            "vol": round(r.std() * np.sqrt(252) * 100, 1),
            "dd": round((nav / nav.cummax() - 1).min() * 100, 1),
            "sharpe": round(r.mean() / r.std() * np.sqrt(252), 2) if r.std() else None}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    st = state_series("2024-01-01")
    expo = exposure(st)
    st["exposure"] = st["trade_date"].map(expo)

    nav = pd.read_csv(ROOT / "output/research/p2_concept_v2/strong_nav_base.csv", parse_dates=["trade_date"])
    nav = nav.set_index("trade_date")
    r = nav["nav"].pct_change().dropna()
    e = expo.reindex(r.index).ffill().fillna(0.7)

    b = pd.read_parquet(ROOT / "assets/data/meta/cn_benchmark_daily.parquet", columns=["ts_code", "trade_date", "close"])
    b = b[b.ts_code == "000905.SH"].copy(); b["trade_date"] = pd.to_datetime(b["trade_date"])
    b = b.set_index("trade_date")["close"].sort_index()
    rb = b.pct_change().reindex(r.index).fillna(0)
    ma20 = b.rolling(20).mean()
    regime = (b > ma20).reindex(r.index).shift(1).ffill().astype(float)

    rows = {
        "strategy": stats(r),
        "strategy_x_concept_overlay": stats(r * e),
        "strategy_x_ma20": stats(r * regime.fillna(1.0)),
        "strategy_x_both(min)": stats(r * np.minimum(e, regime.fillna(1.0))),
        "csi500": stats(rb),
        "csi500_x_concept_overlay": stats(rb * e),
    }
    payload = {"n_days": int(len(r)), "avg_exposure": round(float(e.mean()), 2),
               "exposure_changes": int((e.diff().abs() > 1e-9).sum()),
               "state_summary": st[["n_rising", "breadth_median", "heat_concentration"]].describe().round(3).to_dict(),
               "results": rows}
    (OUT / "concept_overlay.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    st.to_csv(OUT / "concept_state.csv", index=False)
    print(json.dumps({k: v for k, v in rows.items()}, ensure_ascii=False, indent=1))
    print("avg exposure", payload["avg_exposure"], "changes", payload["exposure_changes"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
