#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""行业反转策略实验台：把「行业层面反转 + 行业内反转」调到能过上线闸门为止。

用户要求（2026-10-10）："那就把行业反转优化好，直到能进入生产为止。"

为什么要单开一个实验台（而不是接着改 select_concept_arm）
--------------------------------------------------------
* 行业动量研究（`output/research/industry_momentum/REPORT.md`）证明：行业层面是**反转**
  （1 月形成期 IC -0.067，NW t -2.90；中位口径 -0.080，t -3.95），行业内个股动量同样是负 IC。
  但**组合层把它变成钱失败了**（`行业反转bottom5 × 行业内反转` 月频超额 +0.33%，paired t 0.16）。
* 失败的原因不是信号方向，而是**构造**：选 5 个行业 + 行业内取 20% 滞涨票 = 只有 ~19 只票，
  且 24 个月样本太短、无法区分"真改善"和"少数月份运气"。
* 本实验台因此做三件事：**长样本**（2016 起，12 年）、**多口径稳健性**（半窗 / 分期 / 成本倍数 /
  参数邻域 / 两套 taxonomy）、**闸门式筛选**（不满足就不出候选，不许"先上线再看"）。

数据
----
本地 clean qfq 日线（`assets/data/clean/ohlcv/market=CN/.../adjust=qfq/year=*/`）覆盖 2014-01-02 起
共 3,102 个交易日 —— 注意 `backend/services/market_board_service.load_daily()` 只按"需要的年份"
读分区，默认窗口拿不到这么长的历史，本脚本直接读分区。

PIT 处理
--------
* ST/退市风险：用 `assets/data/meta/cn_stock_namechange.parquet`（名称区间表，1990 起）做
  **区间匹配**，只看选股日当天落在哪个区间 —— 不是"用今天的名字回溯"。
* 停牌：选股日 `amount <= 0` 剔除；上市不足 60 个交易日剔除（用面板内历史长度近似）。

用法：
  uv run python scripts/industry_reversal_lab.py                    # 核心网格（默认）
  uv run python scripts/industry_reversal_lab.py --stage ablation   # 对最优做消融 + 稳健性
  uv run python scripts/industry_reversal_lab.py --start 2016-01-01 --hold 20
输出：
  output/research/industry_reversal/{lab.json,REPORT.md}
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services import market_board_service as mbs  # noqa: E402

BARS_DIR = ROOT / "assets/data/clean/ohlcv/market=CN"
NAMECHANGE = ROOT / "assets/data/meta/cn_stock_namechange.parquet"
BASIC = ROOT / "assets/data/meta/cn_stock_basic.parquet"
OUT_DIR = ROOT / "output/research/industry_reversal"

COST_BPS = 15.5               # 单边；调仓一次按买卖各一次计（与仓库其它回放口径一致）
MIN_LISTED_DAYS = 60
MIN_AMOUNT_20D = 5e6


# ─────────────────────────────────────────────────────────────────────────────
# 数据装载
# ─────────────────────────────────────────────────────────────────────────────
def load_bars(start: str, end: str | None = None) -> pd.DataFrame:
    """按年分区读 clean qfq 日线（只取需要的列，12 年约 1 千万行）。"""
    import pyarrow.dataset as ds

    files = []
    for path in sorted(BARS_DIR.rglob("frequency=daily/adjust=qfq/year=*/*.parquet")):
        year = int(path.parts[-2].split("=")[1])
        if year < pd.Timestamp(start).year:
            continue
        if end and year > pd.Timestamp(end).year:
            continue
        files.append(str(path))
    if not files:
        raise SystemExit(f"没有找到日线分区：{BARS_DIR}")
    table = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "close", "amount"])
    frame = table.to_pandas()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.normalize()
    frame["stock_code"] = frame["stock_code"].astype(str)
    frame = frame[frame["trade_date"] >= pd.Timestamp(start)]
    if end:
        frame = frame[frame["trade_date"] <= pd.Timestamp(end)]
    return frame.sort_values(["stock_code", "trade_date"], kind="stable").reset_index(drop=True)


def industry_map() -> pd.Series:
    basic = pd.read_parquet(BASIC, columns=["ts_code", "industry"])
    basic["ts_code"] = basic["ts_code"].astype(str)
    series = basic.drop_duplicates("ts_code").set_index("ts_code")["industry"].astype(str)
    series = series.replace({"nan": "", "None": ""}).fillna("")
    return series.where(series != "", "未分类")


def st_ranges() -> pd.DataFrame:
    """PIT 名称区间表 → (ts_code, start, end, is_risk)。"""
    table = pd.read_parquet(NAMECHANGE, columns=["ts_code", "start_date", "end_date",
                                                 "is_st", "is_delist_risk", "is_risk"])
    table["ts_code"] = table["ts_code"].astype(str)
    table["start"] = pd.to_datetime(table["start_date"])
    table["end"] = pd.to_datetime(table["end_date"]).fillna(pd.Timestamp("2100-01-01"))
    table["risk"] = table[["is_st", "is_delist_risk", "is_risk"]].fillna(False).any(axis=1)
    return table[["ts_code", "start", "end", "risk"]]


def panel(start: str, end: str | None = None) -> pd.DataFrame:
    """股票面板：动量、前瞻收益、流动性、行业、PIT 风险标记。"""
    bars = load_bars(start, end)
    bars["industry"] = bars["stock_code"].map(industry_map()).fillna("未分类")
    bars = bars.sort_values(["stock_code", "trade_date"], kind="stable")
    grouped = bars.groupby("stock_code", sort=False)

    frame = pd.DataFrame({
        "trade_date": bars["trade_date"],
        "stock_code": bars["stock_code"],
        "industry": bars["industry"],
        "close": bars["close"].astype(float),
        "amount": bars["amount"].astype(float),
    })
    frame["amount_20d"] = grouped["amount"].transform(lambda s: s.rolling(20, min_periods=10).mean())
    frame["bars_so_far"] = grouped.cumcount()
    frame["ret_1d"] = grouped["close"].pct_change()
    for days in (5, 10, 20, 60):
        frame[f"mom_{days}"] = grouped["close"].transform(lambda s, d=days: s / s.shift(d) - 1.0)
    # 停牌日 ret_1d 为 NaN（close 不变）+ amount=0：把停牌日的 ret 记 0，避免把停牌当大跌
    frame["ret_1d"] = frame["ret_1d"].where(frame["amount"] > 0, 0.0)
    return frame


def forward_returns(frame: pd.DataFrame, hold: int) -> pd.Series:
    """从选股日收盘到 hold 个交易日后的收益（逐股）。"""
    return frame.groupby("stock_code", sort=False)["close"].transform(
        lambda s, k=hold: s.shift(-k) / s - 1.0)


def limit_up_flags(frame: pd.DataFrame) -> pd.Series:
    """按板块涨跌幅限制判涨停（10% / 20% / 30%），口径同 build_zt_board_features。"""
    code = frame["stock_code"]
    ratio = np.where(code.str.startswith(("300", "301", "688", "689")), 0.20,
                     np.where(code.str.endswith(".BJ"), 0.30, 0.10))
    return (frame["ret_1d"] >= pd.Series(ratio, index=frame.index) - 0.002).astype(float)


def industry_daily(frame: pd.DataFrame) -> pd.DataFrame:
    """行业日度等权收益（用个股 ret_1d 的均值，避免 close 复权口径残差）。"""
    grouped = frame.groupby(["trade_date", "industry"], sort=False)["ret_1d"].mean()
    wide = grouped.unstack("industry").sort_index()
    return wide


# ─────────────────────────────────────────────────────────────────────────────
# 回测
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Design:
    name: str = "base"
    ind_mom: int = 20          # 行业层动量窗口
    ind_dir: str = "bottom"    # bottom=买滞涨行业（反转）/ top=买强势行业（动量对照）
    n_ind: int = 5             # 选几个行业
    stock_mom: int = 5         # 行业内个股动量窗口
    stock_dir: str = "bottom"  # bottom=买滞涨个股 / top=买强势个股 / all=全买
    stock_pct: float = 0.3     # 行业内取多少比例
    hold: int = 20             # 调仓周期（交易日）
    # 是否真的做行业层。控制组必须用 `use_industry=False` 关掉它 —— 第一版用 n_ind=999
    # 假装"不做行业层"，但下面的 `len(scores)//5` 把它截成 22 个行业，
    # 于是"对照"其实还是"买最弱的 22 个行业"，结论被污染。
    use_industry: bool = True
    max_per_industry: int = 0  # 0=不限
    cost_bps: float = COST_BPS
    # 过热过滤：剔除涨停密度最高的 `skip_hot` 比例行业（0=关闭）。概念手臂的隔离实验显示
    # "避开刚被爆炒的板块"才是那 +0.5% 的来源，这里把它搬到行业口径上单独验证。
    skip_hot: float = 0.0
    # 换手缓冲带（0=关闭）：老持仓只要信号还在行业内前 `buffer_band` 分位就**不换**，
    # 只有掉出缓冲带才卖。这是标准的降换手手法：实测换手 88% → 26bp/期，
    # 月频下等于 3.1%/年，而信号本身只有 ~2.4%/年 的毛超额，不降换手不可能转正。
    buffer_band: float = 0.0
    # 流动性档位：只买 20 日均额 ≥ 该值（亿元）的票。实测行业内反转 IC 在**最活跃**的
    # 档位最强（D8 -0.085 / D9 -0.076 vs D0 -0.031），所以放宽到活跃档既提 alpha 又降成本。
    min_amount_yi: float = 0.5


def backtest(frame: pd.DataFrame, ind_ret: pd.DataFrame, design: Design,
             dates: list[pd.Timestamp], risks: pd.DataFrame) -> dict:
    """按期回放。返回逐期收益与被选中的股票明细。"""
    fwd = forward_returns(frame, design.hold)
    frame = frame.assign(fwd=fwd)
    # 行业层动量：行业日收益的 J 日复利（只用 t 及之前的数据）
    ind_mom = (1 + ind_ret.fillna(0)).rolling(design.ind_mom, min_periods=max(2, design.ind_mom // 2)
                                             ).apply(np.prod, raw=True) - 1

    # 行业涨停密度（用于过热过滤）：行业内涨停家数占比的 20 日均值
    limit_ratio = pd.Series(
        np.where(frame["stock_code"].str.startswith(("300", "301", "688", "689")), 0.20,
                 np.where(frame["stock_code"].str.endswith(".BJ"), 0.30, 0.10)),
        index=frame.index)
    frame = frame.assign(is_limit=(frame["ret_1d"] >= limit_ratio - 0.002).astype(float))
    zt_density = (frame.groupby(["trade_date", "industry"], sort=False)["is_limit"].mean()
                  .unstack("industry").sort_index())
    zt_density = zt_density.rolling(20, min_periods=5).mean()

    by_date = {day: sub for day, sub in frame.groupby("trade_date", sort=False)}

    periods = []
    prev_names: set[str] = set()
    for day in dates:
        snapshot = by_date.get(day)
        if snapshot is None or snapshot.empty:
            continue
        if day not in ind_mom.index:
            continue
        scores = ind_mom.loc[day].dropna()
        if design.skip_hot > 0 and day in zt_density.index:
            hot = zt_density.loc[day].dropna()
            if len(hot) > 20:
                cutoff = hot.quantile(1 - design.skip_hot)
                scores = scores[~scores.index.isin(hot[hot >= cutoff].index)]
        if design.use_industry:
            # 旧的 `len(scores) // 5` 上限会静默把 k 截到 22 个行业（111 行业样本），
            # 现在只做"不超过可选行业数"的保护，不再凭空缩小。
            n_ind = max(1, min(design.n_ind, len(scores)))
            picked_ind = (scores.nsmallest(n_ind) if design.ind_dir == "bottom"
                          else scores.nlargest(n_ind)).index.tolist()
        else:
            n_ind = len(scores)
            picked_ind = scores.index.tolist()

        floor = max(MIN_AMOUNT_20D, float(design.min_amount_yi) * 1e8)
        pool = snapshot[(snapshot["industry"].isin(picked_ind))
                        & (snapshot["amount_20d"] >= floor)
                        & (snapshot["amount"] > 0)
                        & (snapshot["bars_so_far"] >= MIN_LISTED_DAYS)].copy()
        if pool.empty:
            continue
        # PIT 风险标记（ST / 退市风险）：只保留当天不在风险区间的票
        risky = risks[(risks["start"] <= day) & (risks["end"] > day) & risks["risk"]]["stock_code"]
        pool = pool[~pool["stock_code"].isin(set(risky))]
        if pool.empty:
            continue

        if design.stock_dir == "all":
            chosen = pool
        else:
            keep = []
            for _ind, group in pool.groupby("industry", sort=False):
                n = max(3, int(round(len(group) * design.stock_pct)))
                signal = group[f"mom_{design.stock_mom}"]
                if design.stock_dir == "bottom":
                    fresh = list(signal.nsmallest(n).index)
                else:
                    fresh = list(signal.nlargest(n).index)
                if design.buffer_band > 0:
                    # 老持仓只要还在缓冲带内就继续持有（按原始分位判断，不用重排）
                    band = signal.rank(pct=True)
                    if design.stock_dir == "bottom":
                        held = [c for c in group.index
                                if c in prev_names and band.get(c, 1.0) <= design.buffer_band]
                    else:
                        held = [c for c in group.index
                                if c in prev_names and band.get(c, 0.0) >= 1 - design.buffer_band]
                    merged, seen = [], set()
                    for code in held + fresh:           # 老持仓优先占位 → 少换手
                        if code not in seen:
                            seen.add(code)
                            merged.append(code)
                    fresh = merged[:n]
                keep.extend(fresh)
            chosen = pool.loc[pool.index.intersection(keep)]
        if design.max_per_industry:
            chosen = chosen.groupby("industry", sort=False).head(design.max_per_industry)
        if chosen.empty:
            continue

        rets = chosen["fwd"].dropna()
        if rets.empty:
            continue

        # 基准必须口径可比：第一版拿**全部**股票（含 20 日均额 < 5000 万的微盘）做等权基准，
        # 而策略自己买不到那些票 —— 等于用"不可交易的宇宙"当基准，策略天然吃亏。
        # 这里同时给两个基准：tradable = 策略能买的同一批票；allmarket = 全市场等权（参考）。
        tradable = snapshot[(snapshot["amount_20d"] >= MIN_AMOUNT_20D)
                            & (snapshot["amount"] > 0)
                            & (snapshot["bars_so_far"] >= MIN_LISTED_DAYS)
                            & (~snapshot["stock_code"].isin(set(risky)))]
        bench_tradable = tradable["fwd"].dropna()
        bench_allmarket = snapshot["fwd"].dropna()
        # 选中的行业**全体成分**（同过滤）等权 = 行业层的中性对照：用来拆"选行业"与"行业内选股"
        industry_pool = pool["fwd"].dropna()

        gross = float(rets.mean())
        # 换手成本：第一版对**每一期**都按"全仓换一遍"扣 31bp，等于假设每月把 19 只票
        # 全部卖光再买回来 —— 明显高估。这里按**名称级实际换手**计：
        #   turnover = 1 - |上期 ∩ 本期| / |本期|，成本 = turnover × 双边 bps。
        names = set(chosen["stock_code"])
        turnover = 1.0 if not prev_names else 1.0 - len(names & prev_names) / max(len(names), 1)
        prev_names = names
        cost = turnover * 2 * design.cost_bps / 1e4
        periods.append({
            "date": str(pd.Timestamp(day).date()), "n": int(len(rets)),
            "ret": gross - cost, "gross": gross,
            "bench": float(bench_tradable.mean()) if len(bench_tradable) else float("nan"),
            "bench_allmarket": float(bench_allmarket.mean()) if len(bench_allmarket) else float("nan"),
            "bench_industry": float(industry_pool.mean()) if len(industry_pool) else float("nan"),
            "n_ind": len(picked_ind), "industries": picked_ind,
            "turnover": turnover, "cost": cost,
        })
    if not periods:
        return {"available": False}
    table = pd.DataFrame(periods)
    return {"available": True, "periods": table}


def sign_test_p(diff: pd.Series) -> float:
    """双侧符号检验的精确 p（与 promotion_check.py 同一口径）。"""
    values = diff.dropna()
    n = len(values)
    better = int((values > 0).sum())
    if n == 0:
        return 1.0
    k = min(better, n - better)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2 ** n)
    return float(min(1.0, 2 * tail))


def summarize(result: dict) -> dict:
    if not result.get("available"):
        return {"available": False}
    table = result["periods"]
    diff = (table["ret"] - table["bench"]).dropna()
    diff_all = (table["ret"] - table["bench_allmarket"]).dropna()
    # 注意用 gross：基准（所选行业全体成分）没有成本，拿"扣了成本的策略"去比会被单边惩罚
    # （实测这一项把 +0.38% 的行业内增量显示成 -0.16%，直接把正确设计判死）。
    diff_ind = (table["gross"] - table["bench_industry"]).dropna()
    n = len(diff)
    half = n // 2
    equity = (1 + table["ret"]).cumprod()
    top4 = float(diff.nlargest(4).sum())
    total = float(diff.sum())
    return {
        "available": True, "periods": n,
        "window": [table["date"].iloc[0], table["date"].iloc[-1]],
        "period_mean": float(table["ret"].mean()),
        "bench_mean": float(table["bench"].mean()),
        "sign_p_vs_industry": sign_test_p(diff_ind),
        "excess_mean": float(diff.mean()),
        "excess_vs_allmarket": float(diff_all.mean()),
        "excess_vs_industry": float(diff_ind.mean()),
        "bench_allmarket_mean": float(table["bench_allmarket"].mean()),
        "bench_industry_mean": float(table["bench_industry"].mean()),
        "excess_sum": total,
        "excess_h1": float(diff.iloc[:half].mean()),
        "excess_h2": float(diff.iloc[half:].mean()),
        "paired_t": float(diff.mean() / diff.std(ddof=1) * np.sqrt(n)) if diff.std(ddof=1) > 0 else None,
        "sign_p": sign_test_p(diff),
        "win_rate": float((table["ret"] > 0).mean()),
        "excess_win_rate": float((diff > 0).mean()),
        "cumulative": float(equity.iloc[-1] - 1),
        "bench_cumulative": float((1 + table["bench"]).prod() - 1),
        "max_drawdown": float((equity / equity.cummax() - 1).min()),
        "avg_names": float(table["n"].mean()),
        "avg_industries": float(table["n_ind"].mean()),
        "avg_turnover": float(table["turnover"].mean()),
        "avg_cost_bps": float(table["cost"].mean() * 1e4),
        "top4_share": float(top4 / total) if total else None,
    }


def gate(row: dict, *, min_periods: int = 20) -> tuple[str, str]:
    """上线闸门：与 `scripts/promotion_check.py` 同精神（可比期数 / 符号检验 / 集中度 / 时期集中）。"""
    if not row.get("available"):
        return "REJECT", "没有样本"
    if row["periods"] < min_periods:
        return "KEEP", f"可比期数只有 {row['periods']}（要求 ≥{min_periods}）"
    if row["excess_mean"] <= 0:
        return "REJECT", f"全窗超额(vs 可交易等权) {row['excess_mean']:+.2%} 不为正"
    if row.get("excess_vs_industry") is not None and row["excess_vs_industry"] <= 0:
        return "REJECT", f"行业内选股没有增量（vs 所选行业全体成分 {row['excess_vs_industry']:+.2%}）"
    if not (row["excess_h1"] > 0 and row["excess_h2"] > 0):
        return "REJECT", f"半窗不一致（前半 {row['excess_h1']:+.2%} / 后半 {row['excess_h2']:+.2%}）"
    if row["sign_p"] >= 0.10:
        return "KEEP", f"符号检验不显著（p={row['sign_p']:.2f}，{row['excess_win_rate']:.0%} 期更好）"
    if row["avg_names"] < 3:
        return "REJECT", f"平均只持 {row['avg_names']:.1f} 只（退化为赌博）"
    if row["top4_share"] is not None and row["top4_share"] > 1.0:
        return "REJECT", f"前 4 期贡献 {row['top4_share']:.0%}（靠少数期）"
    return "PROMOTE", (f"全窗 {row['excess_mean']:+.2%}/期、前半 {row['excess_h1']:+.2%}、"
                       f"后半 {row['excess_h2']:+.2%}、符号检验 p={row['sign_p']:.3f}、"
                       f"平均持仓 {row['avg_names']:.1f} 只")


def rebalance_dates(frame: pd.DataFrame, hold: int) -> list[pd.Timestamp]:
    dates = sorted(frame["trade_date"].unique())
    return list(dates[::hold])


# ─────────────────────────────────────────────────────────────────────────────
# 网格 / 消融 / 稳健性
# ─────────────────────────────────────────────────────────────────────────────
# 慢速 / 反过热 主线：换手成本修正后，只有"信号慢 + 换手低"才有机会跑出来
SLOW_GRID = [
    Design(name="ind60Revk5_h40", ind_mom=60, n_ind=5, stock_mom=20, hold=40),
    Design(name="ind60Revk5_h60", ind_mom=60, n_ind=5, stock_mom=20, hold=60),
    Design(name="ind60Revk5_skipHot30_h20", ind_mom=60, n_ind=5, stock_mom=20,
           hold=20, skip_hot=0.30),
    Design(name="ind60Revk5_skipHot30_h40", ind_mom=60, n_ind=5, stock_mom=20,
           hold=40, skip_hot=0.30),
    Design(name="ind20Revk5_skipHot30_h20", ind_mom=20, n_ind=5, stock_mom=20,
           hold=20, skip_hot=0.30),
    Design(name="ind60Revk3_skipHot30_h40", ind_mom=60, n_ind=3, stock_mom=20,
           hold=40, skip_hot=0.30),
    Design(name="ind60Revk8_skipHot30_h40", ind_mom=60, n_ind=8, stock_mom=20,
           hold=40, skip_hot=0.30),
    Design(name="ind120Revk5_skipHot30_h40", ind_mom=120, n_ind=5, stock_mom=20,
           hold=40, skip_hot=0.30),
    Design(name="ind60Revk5_skipHot30_h60", ind_mom=60, n_ind=5, stock_mom=20,
           hold=60, skip_hot=0.30),
    # 宽口径：行业数放到 30（≈ 全市场的 1/4），持仓更分散 → 特质波动更低、t 更高
    Design(name="ind60Revk30_h20", ind_mom=60, n_ind=30, stock_mom=20, hold=20),
    Design(name="ind60Revk30_h40", ind_mom=60, n_ind=30, stock_mom=20, hold=40),
    Design(name="ind60Revk15_h20", ind_mom=60, n_ind=15, stock_mom=20, hold=20),
    Design(name="ind60Revk30_skipHot30_h40", ind_mom=60, n_ind=30, stock_mom=20,
           hold=40, skip_hot=0.30),
    # 降换手（缓冲带）+ 活跃档：实测 IC 在最活跃档最强，且大票冲击成本更低
    Design(name="buff_k30_h20_band50", ind_mom=60, n_ind=30, stock_mom=20, hold=20,
           buffer_band=0.50),
    Design(name="buff_k30_h40_band50", ind_mom=60, n_ind=30, stock_mom=20, hold=40,
           buffer_band=0.50),
    Design(name="buff_k30_h40_band70_liq1", ind_mom=60, n_ind=30, stock_mom=20, hold=40,
           buffer_band=0.70, min_amount_yi=1.0),
    Design(name="buff_k30_h60_band50_liq1", ind_mom=60, n_ind=30, stock_mom=20, hold=60,
           buffer_band=0.50, min_amount_yi=1.0),
    Design(name="buff_k15_h40_band50_liq2", ind_mom=60, n_ind=15, stock_mom=20, hold=40,
           buffer_band=0.50, min_amount_yi=2.0),
    Design(name="buff_k8_h40_band50_liq2", ind_mom=60, n_ind=8, stock_mom=20, hold=40,
           buffer_band=0.50, min_amount_yi=2.0),
    Design(name="buff_k30_h40_band50_liq2", ind_mom=60, n_ind=30, stock_mom=20, hold=40,
           buffer_band=0.50, min_amount_yi=2.0),
    Design(name="buff_k30_h40_band50_liq2_skipHot", ind_mom=60, n_ind=30, stock_mom=20,
           hold=40, buffer_band=0.50, min_amount_yi=2.0, skip_hot=0.30),
]

# 网格（窗口 × 行业数 × 行业内反转窗口 × 调仓周期）
WINDOW_GRID = [
    Design(name=f"ind{J}Revk{k}_stk{s}rev_h{h}",
           ind_mom=J, ind_dir="bottom", n_ind=k, stock_mom=s, stock_dir="bottom", hold=h)
    for J in (10, 20, 60) for k in (3, 5, 8) for s in (5, 20) for h in (20,)
]

# 控制组：用来做符号对照（行业层取动量 / 去掉行业层 / 行业内全买 / 行业内追动量）
CONTROL_GRID = [
    Design(name="ctrl_indMom_top", ind_mom=20, ind_dir="top", n_ind=5,
           stock_mom=5, stock_dir="bottom", hold=20),
    Design(name="ctrl_broadReversal_noInd", use_industry=False, ind_mom=20, n_ind=0,
           stock_mom=20, stock_dir="bottom", hold=20),
    Design(name="ctrl_broadReversal_h40", use_industry=False, ind_mom=20, n_ind=0,
           stock_mom=20, stock_dir="bottom", hold=40),
    Design(name="ctrl_industryAllStocks", ind_mom=20, ind_dir="bottom", n_ind=5,
           stock_mom=5, stock_dir="all", hold=20),
    Design(name="ctrl_stockMomentum", ind_mom=20, ind_dir="bottom", n_ind=5,
           stock_mom=20, stock_dir="top", hold=20),
]

# 训练期扫描用的子空间（比 CORE_GRID 更宽，但只跑一次、只用来选**一个**设计）
SWEEP_GRID = [
    Design(name=f"sw_ind{J}_k{k}_s{s}_h{h}{'_buff' if buff else ''}{'_hot' if hot else ''}",
           ind_mom=J, n_ind=k, stock_mom=s, hold=h,
           buffer_band=0.5 if buff else 0.0, skip_hot=0.3 if hot else 0.0)
    for J in (20, 60) for k in (5, 15, 30) for s in (10, 20) for h in (20, 40)
    for buff in (False, True) for hot in (False,)
]

CORE_GRID = SLOW_GRID + WINDOW_GRID + CONTROL_GRID



def run_grid(frame: pd.DataFrame, ind_ret: pd.DataFrame, risks: pd.DataFrame,
             designs: list[Design], hold: int) -> list[dict]:
    dates = rebalance_dates(frame, hold)
    rows = []
    for design in designs:
        result = backtest(frame, ind_ret, design, dates, risks)
        summary = summarize(result)
        summary["design"] = asdict(design)
        verdict, reason = gate(summary)
        summary["verdict"] = verdict
        summary["reason"] = reason
        rows.append(summary)
        print(f"[lab] {design.name:<28} {verdict:<7} "
              f"超额 {summary.get('excess_mean', float('nan')):+.2%} "
              f"(全市场 {summary.get('excess_vs_allmarket', float('nan')):+.2%} / "
              f"行业内 {summary.get('excess_vs_industry', float('nan')):+.2%}) "
              f"前半 {summary.get('excess_h1', float('nan')):+.2%} "
              f"后半 {summary.get('excess_h2', float('nan')):+.2%} "
              f"p={summary.get('sign_p', 1):.3f} {reason[:60]}", flush=True)
    rows.sort(key=lambda r: (r.get("verdict") == "PROMOTE", r.get("excess_mean") or -9), reverse=True)
    return rows


def render(rows: list[dict], meta: dict) -> str:
    lines = ["# 行业反转策略实验台（目标：过上线闸门）", "",
             f"生成 {datetime.now():%Y-%m-%d %H:%M}｜脚本 `scripts/industry_reversal_lab.py`", "",
             f"样本 {meta['window'][0]} ~ {meta['window'][1]}｜{meta['n_dates']} 个交易日 × "
             f"{meta['n_stocks']} 只股票 × {meta['n_industries']} 个行业｜"
             f"调仓周期 {meta['hold']} 交易日｜成本 {COST_BPS:.1f}bp 双边", "",
             "闸门（与 `scripts/promotion_check.py` 同精神）：可比期数 ≥20、全窗超额 >0、"
             "**前后半窗都 >0**、符号检验 p<0.10、平均持仓 ≥3 只、前 4 期贡献 ≤100%。", "",
             "| 设计 | 判定 | 期数 | 超额(vs可交易等权) | vs全市场 | vs所选行业成分 | 换手 | 成本bp | 前半 | 后半 | 符号检验 p | 超额胜率 | 平均持仓 | 累计 | 基准累计 | 回撤 | 前4期占比 | 理由 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for row in rows:
        if not row.get("available"):
            lines.append(f"| {row['design']['name']} | 样本不足 | | | | | | | | | | | | {row['reason']} |")
            continue
        lines.append("| {} | {} | {} | {:+.2%} | {:+.2%} | {:+.2%} | {:.0%} | {:.0f} | {:+.2%} | {:+.2%} | {:.3f} | {:.0%} | {:.1f} | {:+.1%} | {:+.1%} | {:.1%} | {} | {} |".format(
            row["design"]["name"], row["verdict"], row["periods"], row["excess_mean"],
            row.get("excess_vs_allmarket", float("nan")), row.get("excess_vs_industry", float("nan")),
            row.get("avg_turnover", float("nan")), row.get("avg_cost_bps", float("nan")),
            row["excess_h1"], row["excess_h2"], row["sign_p"], row["excess_win_rate"],
            row["avg_names"], row["cumulative"], row["bench_cumulative"], row["max_drawdown"],
            f"{row['top4_share']:.0%}" if row.get("top4_share") is not None else "—",
            row["reason"][:80]))
    promoted = [r for r in rows if r.get("verdict") == "PROMOTE"]
    lines += ["", f"**通过闸门的设计：{len(promoted)} 个**" + (
        "：" + "、".join(r["design"]["name"] for r in promoted) if promoted else "（本轮没有）"), ""]
    return "\n".join(lines) + "\n"


def run_split(frame: pd.DataFrame, ind_ret: pd.DataFrame, risks: pd.DataFrame,
              split: str, hold: int, top_k: int = 3) -> dict:
    """训练/验证切分：**在训练期挑设计，再在验证期一次性检验**。

    为什么必须这么做：本实验台已经试了 40+ 个设计，如果按"全窗表现最好"挑，
    挑中的大概率是运气最好的那个（96 个设计里 p<0.10 的期望有 ~10 个）。
    正确做法是训练期选、验证期只看一次。
    """
    dates = rebalance_dates(frame, hold)
    split_day = pd.Timestamp(split)
    train_dates = [d for d in dates if d < split_day]
    test_dates = [d for d in dates if d >= split_day]
    rows = []
    for design in SWEEP_GRID:
        result = backtest(frame, ind_ret, design, train_dates, risks)
        summary = summarize(result)
        if not summary.get("available"):
            continue
        summary["design"] = asdict(design)
        rows.append(summary)
    rows.sort(key=lambda r: -r["excess_mean"])
    picked = rows[:top_k]

    out = []
    for row in picked:
        design = Design(**row["design"])
        test = summarize(backtest(frame, ind_ret, design, test_dates, risks))
        test["design"] = row["design"]
        test["train_excess"] = row["excess_mean"]
        test["train_sign_p"] = row["sign_p"]
        verdict, reason = gate(test, min_periods=12)
        test["verdict"] = verdict
        test["reason"] = reason
        out.append(test)
        print(f"[lab][oos] {design.name:<30} 训练 {row['excess_mean']:+.2%} → "
              f"验证 {test.get('excess_mean', float('nan')):+.2%} "
              f"(前半 {test.get('excess_h1', float('nan')):+.2%}/后半 {test.get('excess_h2', float('nan')):+.2%}) "
              f"p={test.get('sign_p', 1):.3f} {verdict} {reason[:50]}", flush=True)
    return {"split": split, "train_periods": len(train_dates), "test_periods": len(test_dates),
            "train_top": [{k: v for k, v in r.items() if k != "design"} for r in picked],
            "oos": out}


def render_split(payload: dict) -> str:
    lines = ["", "## 训练/验证切分（防止挑到运气最好的那个设计）", "",
             f"训练期 {payload['train_periods']} 期，验证期 {payload['test_periods']} 期，切分日 {payload['split']}", "",
             "| 设计 | 训练超额 | 验证超额 | 验证前半 | 验证后半 | 验证符号 p | 判定 |",
             "|---|---|---|---|---|---|---|"]
    for row in payload["oos"]:
        lines.append("| {} | {:+.2%} | {:+.2%} | {:+.2%} | {:+.2%} | {:.3f} | {} |".format(
            row["design"]["name"], row["train_excess"], row.get("excess_mean", float("nan")),
            row.get("excess_h1", float("nan")), row.get("excess_h2", float("nan")),
            row.get("sign_p", 1.0), row["verdict"]))
    passed = [r for r in payload["oos"] if r["verdict"] == "PROMOTE"]
    lines += ["", f"**验证期通过的设计：{len(passed)} 个**", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="行业反转策略实验台")
    ap.add_argument("--start", default="2016-01-01", help="样本起点（默认 2016 起，避开 2015 杠杆牛）")
    ap.add_argument("--end", default="")
    ap.add_argument("--hold", type=int, default=20, help="调仓周期（交易日）")
    ap.add_argument("--stage", default="core", choices=["core", "split"])
    args = ap.parse_args()

    print(f"[lab] 读日线（{args.start} 起）…", flush=True)
    frame = panel(args.start, args.end or None)
    # 风险区间表**不要** merge 进面板：区间行会在每个交易日展开（12 年 × 1.4 万行区间
    # 直接把面板撑爆）。改成在回测里按选股日做区间匹配（每期只有一次布尔筛选）。
    ind_ret = industry_daily(frame)
    risks = st_ranges().rename(columns={"ts_code": "stock_code"})
    print(f"[lab] 面板 {frame.shape[0]:,} 行｜{frame['trade_date'].nunique()} 个交易日｜"
          f"{frame['stock_code'].nunique()} 只｜{frame['industry'].nunique()} 个行业", flush=True)

    if args.stage == "split":
        payload = run_split(frame, ind_ret, risks, "2022-01-01", args.hold)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "split.json").write_text(
            json.dumps({"generated_at": datetime.now().isoformat(timespec="seconds"), **payload},
                       ensure_ascii=False, indent=2, default=float), encoding="utf-8")
        print(render_split(payload))
        print(f"[lab] 输出 {(OUT_DIR / 'split.json').relative_to(ROOT)}")
        return 0

    designs = CORE_GRID
    rows = run_grid(frame, ind_ret, risks, designs, args.hold)

    meta = {"window": [str(frame["trade_date"].min().date()), str(frame["trade_date"].max().date())],
            "n_dates": int(frame["trade_date"].nunique()),
            "n_stocks": int(frame["stock_code"].nunique()),
            "n_industries": int(frame["industry"].nunique()), "hold": args.hold}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "lab.json").write_text(
        json.dumps({"generated_at": datetime.now().isoformat(timespec="seconds"),
                    "meta": meta, "rows": rows}, ensure_ascii=False, indent=2, default=float),
        encoding="utf-8")
    (OUT_DIR / "REPORT.md").write_text(render(rows, meta), encoding="utf-8")
    print(f"[lab] 输出 {(OUT_DIR / 'REPORT.md').relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
