"""Versioned daily-bar paper account with deterministic T+1 fills and NAV."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd


def run_paper_account(
    selections: pd.DataFrame,
    bars: pd.DataFrame,
    *,
    account_id="cn_default",
    strategy_version="v1",
    initial_capital=1_000_000.0,
    commission_bps=5.0,
    slippage_bps=5.0,
    lot_size=100,
) -> dict[str, pd.DataFrame]:
    """Replay selection snapshots with next-session fills and daily marks.

    The account treats each selection date as a target-weight rebalance. Orders
    use the first later available bar, enforcing T+1 from a daily data source.
    Missing bars leave orders pending; they are never filled with future data.
    """
    required = {"stock_code", "trade_date"}
    if selections is None or selections.empty or required - set(selections.columns):
        raise ValueError("selections require stock_code and trade_date")
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
    for date in dates:
        day = market[market["trade_date"] == date].set_index("stock_code")
        if date in by_date:
            snapshot = by_date[date]
            next_date = _next_trade_date(dates, date)
            if next_date is not None:
                executable = market[market["trade_date"] == next_date].set_index("stock_code")
                nav_before = cash + sum(qty * float(day.loc[code, "close"]) for code, qty in quantities.items() if code in day.index)
                targets = {str(row.stock_code): float(row.target_weight) for row in snapshot.itertuples()}
                for code in sorted(set(quantities) | set(targets)):
                    target_value = nav_before * targets.get(code, 0.0)
                    if code not in executable.index:
                        orders.append(_order(run_id, account_id, strategy_version, code, date, next_date, "pending", target_value))
                        continue
                    price = float(executable.loc[code, "open"] if "open" in executable and pd.notna(executable.loc[code, "open"]) else executable.loc[code, "close"])
                    target_qty = int(np.floor(target_value / max(price, 1e-12) / lot_size) * lot_size)
                    delta = target_qty - quantities.get(code, 0)
                    if delta == 0:
                        continue
                    side = "buy" if delta > 0 else "sell"
                    signed_price = price * (1 + (1 if side == "buy" else -1) * slippage_bps / 10_000)
                    value = abs(delta) * signed_price
                    fee = value * commission_bps / 10_000
                    if side == "buy" and value + fee > cash:
                        delta = int(np.floor(cash / (signed_price * (1 + commission_bps / 10_000)) / lot_size) * lot_size)
                        value = delta * signed_price
                        fee = value * commission_bps / 10_000
                    order = _order(run_id, account_id, strategy_version, code, date, next_date, "filled" if delta else "rejected", target_value, side=side)
                    orders.append(order)
                    if not delta:
                        continue
                    quantities[code] = quantities.get(code, 0) + delta
                    if quantities[code] == 0:
                        quantities.pop(code)
                    cash += -value - fee if side == "buy" else value - fee
                    fills.append({**order, "fill_time": next_date, "quantity": abs(delta), "price": signed_price, "commission": fee, "slippage_bps": slippage_bps, "participation_rate": None})
        value = cash
        for code, quantity in quantities.items():
            if code in day.index:
                close = float(day.loc[code, "close"])
                market_value = quantity * close
                value += market_value
                positions.append({"account_id": account_id, "run_id": run_id, "asof_date": date, "stock_code": code, "quantity": quantity, "market_value": market_value, "close": close})
        peak = max(peak, value)
        nav_rows.append({"account_id": account_id, "run_id": run_id, "asof_date": date, "cash": cash, "market_value": value - cash, "nav": value, "drawdown": value / peak - 1.0})
    nav = pd.DataFrame(nav_rows)
    if not nav.empty:
        nav["daily_return"] = nav["nav"].pct_change().fillna(0.0)
    return {"orders": pd.DataFrame(orders), "fills": pd.DataFrame(fills), "positions": pd.DataFrame(positions), "nav": nav, "outcomes": pd.DataFrame()}


def persist_paper_account(result: dict[str, pd.DataFrame], output_dir="output/paper_trading") -> dict:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, frame in result.items():
        path = directory / f"{name}.csv"
        frame.to_csv(path, index=False)
        paths[name] = str(path)
    nav = result.get("nav", pd.DataFrame())
    summary = {"nav_rows": int(len(nav)), "final_nav": float(nav["nav"].iloc[-1]) if not nav.empty else None, "max_drawdown": float(nav["drawdown"].min()) if not nav.empty else None}
    (directory / "paper_account_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"paths": paths, **summary}


def _prepare_bars(bars: pd.DataFrame) -> pd.DataFrame:
    required = {"stock_code", "trade_date", "close"}
    if bars is None or bars.empty or required - set(bars.columns):
        raise ValueError("bars require stock_code, trade_date and close")
    frame = bars.copy(); frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    if "open" in frame:
        frame["open"] = pd.to_numeric(frame["open"], errors="coerce")
    return frame.dropna(subset=["stock_code", "trade_date", "close"]).query("close > 0").sort_values(["trade_date", "stock_code"]).drop_duplicates(["trade_date", "stock_code"], keep="last")


def _next_trade_date(dates, current):
    index = dates.index(current)
    return dates[index + 1] if index + 1 < len(dates) else None


def _order(run_id, account_id, strategy_version, code, decision, executable, status, target_value, side=None):
    return {"order_id": str(uuid4()), "run_id": run_id, "account_id": account_id, "strategy_version": strategy_version, "stock_code": code, "decision_time": decision, "executable_from": executable, "side": side, "target_value": target_value, "status": status}
