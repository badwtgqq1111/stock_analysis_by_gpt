#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""逐笔级日频回放：把"月末选股"落成**真实的日度持仓与成交**（L3 的第三个口径）。

与 `--mark-daily` 的区别（那些假设在这里被显式建模）：
  * **停牌不可交易**：调仓日拿不到价格的票，既买不进也卖不出（持仓按最后价保留）；
  * **涨跌停不可成交**：买入日涨停不追、卖出日跌停不砸（按板块判定 10% / 20% 上限）；
  * **整手可买**：买入按 100 股取整，剩余留现金（与 `[selection].initial_capital` 的现实一致）；
  * **退市强制清算**：连续 20 个交易日无行情 → 按最后可得价格清算为现金；
  * 成本：佣金 2.5bp + 滑点 3bp（双向）+ 印花税 10bp（卖出）。

用法:
  uv run python scripts/replay_daily_execution.py \
    --picks output/research/llm_factor_mining_20261007_155107/sleeve_oos_v4d/picks.csv \
    --arm base_v1 --out-dir output/research/l3_replay
输出:
  <out-dir>/<arm>_{nav.csv,trades.csv,metrics.json}
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from factor_engine.portfolio.execution_assumptions import (  # noqa: E402
    cost_model,
    curse_month,
    load_assumptions,
    price_limit_ratio,
)
# 执行假设**不再硬编码**：统一从 config/execution_assumptions_cn.json 读
# （与模拟盘 factor_engine/portfolio/paper_account.py 同一份，见 execution_assumptions.py）。
# 命令行 --assumptions 可换一份假设做敏感性测试；parity 检查见 scripts/execution_parity_check.py。
_ASSUMPTIONS = load_assumptions()
_COSTS = cost_model(_ASSUMPTIONS)
COMMISSION, SLIPPAGE, STAMP = _COSTS["commission"], _COSTS["slippage"], _COSTS["stamp_tax_sell"]
LOT = int(_ASSUMPTIONS["lot_size"])
SUSPEND_LIMIT = int(_ASSUMPTIONS["suspension"]["delist_after_missing_sessions"])   # 连续无行情视为退市
_TOL = float(_ASSUMPTIONS["price_limit"].get("tolerance", 0.005))


def atr_matrix(start: str) -> pd.DataFrame:
    """ATR14%（(高−低)/前收 的 14 日均值）——与仓库 exit_plan 的止损口径一致。"""
    import pyarrow.dataset as ds
    files = [str(p) for y in range(int(start[:4]), 2027)
             for p in (ROOT / "assets/data/clean/ohlcv/market=CN").glob(
                 f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={y}/*.parquet")]
    bars = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "high", "low", "close"]).to_pandas()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    bars = bars[bars["trade_date"] >= pd.Timestamp(start)].sort_values(["stock_code", "trade_date"])
    bars["tr"] = (bars["high"] - bars["low"]) / bars.groupby("stock_code", sort=False)["close"].shift(1)
    bars["atr"] = bars.groupby("stock_code", sort=False)["tr"].transform(lambda s: s.rolling(14, min_periods=7).mean())
    return bars.pivot_table(index="trade_date", columns="stock_code", values="atr", aggfunc="last").sort_index()


def price_matrix(start: str) -> pd.DataFrame:
    import pyarrow.dataset as ds
    files = [str(p) for y in range(int(start[:4]), 2027)
             for p in (ROOT / "assets/data/clean/ohlcv/market=CN").glob(
                 f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={y}/*.parquet")]
    bars = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "close"]).to_pandas()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    dl_path = ROOT / "assets/data/meta/cn_delisted_daily.parquet"
    if dl_path.exists():
        dl = pd.read_parquet(dl_path, columns=["stock_code", "trade_date", "close"])
        dl["trade_date"] = pd.to_datetime(dl["trade_date"])
        bars = pd.concat([bars, dl], ignore_index=True)
    bars = bars[bars["trade_date"] >= pd.Timestamp(start)]
    bars = bars.drop_duplicates(["trade_date", "stock_code"], keep="last")
    return bars.pivot_table(index="trade_date", columns="stock_code", values="close", aggfunc="last").sort_index()


# 涨跌停幅度 / 魔咒月判定都来自共享假设模块（原来这里各写一份，容易和模拟盘漂移）
price_limit = price_limit_ratio


def replay(picks: pd.DataFrame, px: pd.DataFrame, arm: str, capital: float = 45_000.0,
           atr: pd.DataFrame | None = None, use_exit_rules: bool = False,
           atr_mult: float = 2.5, stop_min: float = 0.04, stop_max: float = 0.10,
           max_weight: float = 0.35, market_guard: bool = False,
           market_drop: float = 0.05, guard_cooldown: int = 10,
           curse_months: bool = False, vol_target: float = 0.0) -> dict:
    picks = picks[picks["arm"] == arm]
    if picks.empty:
        return {}
    targets = {m: g.set_index("stock_code")["weight"].to_dict() for m, g in picks.groupby("month")}
    # 只从**第一个决策日**开始计数：训练期(前 24 个月)不交易，若把那段算进年化会系统性低估收益
    first_month = sorted(targets)[0]
    all_days = list(px.index)
    month_last_all = pd.Series(all_days, index=px.index).groupby(px.index.to_period("M").astype(str)).max().to_dict()
    first_day = month_last_all.get(first_month, all_days[0])
    days = [d for d in all_days if d >= first_day]
    decision = {m: d for m, d in month_last_all.items() if m in targets}

    cash, pos = capital, {}                    # pos: code -> shares
    last_price: dict[str, float] = {}
    nav_rows, trades = [], []
    stats = {"skip_buy_suspend": 0, "skip_buy_limit": 0, "skip_sell_suspend": 0,
             "skip_sell_limit": 0, "forced_liquidation": 0, "buy_orders": 0, "sell_orders": 0,
             "stop_exits": 0, "deferred_sell_limitup": 0, "trim_max_weight": 0,
             "market_guard_trigger": 0, "curse_days_blocked": 0}
    stale = {c: 0 for c in []}
    # 组合级风控：大盘防线的市场代理 = 全样本等权日收益（近似"中小综指成分"）
    market_ret = px.pct_change().mean(axis=1)
    market_roll = market_ret.rolling(5).sum()
    guard_until = None
    exposure = 1.0
    entry_price: dict[str, float] = {}          # 建仓价，用于 ATR 止损
    pending: set[str] = set()                   # 待卖出（止损/调出，遇涨停或跌停顺延）
    prev_close = px.shift(1)
    for day in days:
        row = px.loc[day]
        pprev = prev_close.loc[day]
        in_curse = curse_month(day) if curse_months else False
        cooling = guard_until is not None and day <= guard_until
        # ---- 0a) 大盘防线：近 5 日市场累计跌幅超阈值 → 清仓 + 冷却（原文"立即清仓所有股票"）----
        if market_guard and pd.notna(market_roll.get(day)) and float(market_roll.get(day)) <= -market_drop:
            if not cooling:
                stats["market_guard_trigger"] += 1
            guard_until = day + pd.Timedelta(days=guard_cooldown * 2)   # 交易日近似（10 个交易日≈2 周日历）
            cooling = True
            pending.update(list(pos))                                   # 全部转入卖出闸门
        # ---- 0b) 魔咒月：清仓 + 不买入（当窗口内）----
        if in_curse:
            if not stats.get("curse_days_blocked"):
                stats["curse_days_blocked"] = 0
            stats["curse_days_blocked"] += 1
            pending.update(list(pos))
        # ---- 0) 每日：ATR 止损检查（仓库 exit_plan 口径：clamp(2.5×ATR14, 4%~10%) 自建仓价起算）----
        if use_exit_rules and atr is not None:
            for code in list(pos):
                if code in pending:
                    continue
                p, a = row.get(code), atr.loc[day].get(code)
                if pd.isna(p) or code not in entry_price:
                    continue
                if pd.notna(a):
                    stop_pct = min(max(atr_mult * float(a), stop_min), stop_max)
                    if float(p) <= entry_price[code] * (1 - stop_pct):
                        pending.add(code)
        for code in list(pos):
            p = row.get(code)
            if pd.notna(p):
                last_price[code] = float(p)
                stale[code] = 0
            else:
                stale[code] = stale.get(code, 0) + 1
                if stale[code] >= SUSPEND_LIMIT and code in pos:      # 退市强制清算
                    value = pos.pop(code) * last_price.get(code, 0.0)
                    cash += value * (1 - COMMISSION - SLIPPAGE - STAMP)
                    stats["forced_liquidation"] += 1
                    trades.append({"date": day, "code": code, "side": "force_liquidate",
                                   "price": last_price.get(code), "shares": 0})
        if day in decision.values():
            month = [m for m, d in decision.items() if d == day][0]
            target = targets[month]
            total_w = sum(target.values()) or 1.0
            # 大盘防线冷却中 / 魔咒月 → 本月不建仓（已持仓的会被闸门清掉）
            if cooling or in_curse:
                target = {}
            # ---- 先卖（不在目标里 / 权重要降）----
            for code in list(pos):
                if code not in target:
                    pending.add(code)                                 # 调出 → 走统一的卖出闸门
            for code in list(pending):
                if code in target:                                    # 又回到目标 → 撤销卖出
                    pending.discard(code)
            # ---- 再买（等权目标权重）----
            nav_now = cash + sum(s * last_price.get(c, 0.0) for c, s in pos.items())
            for code, weight in target.items():
                if code in pos:
                    continue
                p = row.get(code)
                if pd.isna(p):
                    stats["skip_buy_suspend"] += 1
                    continue
                prev = px.shift(1).loc[day].get(code)
                if pd.notna(prev) and float(p) / float(prev) - 1 >= price_limit(code, day) - _TOL:
                    stats["skip_buy_limit"] += 1                       # 涨停不追
                    continue
                scale = 1.0
                if vol_target > 0 and len(nav_rows) > 20:
                    # 用**已记录**的净值序列算已实现波动（nav 变量此时还没构建）
                    hist = pd.Series([r["nav"] for r in nav_rows]).pct_change().tail(20)
                    rv = float(hist.std() * np.sqrt(252)) if hist.std() else np.nan
                    if np.isfinite(rv) and rv > 0:
                        scale = min(1.0, vol_target / rv)
                budget = min(cash, nav_now * weight / total_w * scale)
                lots = int(budget / (float(p) * LOT))
                if lots <= 0:
                    continue
                shares = lots * LOT
                cost = shares * float(p)
                cash -= cost * (1 + COMMISSION + SLIPPAGE)
                pos[code] = pos.get(code, 0) + shares
                entry_price[code] = float(p)
                last_price[code] = float(p)
                stale[code] = 0
                stats["buy_orders"] += 1
                trades.append({"date": day, "code": code, "side": "buy", "price": float(p), "shares": shares})
            # ---- 单票权重上限（超限部分部分卖出；涨停不卖 → 顺延）----
            nav_after = cash + sum(s * last_price.get(c, 0.0) for c, s in pos.items())
            for code, shares in list(pos.items()):
                p = row.get(code)
                if pd.isna(p) or nav_after <= 0:
                    continue
                weight = shares * float(p) / nav_after
                if weight <= max_weight:
                    continue
                prev = pprev.get(code)
                if pd.notna(prev) and float(p) / float(prev) - 1 >= price_limit(code, day) - _TOL:
                    continue                                          # 涨停不卖
                sell_shares = int((weight - max_weight) * nav_after / float(p) / LOT) * LOT
                if sell_shares <= 0:
                    continue
                pos[code] = shares - sell_shares
                cash += sell_shares * float(p) * (1 - COMMISSION - SLIPPAGE - STAMP)
                stats["trim_max_weight"] += 1
                trades.append({"date": day, "code": code, "side": "trim", "price": float(p), "shares": sell_shares})
                if pos[code] == 0:
                    pos.pop(code)
        # ---- 1) 每日：执行待卖出（停牌不动；涨停不卖，等打开；跌停卖不掉）----
        for code in list(pending):
            if code not in pos:              # 已不在持仓（重复触发/已卖）→ 清理
                pending.discard(code)
                continue
            p = row.get(code)
            if pd.isna(p):
                continue
            prev = pprev.get(code)
            if pd.notna(prev):
                chg = float(p) / float(prev) - 1
                if chg >= price_limit(code, day) - _TOL:      # 涨停不卖
                    stats["deferred_sell_limitup"] += 1
                    continue
                # 跌停卖不掉（2026-10-09 补：注释里一直写着这一条，代码却只判了涨停；
                # 与模拟盘的执行假设对齐时被发现 —— stats["skip_sell_limit"] 此前恒为 0）
                if chg <= -(price_limit(code, day) - _TOL):
                    stats["skip_sell_limit"] += 1
                    continue
            shares = pos.pop(code)
            cash += shares * float(p) * (1 - COMMISSION - SLIPPAGE - STAMP)
            stats["sell_orders"] += 1
            reason = "stop" if (use_exit_rules and atr is not None and
                                float(p) <= entry_price.get(code, float("inf")) * (1 - stop_min)) else "rebalance"
            if reason == "stop":
                stats["stop_exits"] += 1
            trades.append({"date": day, "code": code, "side": "sell", "price": float(p),
                           "shares": shares, "reason": reason})
            pending.discard(code)
        nav = cash + sum(s * last_price.get(c, 0.0) for c, s in pos.items())
        nav_rows.append({"date": day, "nav": nav, "cash": cash, "n_pos": len(pos),
                         "n_suspended": sum(1 for c in pos if pd.isna(row.get(c)))})
    nav = pd.DataFrame(nav_rows).set_index("date")
    ret = nav["nav"].pct_change().dropna()
    dd = float((nav["nav"] / nav["nav"].cummax() - 1).min())
    ann = float(nav["nav"].iloc[-1] / capital) ** (252 / max(len(ret), 1)) - 1
    metrics = {"arm": arm, "assumptions_version": _ASSUMPTIONS.get("version"),
               "costs": {"commission_bps": COMMISSION * 10_000, "slippage_bps": SLIPPAGE * 10_000,
                         "stamp_tax_bps_sell": STAMP * 10_000},
               "lot_size": LOT, "delist_after_missing_sessions": SUSPEND_LIMIT,
               "days": int(len(ret)), "final_nav": float(nav["nav"].iloc[-1]),
               "ann_return": ann, "ann_vol": float(ret.std() * np.sqrt(252)),
               "sharpe": float(ret.mean() / ret.std() * np.sqrt(252)) if ret.std() else None,
               "max_drawdown": dd, "calmar": float(ann / abs(dd)) if dd < 0 else None,
               "trades": len(trades), **stats}
    return {"metrics": metrics, "nav": nav, "trades": pd.DataFrame(trades)}


def main() -> int:
    ap = argparse.ArgumentParser(description="逐笔级日频回放")
    ap.add_argument("--picks", required=True)
    ap.add_argument("--arm", default="base_v1")
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--capital", type=float, default=45_000.0)
    ap.add_argument("--out-dir", default="output/research/l3_replay")
    ap.add_argument("--market-guard", action="store_true", help="大盘防线：近 5 日全市场等权跌幅≤-5% → 清仓+冷却")
    ap.add_argument("--curse-months", action="store_true", help="魔咒月空仓：1/5~2/5 与 4/5~4/30 不建仓并清仓")
    ap.add_argument("--vol-target", type=float, default=0.0, help="波动目标（年化，如 0.18；0=关闭）")
    ap.add_argument("--assumptions", default=None, help="执行假设 JSON 路径（默认 config/execution_assumptions_cn.json）")
    ap.add_argument("--use-exit-rules", action="store_true",
                    help="启用仓库 exit_plan 口径：ATR 止损 clamp(2.5×ATR14,4%~10%) + 涨停不卖 + 单票权重≤35%")
    args = ap.parse_args()
    global _ASSUMPTIONS, _COSTS, COMMISSION, SLIPPAGE, STAMP, LOT, SUSPEND_LIMIT, _TOL
    if args.assumptions:
        _ASSUMPTIONS = load_assumptions(args.assumptions)
        _COSTS = cost_model(_ASSUMPTIONS)
        COMMISSION, SLIPPAGE, STAMP = _COSTS["commission"], _COSTS["slippage"], _COSTS["stamp_tax_sell"]
        LOT = int(_ASSUMPTIONS["lot_size"])
        SUSPEND_LIMIT = int(_ASSUMPTIONS["suspension"]["delist_after_missing_sessions"])
        _TOL = float(_ASSUMPTIONS["price_limit"].get("tolerance", 0.005))
    out = Path(args.out_dir) if Path(args.out_dir).is_absolute() else ROOT / args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    px = price_matrix(args.start)
    atr = atr_matrix(args.start) if args.use_exit_rules else None
    picks = pd.read_csv(args.picks)
    print(f"[replay] 价格 {px.shape[0]} 天 × {px.shape[1]} 只 | 持仓表 {len(picks):,} 行 | "
          f"退出规则={'开' if args.use_exit_rules else '关'}", flush=True)
    result = replay(picks, px, args.arm, args.capital, atr=atr, use_exit_rules=args.use_exit_rules,
                    market_guard=args.market_guard, curse_months=args.curse_months,
                    vol_target=args.vol_target)
    if not result:
        raise SystemExit(f"没有 {args.arm} 的持仓")
    tag = f"{args.arm}{'_stop' if args.use_exit_rules else ''}{'_guard' if args.market_guard else ''}" \
          f"{'_curse' if args.curse_months else ''}{'_voltgt' if args.vol_target else ''}"
    result["nav"].to_csv(out / f"{tag}_nav.csv")
    result["trades"].to_csv(out / f"{tag}_trades.csv", index=False)
    (out / f"{tag}_metrics.json").write_text(json.dumps(result["metrics"], ensure_ascii=False, indent=2),
                                                  encoding="utf-8")
    print(f"[replay] {args.arm}:", json.dumps(result["metrics"], ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
