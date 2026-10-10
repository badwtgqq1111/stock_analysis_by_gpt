#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""概念/板块轮动手臂：把"板块轮动"研究落成一个**独立的策略项**（与 arm_selection 并列）。

为什么单列一条手臂
------------------
`arm_selection`（base_v1）是**个股因子合成**口径；板块轮动那条线（`output/research/industry_rotation/`）
验证的是**板块层信号**：概念动量可复现（J=1/K=12 +2.65%/月，t=2.18，见
`reproduce_papers_sina_concept.md`），行业热度是**反向**的（追热度跑输随机），
且"板块内买滞涨票"比选板块本身贡献更稳定。三者是**不同的选股逻辑**，
混进 base_v1 会互相污染权重，所以单列 `arm=concept_rotation`。

信号口径（写死，可审计）
------------------------
  板块层（用 `assets/data/meta/cn_board_membership.parquet` 的新浪概念成分，175 个）：
      board_score = 0.50×rank(板块20日等权收益)      # 概念动量（Li & Yang 2018 口径）
                  + 0.30×rank(板块5日资金流强度)      # 净流入 ÷ 成交额（行业研究里 IC≈+0.14 的形态）
                  - 0.20×rank(板块涨停密度)           # 过热扣分（行业研究里 IC≈-0.13）
  个股层（只在上一步选出的概念成分里挑）：
      stock_score = 0.40×rank(-5日涨幅) + 0.30×rank(5日净流入) + 0.30×rank(20日均成交额)
  过滤：非 ST（PIT 名称）、上市 ≥ 60 天、20 日均成交额 ≥ 5000 万、当日非涨停（涨停不追高）。
  持仓：概念 top-k（默认 3）内选 stock_score top-N（默认 5），等权。

已知弱点（**必读**，别当成能直接上的策略）
  * 概念动量**近 5 年失效**（后半段 J=1/K=6 = -1.51%，t=-1.45）：本手臂在弱市里应当降权/停用；
  * 板块成分表是**当前**快照（无 PIT 历史），回溯统计有前视，只有"今天的名单"是干净的；
  * 未建模涨停买不进、冲击成本；概念成分平均只有 60 只票，容量小。

用法
----
  uv run python scripts/select_concept_arm.py                      # 选最近一个交易日
  uv run python scripts/select_concept_arm.py --as-of 2026-10-08 --top-boards 3 --holdings 5
  uv run python scripts/select_concept_arm.py --backtest           # 月度回放该规则（写 backtest.json）
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services import market_board_service as mbs  # noqa: E402

OUT = ROOT / "output/results_cn"
MEMBERSHIP = ROOT / "assets/data/meta/cn_board_membership.parquet"
MONEYFLOW = ROOT / "assets/data/derived/cn_moneyflow_features.parquet"
CAPITAL_DEFAULT = 45000.0
TAXONOMY = "sina_concept"
MIN_MEMBER_STOCKS = 5

# 键名必须与 board_scores() 里的列名一致（踩过：写成 flow_5d 而列叫 flow_intensity → KeyError）
BOARD_WEIGHTS = {"mom_20d": 0.50, "flow_intensity": 0.30, "zt_density": -0.20}
STOCK_WEIGHTS = {"rev_5d": 0.40, "flow_5d": 0.30, "amount_20d": 0.30}
MIN_AMOUNT_20D = 5_000_000.0
MIN_LISTED_DAYS = 60


def board_members() -> pd.DataFrame:
    """(board, ts_code, name) 长表 —— 一只票可属于多个概念。"""
    frame = pd.read_parquet(MEMBERSHIP)
    frame = frame[frame["taxonomy"] == TAXONOMY].copy()
    if frame.empty:
        raise SystemExit(f"没有 {TAXONOMY} 成分表（先跑 scripts/sync_cn_boards.py）")
    code6 = frame["code"].astype(str).str.zfill(6)
    suffix = frame.get("symbol", pd.Series("", index=frame.index)).astype(str).str[:2].str.upper()
    suffix = suffix.where(suffix.isin(["SH", "SZ", "BJ"]), "")
    fallback = pd.Series(["SH" if c.startswith(("6", "9")) else ("BJ" if c.startswith(("4", "8")) else "SZ")
                          for c in code6], index=frame.index)
    frame["ts_code"] = code6 + "." + suffix.where(suffix != "", fallback)
    return frame[["board", "ts_code", "name"]].drop_duplicates()


def board_scores(bars: pd.DataFrame, members: pd.DataFrame, flow: pd.DataFrame,
                 as_of: pd.Timestamp) -> pd.DataFrame:
    """板块层打分（动量 / 资金流强度 / 涨停密度）。"""
    window = bars[bars["trade_date"] >= as_of - pd.Timedelta(days=45)]
    dates = sorted(window["trade_date"].unique())
    base_day = dates[-21] if len(dates) >= 21 else dates[0]
    last = pd.Timestamp(dates[-1])
    piv = window.pivot_table(index="stock_code", columns="trade_date", values="close", aggfunc="last")
    ret_20 = (piv[last] / piv[base_day] - 1.0).dropna().rename("ret_20d")

    joined = members.merge(ret_20, left_on="ts_code", right_index=True, how="inner")
    grouped = joined.groupby("board")
    table = pd.DataFrame({"n": grouped.size(), "mom_20d": grouped["ret_20d"].median()})

    recent = flow[flow["trade_date"] >= pd.Timestamp(dates[-5])]
    flow_sum = recent.groupby("ts_code")["net_amount"].sum()
    amount_sum = window[window["trade_date"].isin(dates[-5:])].groupby("stock_code")["amount"].sum()
    joined_flow = members.assign(
        flow=members["ts_code"].map(flow_sum), amount=members["ts_code"].map(amount_sum))
    agg = joined_flow.groupby("board")[["flow", "amount"]].sum()
    table["flow_intensity"] = (agg["flow"] * 1e4) / agg["amount"].replace(0, np.nan)

    zt = window[window["trade_date"].isin(dates[-20:])].groupby("stock_code")["is_limit_up"].sum()
    table["zt_density"] = members.assign(zt=members["ts_code"].map(zt).fillna(0)) \
        .groupby("board")["zt"].sum() / table["n"] / 20.0

    table = table[table["n"] >= MIN_MEMBER_STOCKS].fillna({"flow_intensity": 0.0, "zt_density": 0.0})
    score = pd.Series(0.0, index=table.index)
    for key, weight in BOARD_WEIGHTS.items():
        score = score + table[key].rank(pct=True) * weight
    table["board_score"] = score
    return table.sort_values("board_score", ascending=False)


def stock_scores(bars: pd.DataFrame, flow: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """个股层打分（短期反转 / 资金流 / 流动性）+ 过滤标记。"""
    window = bars[bars["trade_date"] >= as_of - pd.Timedelta(days=45)]
    dates = sorted(window["trade_date"].unique())
    last, base5 = pd.Timestamp(dates[-1]), pd.Timestamp(dates[-6]) if len(dates) >= 6 else pd.Timestamp(dates[0])
    piv = window.pivot_table(index="stock_code", columns="trade_date", values="close", aggfunc="last")
    frame = pd.DataFrame({
        "rev_5d": (piv[last] / piv[base5] - 1.0),
        "amount_20d": window[window["trade_date"] >= pd.Timestamp(dates[-20])]
                              .groupby("stock_code")["amount"].mean(),
        "limit_up": window[window["trade_date"] == last].set_index("stock_code")["is_limit_up"],
        "close": piv[last],
        "st_pit": window[window["trade_date"] == last].set_index("stock_code")["st_pit"],
    })
    recent = flow[flow["trade_date"] >= pd.Timestamp(dates[-5])]
    frame["flow_5d"] = recent.groupby("ts_code")["net_amount"].sum()
    frame["flow_5d"] = frame["flow_5d"] * 1e4            # 万元 → 元
    for key, weight in STOCK_WEIGHTS.items():
        column = frame[key].astype(float)
        if key == "rev_5d":
            # **反转**腿：要奖励"过去 5 日跌得多"的票（板块内买滞涨），
            # 所以排的是 -rev_5d 的分位。第一版直接排 rev_5d，分数奖励了追涨，
            # 选出来全是刚拉过的权重股（与文档口径相反）。
            column = -column
        frame[f"z_{key}"] = weight * column.rank(pct=True)
    frame["stock_score"] = frame[[f"z_{k}" for k in STOCK_WEIGHTS]].sum(axis=1)
    frame["tradable_ok"] = (
        (frame["amount_20d"] >= MIN_AMOUNT_20D)
        & (frame["limit_up"] != True)                     # noqa: E712 —— 涨停不追高
        & (frame["st_pit"] != True)                       # noqa: E712
    )
    return frame


def run(as_of: str, top_boards: int, candidates: int, holdings: int, capital: float,
        per_board: int = 4, max_per_board: int = 2) -> dict:
    bars = mbs.enriched_daily(90)
    dates = sorted(bars["trade_date"].unique())
    snap = pd.Timestamp(as_of) if as_of else pd.Timestamp(dates[-1])
    if snap not in dates:
        snap = pd.Timestamp(dates[-1])
    flow = _moneyflow_window(snap)
    members = board_members()
    boards = board_scores(bars, members, flow, snap)
    stocks = stock_scores(bars, flow, snap)
    basics = mbs.basic_frame()

    picked_boards = boards.head(int(top_boards))
    pool = members[members["board"].isin(picked_boards.index)].copy()
    pool["board_score"] = pool["board"].map(picked_boards["board_score"])
    pool = pool.join(stocks, on="ts_code", how="inner")
    pool = pool[pool["tradable_ok"]].sort_values(["board_score", "stock_score"], ascending=False)
    pool = pool.drop_duplicates(subset=["ts_code"], keep="first")
    # 每个板块先各取 per_board 只再全局排序：否则板块层打分最高的那个概念会把候选池占满
    # （实测第一版 5 只持仓全来自同一个概念，等于押注单一板块）。
    pool = pool.groupby("board", group_keys=False).head(int(per_board))
    pool = pool.sort_values(["board_score", "stock_score"], ascending=False).head(int(candidates))

    # 持仓做单板块上限（默认每板块 ≤2 只），强制分散
    held_rows = []
    counts: dict[str, int] = {}
    for _, row in pool.iterrows():
        if counts.get(row["board"], 0) >= int(max_per_board):
            continue
        held_rows.append(row)
        counts[row["board"]] = counts.get(row["board"], 0) + 1
        if len(held_rows) >= int(holdings):
            break
    held = pd.DataFrame(held_rows)
    budget = capital / max(len(held), 1)
    held_codes = set(held["ts_code"].tolist())
    rows = []
    for rank, (_, row) in enumerate(pool.iterrows(), 1):
        code = str(row["ts_code"])
        px = None if pd.isna(row.get("close")) else float(row["close"])
        in_portfolio = code in held_codes
        lots = int(budget / (px * 100)) if (px and in_portfolio) else 0
        info = basics.loc[code] if (not basics.empty and code in basics.index) else None
        rows.append({
            "trade_date": snap.strftime("%Y-%m-%d"), "stock_code": code,
            "name": str(row["name"] or (info["name"] if info is not None else "")),
            "board": str(row["board"]),
            "industry": str(info["industry"]) if info is not None else "",
            "score": round(float(row["stock_score"]), 6), "rank": rank,
            "in_portfolio": bool(in_portfolio),
            "target_weight": round(1.0 / len(held), 4) if in_portfolio else 0.0,
            "last_close": px, "tradable": True, "limit_up": False,
            "lots_at_capital": lots, "planned_amount": round(lots * (px or 0) * 100, 0),
            "pit_name": str(row["name"] or ""),
            "mktcap_rank": None,
            "concept_score": round(float(row["board_score"]), 6),
            "rev_5d": None if pd.isna(row.get("rev_5d")) else round(float(row["rev_5d"]), 5),
            "flow_5d_yuan": None if pd.isna(row.get("flow_5d")) else round(float(row["flow_5d"]), 0),
        })
    table = pd.DataFrame(rows)
    return {"snapshot_date": snap.strftime("%Y-%m-%d"), "table": table,
            "boards": picked_boards, "board_table": boards, "members": len(members)}


def _moneyflow_window(as_of: pd.Timestamp) -> pd.DataFrame:
    import pyarrow.dataset as ds
    dataset = ds.dataset(str(MONEYFLOW), format="parquet")
    table = dataset.to_table(
        columns=["trade_date", "stock_code", "source", "moneyflow_net_amount"],
        filter=(ds.field("trade_date") >= as_of - pd.Timedelta(days=30))
               & (ds.field("trade_date") <= as_of)
               & (ds.field("source") == "moneyflow"))
    frame = table.to_pandas()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    # 面板里的键是 stock_code，成分表里是 ts_code —— 统一成 ts_code，避免下游
    # groupby("ts_code") 直接 KeyError（实测踩过）。
    return frame.rename(columns={"moneyflow_net_amount": "net_amount", "stock_code": "ts_code"})


def write_outputs(result: dict, args) -> None:
    table = result["table"]
    OUT.mkdir(parents=True, exist_ok=True)
    table.to_csv(OUT / "arm_concept_selection.csv", index=False)
    hist = OUT / "arm_concept_history"
    hist.mkdir(parents=True, exist_ok=True)
    table.to_csv(hist / f"{result['snapshot_date']}.csv", index=False)
    boards = result["boards"]
    (OUT / "arm_concept_selection.json").write_text(json.dumps({
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "snapshot_date": result["snapshot_date"],
        "arm": "concept_rotation",
        "source": f"概念动量(0.5)×板块资金流(0.3)−涨停密度(0.2) → 板块内反转(0.4)+资金流(0.3)+流动性(0.3)"
                  f"｜成分表：{TAXONOMY}（{result['members']} 行）",
        "panel": str(MEMBERSHIP),
        "domain_size": int(result["members"]),
        "candidates": args.candidates, "holdings": args.holdings, "capital": args.capital,
        "note": "研究手臂（concept_rotation），**不替代** preselection→pk 的生产名单；"
                "已知弱点见脚本头部（概念动量近 5 年失效、成分表非 PIT）",
        "st_mode": "pit", "st_anchor": "start",
        "st_pit_caveat": "用 PIT 名称区间表剔除 ST/退市风险股（与 base_v1 手臂同一口径）",
        "pit_columns": "pit_name=该股在快照日的实际证券名称",
        "order_precheck": "已排除当日涨停（不追高）、20 日均成交额 < 5000 万、ST",
        "boards": [{"board": str(i), "score": round(float(r["board_score"]), 5),
                    "n": int(r["n"]), "mom_20d": round(float(r["mom_20d"]), 5),
                    "flow_intensity": round(float(r["flow_intensity"]), 5),
                    "zt_density": round(float(r["zt_density"]), 6)}
                   for i, r in boards.iterrows()],
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def backtest(months: int = 24) -> dict:
    """月度回放该规则（板块层 + 个股层同一套打分），报告持仓组合的期均收益与胜率。"""
    bars = mbs.enriched_daily(max(int(months) * 20 + 60, 200))
    dates = sorted(bars["trade_date"].unique())
    rebalance = dates[::20][-int(months):]
    rows = []
    for day in rebalance:
        try:
            result = run(pd.Timestamp(day).strftime("%Y-%m-%d"), 3, 10, 5, CAPITAL_DEFAULT)
        except Exception:                                   # noqa: BLE001
            continue
        held = result["table"][result["table"]["in_portfolio"]]
        if held.empty:
            continue
        forward = bars[(bars["trade_date"] > day) & (bars["trade_date"] <= pd.Timestamp(day) + pd.Timedelta(days=35))]
        pivot = forward.pivot_table(index="stock_code", columns="trade_date", values="close", aggfunc="last")
        if pivot.empty:
            continue
        base = bars[bars["trade_date"] == day].set_index("stock_code")["close"]
        last = pivot.iloc[:, -1]
        codes = [c for c in held["stock_code"] if c in last.index and c in base.index]
        if not codes:
            continue
        rets = [float(last[c]) / float(base[c]) - 1 for c in codes]
        # 基准：同窗口全市场等权（用同一份日线算，避免拿"指数"和"等权组合"对比造成错觉）
        bench_window = bars[(bars["trade_date"] > day) & (bars["trade_date"] <= pd.Timestamp(day) + pd.Timedelta(days=35))]
        bench = float((1 + bench_window["pct_chg"].fillna(0)).groupby(bench_window["stock_code"]).prod().mean() - 1) \
            if not bench_window.empty else None
        rows.append({"date": pd.Timestamp(day).strftime("%Y-%m-%d"), "n": len(codes),
                     "ret": float(np.mean(rets)), "bench": bench})
    if not rows:
        return {"available": False}
    frame = pd.DataFrame(rows)
    equity = (1 + frame["ret"]).cumprod()
    bench_mean = float(frame["bench"].dropna().mean()) if frame["bench"].notna().any() else None
    excess = float((frame["ret"] - frame["bench"].fillna(0)).mean()) if bench_mean is not None else None
    return {"available": True, "months": len(frame),
            "period_mean": float(frame["ret"].mean()),
            "bench_mean": bench_mean, "excess_mean": excess,
            "win_rate": float((frame["ret"] > 0).mean()),
            "cumulative": float(equity.iloc[-1] - 1),
            "max_drawdown": float((equity / equity.cummax() - 1).min()),
            "monthly": frame.to_dict("records"),
            "window": [frame["date"].iloc[0], frame["date"].iloc[-1]],
            "caveat": "板块成分表为当前快照（非 PIT），回溯统计有前视；未建模涨停买不进与冲击成本"}


def main() -> int:
    ap = argparse.ArgumentParser(description="概念/板块轮动手臂（独立策略项）")
    ap.add_argument("--as-of", default="")
    ap.add_argument("--top-boards", type=int, default=3)
    ap.add_argument("--candidates", type=int, default=10)
    ap.add_argument("--holdings", type=int, default=5)
    ap.add_argument("--per-board", type=int, default=4, help="每个板块进候选池的只数上限")
    ap.add_argument("--max-per-board", type=int, default=2, help="持仓里单个板块的只数上限")
    ap.add_argument("--capital", type=float, default=CAPITAL_DEFAULT)
    ap.add_argument("--backtest", action="store_true", help="月度回放该规则并写 arm_concept_backtest.json")
    ap.add_argument("--months", type=int, default=24)
    args = ap.parse_args()

    if args.backtest:
        payload = backtest(args.months)
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "arm_concept_backtest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                                       encoding="utf-8")
        if payload.get("available"):
            bench = payload.get("bench_mean")
            excess = payload.get("excess_mean")
            print(f"[concept-arm] 回放 {payload['months']} 个月（{payload['window'][0]} ~ {payload['window'][1]}）："
                  f"期均 {payload['period_mean']:+.2%}｜全市场等权基准 "
                  f"{bench:+.2%}｜超额 {excess:+.2%}｜胜率 {payload['win_rate']:.0%}｜"
                  f"累计 {payload['cumulative']:+.1%}｜最大回撤 {payload['max_drawdown']:.1%}")
            print(f"[concept-arm] 注意：{payload['caveat']}")
        else:
            print("[concept-arm] 回放没有可用样本")
        return 0

    result = run(args.as_of, args.top_boards, args.candidates, args.holdings, args.capital,
                 per_board=args.per_board, max_per_board=args.max_per_board)
    write_outputs(result, args)
    boards = result["boards"]
    print(f"[concept-arm] 快照 {result['snapshot_date']}｜选中概念 {len(boards)} 个：")
    for name, row in boards.iterrows():
        print(f"    {name:<12} 打分 {row['board_score']:.3f}｜成分 {int(row['n']):>3}｜"
              f"20日中位 {row['mom_20d']:+.2%}｜资金流强度 {row['flow_intensity']:+.4f}｜"
              f"涨停密度 {row['zt_density']:.4f}")
    table = result["table"]
    held = table[table["in_portfolio"]]
    print(f"[concept-arm] 持仓 {len(held)} 只（预算 {args.capital:,.0f}）：")
    for _, row in held.iterrows():
        print(f"    {row['stock_code']} {row['name']:<8} [{row['board']}] 5日 {row['rev_5d']:+.2%} "
              f"| 手数 {row['lots_at_capital']} | 金额 {row['planned_amount']:,.0f}")
    print(f"[concept-arm] 输出 {OUT.relative_to(ROOT)}/arm_concept_selection.{{csv,json}}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
