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
  候选：板块上限 4 只/板块 + **行业上限 2 只/行业**（治"候选里一堆银行"）；
  持仓：概念 top-k（默认 3）内选 stock_score top-N（默认 5），单板块 ≤2，等权。

口径由数据说话（2026-10-10）
---------------------------
三套板块口径 × 四套个股口径 = 12 组，同一窗口月度回放（`--compare-grid`）：

| 板块口径 | 个股口径 | 期均 | 超额 | 胜率 | 累计 | 回撤 |
|---|---|---|---|---|---|---|
| no_flow（现默认） | reversal | +2.48% | **+0.52%** | 59% | +57.0% | -15.4% |
| flow（旧） | reversal | +1.66% | -0.30% | 68% | +34.9% | -20.9% |
| flow | leader_5d | +1.51% | -0.79% | 50% | +34.7% | -17.7% |
| no_flow | cap_leader | +1.50% | -0.79% | 64% | +33.0% | -16.8% |
| … | … | | | | | |
| flow | leader | +0.92% | -1.38% | 55% | +19.4% | -14.7% |

结论（用户问"应该选行业龙头或者次龙头么？"）：
  * **不采用**。三种龙头定义（20 日强度 / 5 日强度 / 流通市值）在**同一板块口径下**
    全部比"买滞涨"差 0.5~1.1 个百分点/月，12 组里没有一个龙头组合跑赢基准；
  * 但"选出一堆银行"的抱怨成立，根因在**板块层的聚合资金流项**，去掉它才由 -0.30% 转为 +0.52%；
  * 所以正确的改动是「去资金流项 + 行业上限」，不是「改买龙头」。

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
# 板块层：选哪些板块。`flow_intensity` 是**板块聚合**口径（净流入÷成交额），
# 体量越大越占便宜 —— 银行主导的 超大盘/央企50 因此天然最高，但它们既没动量也没涨停。
# 所以这里给出三套口径，用同一份数据回放比较（见 BOARD_MODE_LABELS）。
BOARD_MODES: dict[str, dict[str, float]] = {
    # 旧口径：动量 + 资金流 − 涨停密度
    "flow": {"mom_20d": 0.50, "flow_intensity": 0.30, "zt_density": -0.20},
    # 去资金流：只留动量与涨停密度（资金流项是"巨型风格板块"占便宜的来源）
    "no_flow": {"mom_20d": 0.70, "zt_density": -0.30},
    # 热度口径：动量 + 涨停密度（真正"在风口上"的板块；最贴近行业热力图里红的那一片）
    "hot": {"mom_20d": 0.60, "zt_density": 0.40},
    # 纯动量（隔离实验）：只留动量项，用来判断"动量本身到底有没有贡献"。
    # 2026-10-10 行业动量研究（output/research/industry_momentum/REPORT.md）显示板块/行业层面
    # 动量 IC 为负 → 若 mom_only 的超额 ≈0 或为负，说明 no_flow 的正超额来自**涨停密度惩罚项**
    # （"避开刚被爆炒的板块"）而不是动量。
    "mom_only": {"mom_20d": 1.00},
}
# 默认口径 = `no_flow`（2026-10-10 由 12 组网格回放定下来，不是拍脑袋）：
#   12 组（3 个板块口径 × 4 个股口径）里**只有 `no_flow × reversal` 跑赢全市场等权**
#   （月均超额 +0.52%，累计 +57.0%，回撤 -15.4%）；旧口径 `flow × reversal` 是 -0.30%。
#   去掉板块**聚合资金流**项是唯一的正贡献来源 —— 该项按 净流入÷成交额 打分，体量越大越占便宜，
#   于是银行主导的「超大盘/央企50」这类风格板块天然第一（这正是"选出一堆银行"的根因）。
DEFAULT_BOARD_MODE = "no_flow"
BOARD_WEIGHTS = BOARD_MODES[DEFAULT_BOARD_MODE]                    # 兼容旧引用

# 个股层（板块内）选谁 —— 四套口径，权重**带符号**（正=越大越好，负=反向）。
#
# 为什么要这个开关（2026-10-10 用户提问："行业选股里怎么选了一堆银行啊，
# 应该选行业龙头或者次龙头么？"）：
#   实测诊断：板块层选出的是 超大盘 / 央企50 这类**风格板块**，它们上榜靠的是
#   `flow_intensity`（银行体量大 → 板块净流入÷成交额天然最高），而 20 日中位收益≈0、
#   涨停密度=0（既没热度也没强度）。再叠加旧口径 `reversal`
#   （0.4×rank(-5日涨幅) + 0.3×rank(资金流) + 0.3×rank(成交额)），就等于在
#   "资金流最强的巨型板块"里专挑"最大、最没涨、最有钱"的票 —— 工行/农行/中行/中信
#   三项全拉满，所以候选里一堆银行。这是**规则设计**的结果，不是数据错。
#   四套口径用同一份数据回放（`--backtest --stock-mode X`）比收益，不靠直觉吵架。
STOCK_MODES: dict[str, dict[str, float]] = {
    # 板块内买**滞涨**（旧口径；行业研究里"板块内买滞涨票"比选板块本身贡献更稳定）
    "reversal": {"rev_5d": -0.40, "flow_5d": 0.30, "amount_20d": 0.30},
    # 板块内买**龙头**：20 日强度 + 资金流 + 流动性
    "leader": {"mom_20d": 0.40, "flow_5d": 0.30, "amount_20d": 0.30},
    # **短期龙头**：用 5 日强度当龙头度（更贴近"板块启动就买最强的那只"的直觉）
    "leader_5d": {"rev_5d": 0.40, "flow_5d": 0.30, "amount_20d": 0.30},
    # **市值龙头**：直接买板块里最大的那只（看热力图时最直觉的"行业龙头"）
    "cap_leader": {"mktcap": 0.50, "mom_20d": 0.30, "flow_5d": 0.20},
}
DEFAULT_STOCK_MODE = "reversal"
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
                 as_of: pd.Timestamp, mode: str = DEFAULT_BOARD_MODE) -> pd.DataFrame:
    """板块层打分（动量 / 资金流强度 / 涨停密度），权重由 `mode` 决定。"""
    # ⚠️ 上界必须有（2026-10-15 修）：原来只切下界 `>= as_of - 45d`，而窗口是"锚在今天的最近 N 天"
    # ⇒ 窗口实际是 [as_of-45d, **今天**]，后面所有 `dates[-21] / dates[-5:] / dates[-1]` 都取到今天，
    # 于是不同历史日期算出的分数完全一样（实测 5 个日期跑出同一批票 = 前视）。
    window = bars[(bars["trade_date"] >= as_of - pd.Timedelta(days=45))
                  & (bars["trade_date"] <= as_of)]
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
    for key, weight in BOARD_MODES[mode].items():
        score = score + table[key].rank(pct=True) * weight
    table["board_score"] = score
    return table.sort_values("board_score", ascending=False)


def _mktcap_lookup() -> dict:
    """流通市值（亿元）—— 复用行业热度服务那份快照，保证页面上和选股用的是同一口径。"""
    try:
        from backend.services import industry_heat_service as ihs
        return ihs._mktcap_lookup()[0]                            # noqa: SLF001
    except Exception:                                            # noqa: BLE001
        return {}


def stock_scores(bars: pd.DataFrame, flow: pd.DataFrame, as_of: pd.Timestamp,
                 mode: str = DEFAULT_STOCK_MODE) -> pd.DataFrame:
    """个股层打分（板块内选谁）+ 过滤标记。

    `mode` 决定用哪套权重（见 `STOCK_MODES` 的注释）：
      * `reversal`   —— 滞涨腿（旧默认）：奖励"过去 5 日跌得多"的票；
      * `leader`     —— 龙头腿（20 日强度）：买板块里最强的那只；
      * `leader_5d`  —— 短期龙头（5 日强度）；
      * `cap_leader` —— 市值龙头（流通市值最大）。
    所有分项都是**横截面分位**再加权；权重带符号，正=越大越好、负=反向。
    """
    if mode not in STOCK_MODES:
        raise SystemExit(f"未知 stock-mode={mode}；可选 {sorted(STOCK_MODES)}")
    weights = STOCK_MODES[mode]
    # ⚠️ 上界必须有（2026-10-15 修）：原来只切下界 `>= as_of - 45d`，而窗口是"锚在今天的最近 N 天"
    # ⇒ 窗口实际是 [as_of-45d, **今天**]，后面所有 `dates[-21] / dates[-5:] / dates[-1]` 都取到今天，
    # 于是不同历史日期算出的分数完全一样（实测 5 个日期跑出同一批票 = 前视）。
    window = bars[(bars["trade_date"] >= as_of - pd.Timedelta(days=45))
                  & (bars["trade_date"] <= as_of)]
    dates = sorted(window["trade_date"].unique())
    last = pd.Timestamp(dates[-1])
    base5 = pd.Timestamp(dates[-6]) if len(dates) >= 6 else pd.Timestamp(dates[0])
    base20 = pd.Timestamp(dates[-21]) if len(dates) >= 21 else pd.Timestamp(dates[0])
    piv = window.pivot_table(index="stock_code", columns="trade_date", values="close", aggfunc="last")
    frame = pd.DataFrame({
        "rev_5d": (piv[last] / piv[base5] - 1.0),
        "mom_20d": (piv[last] / piv[base20] - 1.0),
        "amount_20d": window[window["trade_date"] >= pd.Timestamp(dates[-20])]
                              .groupby("stock_code")["amount"].mean(),
        "limit_up": window[window["trade_date"] == last].set_index("stock_code")["is_limit_up"],
        "close": piv[last],
        "st_pit": window[window["trade_date"] == last].set_index("stock_code")["st_pit"],
    })
    recent = flow[flow["trade_date"] >= pd.Timestamp(dates[-5])]
    frame["flow_5d"] = recent.groupby("ts_code")["net_amount"].sum() * 1e4      # 万元 → 元
    if "mktcap" in weights:
        caps = _mktcap_lookup()
        frame["mktcap"] = frame.index.map(caps)
    # 缺少所需字段时整列置中位（不能静默当 0，否则会系统性偏向某一侧）
    for key in weights:
        if key not in frame.columns:
            frame[key] = np.nan
    for key, weight in weights.items():
        column = pd.to_numeric(frame[key], errors="coerce")
        column = column.fillna(column.median())
        frame[f"z_{key}"] = weight * column.rank(pct=True)
    frame["stock_score"] = frame[[f"z_{k}" for k in weights]].sum(axis=1)
    frame["tradable_ok"] = (
        (frame["amount_20d"] >= MIN_AMOUNT_20D)
        & (frame["limit_up"] != True)                     # noqa: E712 —— 涨停不追高
        & (frame["st_pit"] != True)                       # noqa: E712
    )
    return frame


def run(as_of: str, top_boards: int, candidates: int, holdings: int, capital: float,
        per_board: int = 4, max_per_board: int = 2, max_per_industry: int = 2,
        stock_mode: str = DEFAULT_STOCK_MODE, board_mode: str = DEFAULT_BOARD_MODE) -> dict:
    # ── as_of 感知（2026-10-15 修）──────────────────────────────────────────────
    # 事故：原来固定 `enriched_daily(90)`（= 最近 90 个交易日，锚在**今天**），
    # 而 `board_scores/stock_scores` 又按 `as_of - 45d` 过滤 ⇒ 只要 as_of 早于约 90 天，
    # 窗口里根本没有那天的数据，分数退化成同一批 ⇒ **四个不同历史日期跑出同一批票**
    # （实测 2022-06-30 / 2023-06-30 / 2024-06-28 / 2025-06-30 完全相同 = 前视）。
    # 现在：按 as_of 需要的历史长度装载窗口；找不到"≤ as_of 的交易日"就**如实报错**，
    # 绝不静默退回今天（那种静默回退就是前视本身）。
    want = pd.Timestamp(as_of).normalize() if as_of else None
    if want is not None:
        need_days = mbs.window_days_for(want)          # 覆盖到 want 所需交易日数（+2 冗余）
        # +60 个交易日：评分窗口要的是 [as_of-45d, as_of]，只装"到 as_of 为止"的天数会让窗口被截断
        # （实测：只装 315 天时窗口只剩 3 天 ⇒ dates[-5] 直接 IndexError）。
        bars = mbs.enriched_daily(max(120, min(int(need_days) + 60, 1600)))
    else:
        bars = mbs.enriched_daily(90)
    dates = sorted(bars["trade_date"].unique())
    if want is not None:
        usable = [d for d in dates if pd.Timestamp(d) <= want]
        if not usable:
            raise ValueError(f"窗口里没有 ≤ {want.date()} 的交易日（数据不够长），拒绝静默退回今天")
        snap = pd.Timestamp(usable[-1])
    else:
        snap = pd.Timestamp(dates[-1])
    flow = _moneyflow_window(snap)
    members = board_members()
    boards = board_scores(bars, members, flow, snap, mode=board_mode)
    stocks = stock_scores(bars, flow, snap, mode=stock_mode)
    basics = mbs.basic_frame()

    picked_boards = boards.head(int(top_boards))
    pool = members[members["board"].isin(picked_boards.index)].copy()
    pool["board_score"] = pool["board"].map(picked_boards["board_score"])
    pool = pool.join(stocks, on="ts_code", how="inner")
    pool = pool[pool["tradable_ok"]].sort_values(["board_score", "stock_score"], ascending=False)
    pool = pool.drop_duplicates(subset=["ts_code"], keep="first")
    # 行业归属：概念成分表里没有行业，从 basic 表补（用户看的是**行业**，概念只是选股口径）
    industry_map = basics["industry"] if not basics.empty else pd.Series(dtype=object)
    pool["industry"] = pool["ts_code"].map(industry_map).fillna("")

    # 候选池构造：板块上限 per_board + **行业上限 max_per_industry** 一起做，再取前 candidates。
    #
    # 为什么加行业上限（2026-10-10 用户："行业选股里怎么选了一堆银行啊"）：
    #   板块层选出的 `超大盘` 里有 4 只银行、`央企50` 里有 6 只银行。板块上限只管住
    #   "一个概念几只"，管不住"一个行业几只" —— 实测候选 10 只里 4 只是银行。
    #   行业上限是**分散度约束**（不改 alpha 排序，只限制同类占比），比"改成买龙头"更对症。
    pool_rows, board_counts, industry_counts = [], {}, {}
    for _, row in pool.iterrows():
        board, industry = row["board"], row["industry"]
        if board_counts.get(board, 0) >= int(per_board):
            continue
        if industry and industry_counts.get(industry, 0) >= int(max_per_industry):
            continue
        pool_rows.append(row)
        board_counts[board] = board_counts.get(board, 0) + 1
        if industry:
            industry_counts[industry] = industry_counts.get(industry, 0) + 1
        if len(pool_rows) >= int(candidates):
            break
    pool = pd.DataFrame(pool_rows)

    # 持仓：单板块上限（默认 ≤2）；行业上限已经在候选池那一层生效
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
            "industry": str(row.get("industry") or (info["industry"] if info is not None else "")),
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
            "stock_mode": stock_mode, "board_mode": board_mode,
        })
    table = pd.DataFrame(rows)
    return {"snapshot_date": snap.strftime("%Y-%m-%d"), "table": table,
            "boards": picked_boards, "board_table": boards, "members": len(members),
            "stock_mode": stock_mode, "board_mode": board_mode,
            "max_per_industry": max_per_industry}


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
        "stock_mode": result.get("stock_mode", DEFAULT_STOCK_MODE),
        "stock_mode_label": STOCK_MODE_LABELS.get(result.get("stock_mode", ""), ""),
        "board_mode": result.get("board_mode", DEFAULT_BOARD_MODE),
        "board_mode_label": BOARD_MODE_LABELS.get(result.get("board_mode", ""), ""),
        # 口径说明必须**由实际权重生成**：第一版把公式写死成字符串，默认口径换成 no_flow 之后
        # JSON 里还在报旧公式（前端会显示成"概念动量0.5×资金流0.3"），自相矛盾。
        "source": f"板块层：{BOARD_MODE_LABELS.get(result.get('board_mode', DEFAULT_BOARD_MODE), '')}"
                  f"（实际权重 {BOARD_MODES[result.get('board_mode', DEFAULT_BOARD_MODE)]}）"
                  f" → 个股层：{STOCK_MODE_LABELS.get(result.get('stock_mode', DEFAULT_STOCK_MODE), '')}"
                  f"｜候选上限 行业≤{result.get('max_per_industry', 2)}只｜成分表：{TAXONOMY}（{result['members']} 行）",
        "panel": str(MEMBERSHIP),
        "domain_size": int(result["members"]),
        "candidates": args.candidates, "holdings": args.holdings, "capital": args.capital,
        "note": "研究手臂（concept_rotation），**不替代** preselection→pk 的生产名单；"
                "已知弱点见脚本头部（概念动量近 5 年失效、成分表非 PIT）",
        "st_mode": "pit", "st_anchor": "start",
        "st_pit_caveat": "用 PIT 名称区间表剔除 ST/退市风险股（与 base_v1 手臂同一口径）",
        "pit_columns": "pit_name=该股在快照日的实际证券名称",
        "order_precheck": "已排除当日涨停（不追高）、20 日均成交额 < 5000 万、ST",
        "diversify": f"候选：每板块 ≤{getattr(args, 'per_board', 4)} 只、每行业 ≤{getattr(args, 'max_per_industry', 2)} 只；"
                     f"持仓：每板块 ≤{getattr(args, 'max_per_board', 2)} 只",
        "boards": [{"board": str(i), "score": round(float(r["board_score"]), 5),
                    "n": int(r["n"]), "mom_20d": round(float(r["mom_20d"]), 5),
                    "flow_intensity": round(float(r["flow_intensity"]), 5),
                    "zt_density": round(float(r["zt_density"]), 6)}
                   for i, r in boards.iterrows()],
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def backtest(months: int = 24, stock_mode: str = DEFAULT_STOCK_MODE,
             board_mode: str = DEFAULT_BOARD_MODE, max_per_industry: int = 2) -> dict:
    """月度回放该规则（板块层 + 个股层同一套打分），报告持仓组合的期均收益与胜率。"""
    bars = mbs.enriched_daily(max(int(months) * 20 + 60, 200))
    dates = sorted(bars["trade_date"].unique())
    rebalance = dates[::20][-int(months):]
    rows = []
    for day in rebalance:
        try:
            result = run(pd.Timestamp(day).strftime("%Y-%m-%d"), 3, 10, 5, CAPITAL_DEFAULT,
                         stock_mode=stock_mode, board_mode=board_mode,
                         max_per_industry=max_per_industry)
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
    return {"available": True, "stock_mode": stock_mode, "board_mode": board_mode,
            "max_per_industry": max_per_industry, "months": len(frame),
            "period_mean": float(frame["ret"].mean()),
            "bench_mean": bench_mean, "excess_mean": excess,
            "win_rate": float((frame["ret"] > 0).mean()),
            "cumulative": float(equity.iloc[-1] - 1),
            "max_drawdown": float((equity / equity.cummax() - 1).min()),
            "monthly": frame.to_dict("records"),
            "window": [frame["date"].iloc[0], frame["date"].iloc[-1]],
            "caveat": "板块成分表为当前快照（非 PIT），回溯统计有前视；未建模涨停买不进与冲击成本"}


STOCK_MODE_LABELS = {
    "reversal": "买滞涨（0.4×-5日涨幅 + 0.3×资金流 + 0.3×流动性）",
    "leader": "买龙头（0.4×20日强度 + 0.3×资金流 + 0.3×流动性）",
    "leader_5d": "买短期龙头（0.4×5日强度 + 0.3×资金流 + 0.3×流动性）",
    "cap_leader": "买市值龙头（0.5×流通市值 + 0.3×20日强度 + 0.2×资金流）",
}


BOARD_MODE_LABELS = {
    "flow": "动量0.5+资金流0.3−涨停0.2（旧口径）",
    "no_flow": "动量0.7−涨停0.3（去掉资金流项）",
    "hot": "动量0.6+涨停0.4（热度口径）",
}


def compare_grid(months: int = 24, board_modes: list[str] | None = None,
                 stock_modes: list[str] | None = None,
                 max_per_industry: int = 2) -> list[dict]:
    """板块层 × 个股层 全网格回放（同一窗口），返回按超额排序的行。"""
    rows: list[dict] = []
    for bm in (board_modes or sorted(BOARD_MODES)):
        for sm in (stock_modes or sorted(STOCK_MODES)):
            payload = backtest(months, sm, bm, max_per_industry=max_per_industry)
            if not payload.get("available"):
                continue
            rows.append({
                "board_mode": bm, "stock_mode": sm,
                "label": f"{bm} × {sm}",
                "excess_mean": payload["excess_mean"], "period_mean": payload["period_mean"],
                "bench_mean": payload["bench_mean"], "win_rate": payload["win_rate"],
                "cumulative": payload["cumulative"], "max_drawdown": payload["max_drawdown"],
                "months": payload["months"], "window": payload["window"],
            })
    rows.sort(key=lambda r: r["excess_mean"], reverse=True)
    return rows


def render_grid(rows: list[dict]) -> str:
    lines = ["[concept-arm] 板块层 × 个股层 全网格（同一窗口月度回放，按超额排序）：",
             f"{'板块口径':<10}{'个股口径':<12}{'期均':>9}{'基准':>9}{'超额':>9}{'胜率':>8}{'累计':>9}{'回撤':>8}"]
    for r in rows:
        lines.append(f"{r['board_mode']:<10}{r['stock_mode']:<12}{r['period_mean']:>+9.2%}"
                     f"{r['bench_mean']:>+9.2%}{r['excess_mean']:>+9.2%}{r['win_rate']:>8.0%}"
                     f"{r['cumulative']:>+9.1%}{r['max_drawdown']:>8.1%}")
    best = rows[0] if rows else None
    if best:
        lines.append(f"  → 超额最高的组合：{best['label']}（月均超额 {best['excess_mean']:+.2%}）")
    lines.append("  （基准 = 同窗口全市场等权；超额为月均，非年化；板块成分表非 PIT，有前视）")
    return "\n".join(lines)


def compare_modes(months: int = 24, max_per_industry: int = 2) -> list[dict]:
    """对每套 stock-mode 跑一遍同一窗口的月度回放，输出可直接对比的表。"""
    rows: list[dict] = []
    for mode in sorted(STOCK_MODES):
        payload = backtest(months, mode, max_per_industry=max_per_industry)
        if not payload.get("available"):
            rows.append({"stock_mode": mode, "label": STOCK_MODE_LABELS.get(mode, mode),
                         "available": False})
            continue
        rows.append({
            "stock_mode": mode, "label": STOCK_MODE_LABELS.get(mode, mode), "available": True,
            "months": payload["months"], "window": payload["window"],
            "period_mean": payload["period_mean"], "bench_mean": payload["bench_mean"],
            "excess_mean": payload["excess_mean"], "win_rate": payload["win_rate"],
            "cumulative": payload["cumulative"], "max_drawdown": payload["max_drawdown"],
        })
    rows.sort(key=lambda r: (r.get("excess_mean") if r.get("excess_mean") is not None else -9),
              reverse=True)
    return rows


def render_compare(rows: list[dict]) -> str:
    lines = ["[concept-arm] 板块内选谁 —— 四套口径同一窗口月度回放：",
             f"{'口径':<12}{'期均':>9}{'基准':>9}{'超额':>9}{'胜率':>8}{'累计':>9}{'最大回撤':>10}"]
    for r in rows:
        if not r.get("available"):
            lines.append(f"{r['stock_mode']:<12}  样本不足")
            continue
        lines.append(f"{r['stock_mode']:<12}{r['period_mean']:>+9.2%}{r['bench_mean']:>+9.2%}"
                     f"{r['excess_mean']:>+9.2%}{r['win_rate']:>8.0%}{r['cumulative']:>+9.1%}"
                     f"{r['max_drawdown']:>10.1%}")
    lines.append("  （基准 = 同窗口全市场等权；超额为月均，非年化）")
    return "\n".join(lines)


def robustness(payload_modes: list[tuple[str, str]], months: int = 24) -> dict:
    """稳健性拆分：**全窗超额排名第一 ≠ 可以上线**。

    为什么必须有这一步（2026-10-10 实测）：16 组网格里 `mom_only × leader`（纯板块动量 +
    板块内买龙头，即教科书式的"行业动量"）全窗超额 **+1.83%/月**、累计 +123%，看着最漂亮；
    但把它按月拆开：前半 **-1.08%**（胜率 45%）、后半 +4.75%（胜率 64%），
    而且 22 个月里有 4 个月的单月超额是 +12.0 / +16.7 / +15.7 / +18.5pp ——
    **把这 4 个月拿掉，整条策略就是负超额**。±10~18% 的单月波动 + 集中在一半样本，
    说明它不是"稳定的行业动量"，而是少数月份的运气。

    判定：
      * `halves_consistent`：前半与后半超额都得 > 0；
      * `top4_share`：贡献最大的 4 个月占全窗总超额的比例（> 100% 意味着去掉它们就翻负）；
      * `verdict`：`robust` / `half_sample_negative` / `few_months_driven`。
    """
    rows = []
    for board_mode, stock_mode in payload_modes:
        payload = backtest(months, stock_mode=stock_mode, board_mode=board_mode,
                           max_per_industry=2)
        if not payload.get("available"):
            continue
        monthly = [(r["date"], r["ret"], r["bench"]) for r in payload["monthly"]
                   if r["ret"] == r["ret"]]
        if len(monthly) < 8:
            continue
        excess = [r - b for _d, r, b in monthly]
        half = len(excess) // 2
        top4 = sum(sorted(excess, reverse=True)[:4])
        total = sum(excess)
        h1, h2 = excess[:half], excess[half:]
        halves_consistent = (sum(h1) > 0) and (sum(h2) > 0)
        concentrated = total != 0 and abs(top4) > abs(total) * 0.9
        rows.append({
            "board_mode": board_mode, "stock_mode": stock_mode,
            "label": f"{board_mode} × {stock_mode}",
            "months": len(excess), "window": [monthly[0][0], monthly[-1][0]],
            "excess_all": float(sum(excess) / len(excess)),
            "excess_h1": float(sum(h1) / len(h1)), "excess_h2": float(sum(h2) / len(h2)),
            "win_h1": float(sum(1 for x in h1 if x > 0) / len(h1)),
            "win_h2": float(sum(1 for x in h2 if x > 0) / len(h2)),
            "top4_sum": float(top4), "total_sum": float(total),
            "top4_share": float(top4 / total) if total else None,
            "halves_consistent": bool(halves_consistent),
            "verdict": ("robust" if halves_consistent and not concentrated
                        else ("half_sample_negative" if not halves_consistent else "few_months_driven")),
        })
    rows.sort(key=lambda r: -r["excess_all"])
    return {"months": months, "rows": rows,
            "caveat": "板块成分表为当前快照（非 PIT）；样本仅 2 年出头，半窗只有 ~11 期，"
                      "任何单窗口结论都不该直接上线。",
            "default": {"board_mode": DEFAULT_BOARD_MODE, "stock_mode": DEFAULT_STOCK_MODE}}


def render_robustness(payload: dict) -> str:
    lines = ["[concept-arm] 稳健性拆分（全窗 / 前半 / 后半 / 顶部 4 个月占比）：",
             f"{'组合':<24}{'全窗':>9}{'前半':>9}{'后半':>9}{'前胜':>6}{'后胜':>6}{'top4占比':>10}  判定"]
    for row in payload["rows"]:
        lines.append(f"{row['label']:<24}{row['excess_all']:>+9.2%}{row['excess_h1']:>+9.2%}"
                     f"{row['excess_h2']:>+9.2%}{row['win_h1']:>6.0%}{row['win_h2']:>6.0%}"
                     f"{(row['top4_share'] * 100 if row['top4_share'] is not None else float('nan')):>9.0f}%  "
                     f"{row['verdict']}")
    default = payload.get("default", {})
    lines.append(f"  → 当前默认口径：{default.get('board_mode')} × {default.get('stock_mode')}")
    lines.append("  （top4占比 ≈ 全窗超额里由贡献最大的 4 个月贡献的比例；>100% = 去掉它们就翻负）")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="概念/板块轮动手臂（独立策略项）")
    ap.add_argument("--as-of", default="")
    ap.add_argument("--top-boards", type=int, default=3)
    ap.add_argument("--candidates", type=int, default=10)
    ap.add_argument("--holdings", type=int, default=5)
    ap.add_argument("--per-board", type=int, default=4, help="每个板块进候选池的只数上限")
    ap.add_argument("--max-per-board", type=int, default=2, help="持仓里单个板块的只数上限")
    ap.add_argument("--max-per-industry", type=int, default=2,
                    help="同一**行业**进候选/持仓的只数上限（默认 2；治'候选里一堆银行'）")
    ap.add_argument("--capital", type=float, default=CAPITAL_DEFAULT)
    ap.add_argument("--backtest", action="store_true", help="月度回放该规则并写 arm_concept_backtest.json")
    ap.add_argument("--months", type=int, default=24)
    ap.add_argument("--stock-mode", default=DEFAULT_STOCK_MODE, choices=sorted(STOCK_MODES),
                    help="板块内选谁：reversal(滞涨)/leader(20日龙头)/leader_5d(短期龙头)/cap_leader(市值龙头)")
    ap.add_argument("--board-mode", default=DEFAULT_BOARD_MODE, choices=sorted(BOARD_MODES),
                    help="选哪些板块：flow(旧)/no_flow(去资金流)/hot(热度)")
    ap.add_argument("--robustness", action="store_true",
                    help="对网格里最强的几组做半窗拆分（防止「少数月份撑起来的策略」被当成结论）")
    ap.add_argument("--compare-grid", action="store_true",
                    help="板块层 × 个股层 全网格回放对比（12 组）")
    ap.add_argument("--compare-modes", action="store_true",
                    help="对四套 stock-mode 各跑一遍月度回放并对比（用收益说话，不信直觉）")
    args = ap.parse_args()

    if args.robustness:
        grid_path = OUT / "arm_concept_grid_compare.json"
        combos: list[tuple[str, str]] = []
        if grid_path.exists():
            grid = json.loads(grid_path.read_text(encoding="utf-8")).get("rows") or []
            combos = [(r["board_mode"], r["stock_mode"]) for r in grid[:4]]
        for pair in ((DEFAULT_BOARD_MODE, DEFAULT_STOCK_MODE), ("mom_only", "leader"),
                     ("mom_only", "cap_leader")):
            if pair not in combos:
                combos.append(pair)
        payload = robustness(combos, args.months)
        # 默认口径的关键数字提到顶层：策略池的 metrics_map 是按**顶层字段**取的，
        # 只放在 rows 里页面读不到（实测踩过：卡上只有回放数字、看不到"前半窗为负"）。
        default_row = next((r for r in payload["rows"]
                            if r["board_mode"] == DEFAULT_BOARD_MODE
                            and r["stock_mode"] == DEFAULT_STOCK_MODE), None)
        if default_row:
            payload.update({k: default_row[k] for k in
                            ("excess_all", "excess_h1", "excess_h2", "top4_share",
                             "halves_consistent", "verdict")})
        print(render_robustness(payload))
        (OUT / "arm_concept_robustness.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[concept-arm] 输出 {(OUT / 'arm_concept_robustness.json').relative_to(ROOT)}")
        return 0

    if args.compare_grid:
        rows = compare_grid(args.months, max_per_industry=args.max_per_industry)
        print(render_grid(rows))
        (OUT / "arm_concept_grid_compare.json").write_text(
            json.dumps({"months": args.months, "rows": rows,
                        "caveat": "板块成分表为当前快照（非 PIT），回溯统计有前视；未建模涨停买不进与冲击成本"},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[concept-arm] 输出 {(OUT / 'arm_concept_grid_compare.json').relative_to(ROOT)}")
        return 0

    if args.compare_modes:
        rows = compare_modes(args.months, args.max_per_industry)
        print(render_compare(rows))
        (OUT / "arm_concept_mode_compare.json").write_text(
            json.dumps({"months": args.months, "rows": rows,
                        "caveat": "板块成分表为当前快照（非 PIT），回溯统计有前视；未建模涨停买不进与冲击成本"},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[concept-arm] 输出 {(OUT / 'arm_concept_mode_compare.json').relative_to(ROOT)}")
        return 0

    if args.backtest:
        payload = backtest(args.months, args.stock_mode, args.board_mode, args.max_per_industry)
        OUT.mkdir(parents=True, exist_ok=True)
        # **只让默认口径写规范文件**：2026-10-10 实测踩过 —— 做隔离实验时依次跑了
        # `--board-mode hot/flow`，每次都覆盖 arm_concept_backtest.json，
        # 结果策略池卡上显示的是 hot 口径的 -1.53%，而实际默认口径是 no_flow 的 +0.52%
        # （页面数字和真实默认彻底对不上，而且没人会发现）。非默认口径写带后缀的文件。
        is_default = (args.board_mode == DEFAULT_BOARD_MODE and args.stock_mode == DEFAULT_STOCK_MODE)
        name = "arm_concept_backtest.json" if is_default else \
            f"arm_concept_backtest_{args.board_mode}_{args.stock_mode}.json"
        (OUT / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if payload.get("available"):
            bench = payload.get("bench_mean")
            excess = payload.get("excess_mean")
            print(f"[concept-arm] 回放 {payload['months']} 个月（{payload['window'][0]} ~ {payload['window'][1]}）："
                  f"期均 {payload['period_mean']:+.2%}｜全市场等权基准 "
                  f"{bench:+.2%}｜超额 {excess:+.2%}｜胜率 {payload['win_rate']:.0%}｜"
                  f"累计 {payload['cumulative']:+.1%}｜最大回撤 {payload['max_drawdown']:.1%}")
            print(f"[concept-arm] 注意：{payload['caveat']}")
            print(f"[concept-arm] 口径 {args.board_mode} × {args.stock_mode}"
                  f"{'（默认，写规范文件）' if is_default else '（非默认，写带后缀文件）'}")
        else:
            print("[concept-arm] 回放没有可用样本")
        return 0

    result = run(args.as_of, args.top_boards, args.candidates, args.holdings, args.capital,
                 per_board=args.per_board, max_per_board=args.max_per_board,
                 max_per_industry=args.max_per_industry,
                 stock_mode=args.stock_mode, board_mode=args.board_mode)
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
