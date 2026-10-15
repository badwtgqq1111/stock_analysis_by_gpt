#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""主力资金流追踪（moneyflow_arm）：**用大单/主力净流入**找"有人在收"的票（用户 2026-10-14）。

与 `docs/reference/主力追踪.md` 的关系：那份是聚宽研究脚本（拉十大流通股东数据存 Excel），
口径是"股东结构变化"；本仓库**没有**股东户数/十大流通股东数据，但有**三源资金流特征表**
（`assets/data/derived/cn_moneyflow_features.parquet`：东财/同花顺/默认源 的净流入 + z 分数 + 净流入天数占比，
覆盖 2024-08-23 起）。所以这里做的是**资金流口径的主力追踪**，并把口径与样本长度如实写进结论。

信号（全部只用当日及之前数据）：
  1. **主力净流入强度**：`moneyflow_net_z_5d ≥ 1.5`（5 日净流入相对自身历史的 z 分数，跨股票可比）；
  2. **持续性**：`moneyflow_positive_ratio_10d ≥ 0.6`（10 日里 ≥6 天净流入）；
  3. **没被拉爆**：近 20 日涨幅 ∈ [−10%, +8%]（是"吸筹"不是"追高"）；
  4. **趋势确认**：收盘 > MA20；
  5. 流动性：20 日均成交额 ≥ 2000 万；价格 > 2 元。

组合：每日最多 5 只（按 5 日净流入 z 分数降序）、次日开盘买、持有 10 日或跌破入场价 −8% 止损、
双边 15.5bp。回放引擎复用 `wyckoff_study.replay`（同一套执行假设，避免两套算术）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "scripts")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import wyckoff_study as ws  # noqa: E402

MF = ROOT / "assets/data/derived/cn_moneyflow_features.parquet"
CHIP = ROOT / "assets/data/derived/cn_chip_features.parquet"
# 默认参数**不是网格里最好的那一格**（那格是 z=1.5/pos=5/hold=10 = +226.6pp 的孤立高点），
# 而是"整块高原"的中心：z=1.0 时 hold=10 的 pos∈{3,5,10} 与 z=1.5/pos=5/hold=10 全为正超额。
# 口径：`moneyflow_net_z_5d` 在数据里被硬截断在 ±1.79（实测 max=1.79），所以阈值 >1.79 恒为 0 信号。
DEFAULTS = dict(z_5d=1.0, pos_ratio_10d=0.6, ret20_min=-0.10, ret20_max=0.08,
                max_positions=5, max_hold=10, stop_pct=0.08, min_amount_20d=2e7,
                # ── 筹码分布（第八十二轮补库的数据，第八十三轮接进策略）──────────────
                # `chip_rank=True`：名额排序 = 资金流 z 排名 + **现价偏离成本**排名（各半）
                #   —— IC 检验显示 chip_price_vs_cost 显著为负（现价越高于平均成本越弱），
                #      所以"成本偏离小"的票优先（实测夏普 0.95→1.48、回撤 −40%→−14.6%）。
                # `chip_cost_max`：硬过滤"现价高于平均成本太多"的票（默认 None = 不过滤）。
                chip_rank=False, chip_cost_max=None,
                # 反例开关：要求"筹码集中"（宽度小）—— IC 检验说这条方向是错的，
                # 实测会把累计收益从 +174% 打到 +20%，保留这个开关是为了能一键复现反例。
                chip_require_concentrated=False)


def load(bars_start: str = "2024-06-01") -> tuple[pd.DataFrame, pd.DataFrame]:
    bars = ws.load_bars(bars_start)
    mf = pd.read_parquet(MF)
    mf["trade_date"] = pd.to_datetime(mf["trade_date"])
    cols = ["trade_date", "stock_code", "moneyflow_net_z_5d", "moneyflow_positive_ratio_10d",
            "moneyflow_net_z_20d", "moneyflow_net_amount_pct"]
    return bars, mf[cols]


def build_signals(bars: pd.DataFrame, mf: pd.DataFrame, **kw) -> pd.DataFrame:
    p = {**DEFAULTS, **kw}
    frame = bars.sort_values(["stock_code", "trade_date"]).copy()
    universe = ws._universe()
    if universe:
        frame = frame[frame["stock_code"].isin(universe)]
    g = frame.groupby("stock_code", sort=False)
    frame["ma20"] = g["close"].transform(lambda s: s.rolling(20).mean())
    frame["close_20"] = g["close"].transform(lambda s: s.shift(20))
    frame["amount_ma"] = g["amount"].transform(lambda s: s.rolling(20).mean())
    frame["ret20"] = frame["close"] / frame["close_20"] - 1

    merged = frame.merge(mf, on=["trade_date", "stock_code"], how="left")
    cond = (
        (merged["moneyflow_net_z_5d"] >= p["z_5d"])
        & (merged["moneyflow_positive_ratio_10d"] >= p["pos_ratio_10d"])
        & (merged["ret20"].between(p["ret20_min"], p["ret20_max"]))
        & (merged["close"] > merged["ma20"])
        & (merged["amount_ma"] >= p["min_amount_20d"])
        & (merged["close"] > 2.0)
    )
    sig = merged[cond].copy()
    if sig.empty:
        return sig
    # ── 筹码分布（同日收盘后可得，与资金流同一决策时点：都用于"次日开盘买"）──────────
    if p["chip_rank"] or p["chip_cost_max"] is not None or p["chip_require_concentrated"]:
        chip = pd.read_parquet(CHIP, columns=["ts_code", "trade_date", "chip_winner_rate",
                                             "chip_price_vs_cost", "chip_cost_width",
                                             "chip_concentration", "chip_upper_pressure"])
        chip["trade_date"] = pd.to_datetime(chip["trade_date"])
        sig = sig.merge(chip.rename(columns={"ts_code": "stock_code"}),
                        on=["stock_code", "trade_date"], how="left")
        if p["chip_require_concentrated"]:          # 反例开关：要求"筹码集中"（方向与 IC 相反）
            sig = sig[sig["chip_cost_width"].fillna(9) <= sig["chip_cost_width"].median()]
        if p["chip_cost_max"] is not None:          # 硬过滤"现价高于平均成本太多"的票
            sig = sig[sig["chip_price_vs_cost"].fillna(0) <= float(p["chip_cost_max"])]
        if sig.empty:
            return sig
    # 出场/止损价：用"入场当日收盘"为基准（引擎的 holding 模式只看 rl 与 stop_pct）
    sig["rh"] = sig["close"] * 1.5
    sig["rl"] = sig["close"]
    # 回放引擎按 `cause_width` **升序**挑当天的名额
    if p["chip_rank"] and "chip_price_vs_cost" in sig.columns:
        # 资金流 z 排名（越大越前） + 现价偏离成本排名（越小越前），两个横截面排名各占一半
        rank_mf = sig.groupby("trade_date")["moneyflow_net_z_5d"].rank(ascending=False)
        rank_chip = sig.groupby("trade_date")["chip_price_vs_cost"].rank(ascending=True)
        sig["cause_width"] = rank_mf + rank_chip
    else:
        sig["cause_width"] = -sig["moneyflow_net_z_5d"]
    return sig[["stock_code", "trade_date", "close", "rh", "rl", "cause_width",
                "moneyflow_net_z_5d", "moneyflow_positive_ratio_10d", "ret20",
                "amount_ma"]].reset_index(drop=True)


def replay(signals: pd.DataFrame, bars: pd.DataFrame, bench: pd.Series, **kw) -> dict:
    p = {**DEFAULTS, **kw}
    sig = signals.sort_values(["trade_date", "moneyflow_net_z_5d"], ascending=[True, False])
    return ws.replay(sig, bars, bench, exit_mode="hold", max_hold=p["max_hold"],
                     max_positions=p["max_positions"], stop_pct=p["stop_pct"])
