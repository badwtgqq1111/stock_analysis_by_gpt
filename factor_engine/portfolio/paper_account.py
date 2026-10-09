"""Versioned daily-bar paper account with deterministic T+1 fills and NAV.

执行假设（成本 / 整手 / 涨跌停 / 停牌 / 退市）**统一**来自
`factor_engine/portfolio/execution_assumptions.py`（读 `config/execution_assumptions_cn.json`）。
2026-10-09 之前这里是硬编码的 5bp+5bp 且无印花税、不建模涨跌停与停牌，
与 `scripts/replay_daily_execution.py`（L3 回放）口径不同，两边净值不可比。
`enforce_frictions=False` 可退回"只有成本和整手"的旧行为，仅用于 A/B 对照。
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd

from factor_engine.portfolio.execution_assumptions import (
    cost_model,
    is_limit_down,
    is_limit_up,
    load_assumptions,
)


def run_paper_account(
    selections: pd.DataFrame,
    bars: pd.DataFrame,
    *,
    account_id="cn_default",
    strategy_version="v1",
    initial_capital=1_000_000.0,
    commission_bps=None,
    slippage_bps=None,
    lot_size=None,
    assumptions: dict | str | None = None,
    enforce_frictions: bool = True,
) -> dict[str, pd.DataFrame]:
    """Replay selection snapshots with next-session fills and daily marks.

    The account treats each selection date as a target-weight rebalance. Orders
    use the first later available bar, enforcing T+1 from a daily data source.
    Missing bars leave orders pending; they are never filled with future data.

    ``enforce_frictions`` 打开时额外建模（与 L3 回放同一套假设）：
      * 涨停不买（执行日涨幅达上限 → 本轮回合放弃买入，与回放一致）；
      * 跌停卖不出 / 涨停不卖（顺延到下一个可以卖的交易日，不丢单）；
      * 停牌（执行日无 bar）→ 买单挂 pending、卖单继续等，持仓按最后可得价盯市；
      * 连续 N（默认 20）个交易日无行情 → 视同退市，按最后可得价强制清算；
      * 卖出计印花税。
    """
    required = {"stock_code", "trade_date"}
    if selections is None or selections.empty or required - set(selections.columns):
        raise ValueError("selections require stock_code and trade_date")
    cfg = assumptions if isinstance(assumptions, dict) else load_assumptions(assumptions)
    costs = cost_model(cfg)
    commission = float(costs["commission"]) if commission_bps is None else float(commission_bps) / 10_000
    slippage = float(costs["slippage"]) if slippage_bps is None else float(slippage_bps) / 10_000
    stamp = float(costs["stamp_tax_sell"]) if enforce_frictions else 0.0
    lot = int(cfg["lot_size"] if lot_size is None else lot_size)
    delist_after = int(cfg["suspension"]["delist_after_missing_sessions"])
    no_sell_limit_up = bool(cfg["price_limit"].get("no_sell_on_limit_up", True))

    market = _prepare_bars(bars)
    signals = selections.copy()
    signals["trade_date"] = pd.to_datetime(signals["trade_date"], errors="coerce")
    signals = signals.dropna(subset=["trade_date", "stock_code"]).sort_values(["trade_date", "stock_code"])
    if "target_weight" not in signals.columns:
        signals["target_weight"] = signals.groupby("trade_date")["stock_code"].transform(lambda s: 1.0 / len(s))
    signals["target_weight"] = pd.to_numeric(signals["target_weight"], errors="coerce").fillna(0.0).clip(lower=0.0)
    dates = sorted(market["trade_date"].unique())
    if not dates:
        return {name: pd.DataFrame() for name in ("orders", "fills", "positions", "nav", "outcomes")}
    cash = float(initial_capital)
    quantities: dict[str, int] = {}
    orders, fills, positions, nav_rows = [], [], [], []
    peak = float(initial_capital)
    run_id = str(uuid4())
    by_date = {date: rows for date, rows in signals.groupby("trade_date", sort=False)}
    deferred_sells: dict[str, str] = {}      # code -> 延期原因（跌停卖不出 / 涨停不卖 / 停牌）
    stale: dict[str, int] = {}               # code -> 连续多少个交易日没有行情
    last_close: dict[str, float] = {}
    for date in dates:
        day = market[market["trade_date"] == date].set_index("stock_code")
        # ── 0a) 盯市缓存 + 退市强制清算（连续 N 日无行情） ──────────────────────
        for code in list(quantities):
            if code in day.index:
                stale[code] = 0
                last_close[code] = float(day.loc[code, "close"])
                continue
            stale[code] = stale.get(code, 0) + 1
            if stale[code] >= delist_after:
                price = float(last_close.get(code, 0.0))
                shares = quantities.pop(code)
                proceeds = shares * price * (1 - commission - slippage - stamp)
                cash += proceeds
                deferred_sells.pop(code, None)
                stale.pop(code, None)
                fills.append({"order_id": str(uuid4()), "run_id": run_id, "account_id": account_id,
                              "strategy_version": strategy_version, "stock_code": code, "decision_time": None,
                              "executable_from": date, "side": "force_liquidate", "target_value": 0.0,
                              "status": "forced_liquidation", "fill_time": date, "quantity": shares,
                              "price": price, "commission": shares * price * (commission + stamp),
                              "slippage_bps": slippage_bps, "participation_rate": None})
        # ── 0b) 延期卖单重试（跌停/涨停/停牌顺延，不丢单） ─────────────────────
        for code in list(deferred_sells):
            if code not in quantities:
                deferred_sells.pop(code, None)
                continue
            if code not in day.index:
                continue
            price = _exec_price(day.loc[code])
            prev = _prev_close(day.loc[code])
            if is_limit_down(code, date, price, prev, cfg):
                continue
            if no_sell_limit_up and is_limit_up(code, date, price, prev, cfg):
                continue
            shares = quantities.pop(code)
            value = shares * price
            fee = value * (commission + stamp)
            cash += value - fee
            fills.append(_fill(run_id, account_id, strategy_version, code, None, date, "sell", shares,
                               price, fee, slippage_bps))
            deferred_sells.pop(code, None)
        if date in by_date:
            snapshot = by_date[date]
            next_date = _next_trade_date(dates, date)
            nav_before = cash + sum(qty * _mark(day, last_close, code) for code, qty in quantities.items())
            targets = {str(row.stock_code): float(row.target_weight) for row in snapshot.itertuples()}
            if next_date is None:
                # The newest decision does not yet have a later daily bar.
                # Retain a pending intent for auditability; the next daily run
                # rebuilds it against the first available T+1 session.
                for code, target_weight in sorted(targets.items()):
                    if target_weight > 0:
                        orders.append(
                            _order(
                                run_id, account_id, strategy_version, code, date, None,
                                "pending", nav_before * target_weight,
                            )
                        )
            else:
                executable = market[market["trade_date"] == next_date].set_index("stock_code")
                for code in sorted(set(quantities) | set(targets)):
                    target_value = nav_before * targets.get(code, 0.0)
                    if code not in executable.index:
                        orders.append(_order(run_id, account_id, strategy_version, code, date, next_date,
                                             "pending", target_value, reason="suspended"))
                        continue
                    row = executable.loc[code]
                    price = _exec_price(row)
                    prev = _prev_close(row)
                    target_qty = int(np.floor(target_value / max(price, 1e-12) / lot) * lot)
                    delta = target_qty - quantities.get(code, 0)
                    if delta == 0:
                        continue
                    side = "buy" if delta > 0 else "sell"
                    if enforce_frictions and is_limit_up(code, next_date, price, prev, cfg):
                        if side == "buy":
                            orders.append(_order(run_id, account_id, strategy_version, code, date, next_date,
                                                 "skipped_limit_up", target_value, side=side,
                                                 reason="limit_up_no_buy"))
                            continue
                        if no_sell_limit_up:
                            deferred_sells[code] = "limit_up_no_sell"
                            orders.append(_order(run_id, account_id, strategy_version, code, date, next_date,
                                                 "deferred_limit_up", target_value, side=side,
                                                 reason="limit_up_no_sell"))
                            continue
                    if enforce_frictions and side == "sell" and is_limit_down(code, next_date, price, prev, cfg):
                        deferred_sells[code] = "limit_down_no_sell"
                        orders.append(_order(run_id, account_id, strategy_version, code, date, next_date,
                                             "deferred_limit_down", target_value, side=side,
                                             reason="limit_down_no_sell"))
                        continue
                    signed_price = price * (1 + (1 if side == "buy" else -1) * slippage)
                    value = abs(delta) * signed_price
                    fee = value * (commission + (stamp if side == "sell" else 0.0))
                    if side == "buy" and value + fee > cash:
                        delta = int(np.floor(cash / (signed_price * (1 + commission)) / lot) * lot)
                        value = delta * signed_price
                        fee = value * commission
                    order = _order(run_id, account_id, strategy_version, code, date, next_date,
                                   "filled" if delta else "rejected", target_value, side=side)
                    orders.append(order)
                    if not delta:
                        continue
                    quantities[code] = quantities.get(code, 0) + delta
                    if quantities[code] == 0:
                        quantities.pop(code)
                    cash += -value - fee if side == "buy" else value - fee
                    fills.append({**order, "fill_time": next_date, "quantity": abs(delta), "price": signed_price,
                                  "commission": fee, "slippage_bps": slippage_bps, "participation_rate": None})
        value = cash
        for code, quantity in quantities.items():
            mark = _mark(day, last_close, code)
            market_value = quantity * mark
            value += market_value
            positions.append({"account_id": account_id, "run_id": run_id, "asof_date": date, "stock_code": code,
                              "quantity": quantity, "market_value": market_value, "close": mark})
        peak = max(peak, value)
        nav_rows.append({"account_id": account_id, "run_id": run_id, "asof_date": date, "cash": cash,
                         "market_value": value - cash, "nav": value, "drawdown": value / peak - 1.0})
    nav = pd.DataFrame(nav_rows)
    if not nav.empty:
        nav["daily_return"] = nav["nav"].pct_change().fillna(0.0)
    return {"orders": pd.DataFrame(orders), "fills": pd.DataFrame(fills), "positions": pd.DataFrame(positions),
            "nav": nav, "outcomes": pd.DataFrame()}


def _mark(day: pd.DataFrame, last_close: dict[str, float], code: str) -> float:
    """盯市价：当天有 bar 用收盘价；停牌/无行情沿用最后可得价（回放的 hold_last_price 口径）。"""
    if code in day.index:
        return float(day.loc[code, "close"])
    return float(last_close.get(code, 0.0))


def _exec_price(row) -> float:
    """成交参考价：优先开盘（T+1 开盘成交），没有开盘价退回收盘价。"""
    if "open" in row.index and pd.notna(row.get("open")):
        return float(row.get("open"))
    return float(row.get("close"))


def _prev_close(row) -> float | None:
    prev = row.get("prev_close") if hasattr(row, "get") else None
    return None if prev is None or pd.isna(prev) else float(prev)


def _fill(run_id, account_id, strategy_version, code, decision, day, side, quantity, price, fee, slippage_bps):
    return {"order_id": str(uuid4()), "run_id": run_id, "account_id": account_id,
            "strategy_version": strategy_version, "stock_code": code, "decision_time": decision,
            "executable_from": day, "side": side, "target_value": quantity * price, "status": "filled",
            "fill_time": day, "quantity": quantity, "price": price, "commission": fee,
            "slippage_bps": slippage_bps, "participation_rate": None}


def persist_paper_account(result: dict[str, pd.DataFrame], output_dir="output/paper_trading",
                          assumptions: dict | None = None) -> dict:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, frame in result.items():
        if not isinstance(frame, pd.DataFrame):
            continue
        path = directory / f"{name}.csv"
        frame.to_csv(path, index=False)
        paths[name] = str(path)
    nav = result.get("nav", pd.DataFrame())
    summary = {"nav_rows": int(len(nav)), "final_nav": float(nav["nav"].iloc[-1]) if not nav.empty else None, "max_drawdown": float(nav["drawdown"].min()) if not nav.empty else None}
    (directory / "paper_account_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    # 假设快照：产物自带"这份净值是按什么口径算的"，免得半年后没人说得清
    snapshot = assumptions if isinstance(assumptions, dict) else load_assumptions()
    (directory / "execution_assumptions.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"paths": paths, **summary}


def _prepare_bars(bars: pd.DataFrame) -> pd.DataFrame:
    required = {"stock_code", "trade_date", "close"}
    if bars is None or bars.empty or required - set(bars.columns):
        raise ValueError("bars require stock_code, trade_date and close")
    frame = bars.copy(); frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    if "open" in frame:
        frame["open"] = pd.to_numeric(frame["open"], errors="coerce")
    frame = (frame.dropna(subset=["stock_code", "trade_date", "close"]).query("close > 0")
             .sort_values(["trade_date", "stock_code"]).drop_duplicates(["trade_date", "stock_code"], keep="last"))
    # 涨跌停判定要"前收"：按每只票自己的时间序取上一根 bar 的收盘（与回放的 px.shift(1) 同口径）
    frame = frame.sort_values(["stock_code", "trade_date"])
    frame["prev_close"] = frame.groupby("stock_code", sort=False)["close"].shift(1)
    return frame.sort_values(["trade_date", "stock_code"])


def _next_trade_date(dates, current):
    index = dates.index(current)
    return dates[index + 1] if index + 1 < len(dates) else None


def _order(run_id, account_id, strategy_version, code, decision, executable, status, target_value,
           side=None, reason=None):
    return {"order_id": str(uuid4()), "run_id": run_id, "account_id": account_id,
            "strategy_version": strategy_version, "stock_code": code, "decision_time": decision,
            "executable_from": executable, "side": side, "target_value": target_value,
            "status": status, "reason": reason}
