"""Explicit cash/inventory/fee ledger for P2.71 execution audits.

Fees are charged on actual gross notional for each side.  The ledger supports
long-only and long/short research books, partial reductions and cash-neutral
asset switches so the one-way/two-sided turnover mapping can be hand-checked.
It is a research accounting primitive, not a production A-share simulator.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ExecutionCost:
    commission_bps: float = 2.5
    stamp_duty_bps: float = 5.0       # sell side only
    transfer_fee_bps: float = 0.1
    slippage_bps: float = 5.0
    impact_bps: float = 2.9
    borrow_bps_annual: float = 0.0
    min_commission: float = 0.0

    def __post_init__(self):
        for name, value in asdict(self).items():
            if not np.isfinite(value) or value < 0:
                raise ValueError(f'{name} 必须为非负有限数')

    def one_way_rate(self, side: str) -> float:
        side = side.upper()
        if side not in ('BUY', 'SELL'):
            raise ValueError('side 必须是 BUY 或 SELL')
        core = self.commission_bps + self.transfer_fee_bps + self.slippage_bps + self.impact_bps
        if side == 'SELL':
            core += self.stamp_duty_bps
        return core / 1e4


class PortfolioLedger:
    def __init__(self, cash: float, cost: ExecutionCost = ExecutionCost()):
        if not np.isfinite(cash) or cash <= 0:
            raise ValueError('初始现金必须为正')
        self.initial_cash = float(cash)
        self.cash = float(cash)
        self.cost = cost
        self.positions: dict[str, dict[str, float]] = {}
        self.rows: list[dict] = []
        self.realized_pnl = 0.0

    def position(self, code: str) -> float:
        return self.positions.get(code, {}).get('shares', 0.0)

    def trade(self, date, code: str, side: str, quantity: float, price: float,
              *, reason: str = 'rebalance') -> dict:
        side = side.upper()
        if not np.isfinite(quantity) or quantity <= 0 or not np.isfinite(price) or price <= 0:
            raise ValueError('数量和价格必须为正')
        pos = self.positions.setdefault(code, {'shares': 0.0, 'avg_cost': 0.0})
        notional = float(quantity) * float(price)
        rate = self.cost.one_way_rate(side)
        variable_fee = notional * rate
        fee = max(variable_fee, self.cost.min_commission) if side == 'BUY' else variable_fee
        if side == 'BUY':
            self.cash -= notional + fee
            old = pos['shares']
            if old < 0:
                closed = min(quantity, -old)
                pnl = (pos['avg_cost'] - price) * closed - fee * closed / quantity
                self.realized_pnl += pnl
                remaining = quantity - closed
                if remaining <= 1e-12:
                    pos['shares'] = old + closed
                    if abs(pos['shares']) <= 1e-12:
                        pos['shares'] = 0.0
                        pos['avg_cost'] = 0.0
                else:
                    pos['shares'] = remaining
                    pos['avg_cost'] = (notional + fee) / quantity
            else:
                new_shares = old + quantity
                pos['avg_cost'] = ((pos['avg_cost'] * old) + notional + fee) / new_shares
                pos['shares'] = new_shares
                pnl = None
        else:
            self.cash += notional - fee
            old = pos['shares']
            if old > 0:
                closed = min(quantity, old)
                pnl = (price - pos['avg_cost']) * closed - fee * closed / quantity
                self.realized_pnl += pnl
                remaining = quantity - closed
                if remaining <= 1e-12:
                    pos['shares'] = old - closed
                    if abs(pos['shares']) <= 1e-12:
                        pos['shares'] = 0.0
                        pos['avg_cost'] = 0.0
                else:
                    pos['shares'] = -remaining
                    pos['avg_cost'] = (notional - fee) / quantity
            else:
                new_shares = old - quantity
                pos['avg_cost'] = ((pos['avg_cost'] * abs(old)) + notional - fee) / abs(new_shares)
                pos['shares'] = new_shares
                pnl = None
        row = {'trade_date': date, 'code': code, 'side': side, 'quantity': float(quantity),
               'price': float(price), 'gross': notional, 'fee': float(fee),
               'fee_bps': float(fee / notional * 1e4), 'net_cash': float(
                   -(notional + fee) if side == 'BUY' else notional - fee),
               'cash_after': float(self.cash), 'shares_after': float(pos['shares']),
               'avg_cost_after': float(pos['avg_cost']), 'realized_pnl': pnl, 'reason': reason}
        self.rows.append(row)
        return row

    def mark(self, prices: dict[str, float]) -> dict:
        missing = [c for c, p in self.positions.items() if p['shares'] > 0 and c not in prices]
        if missing:
            raise ValueError(f'缺估值价格: {missing}')
        market_value = sum(p['shares'] * float(prices[c]) for c, p in self.positions.items()
                           if p['shares'] > 0)
        equity = self.cash + market_value
        return {'cash': float(self.cash), 'market_value': float(market_value),
                'equity': float(equity), 'nav': float(equity / self.initial_cash)}

    def rebalance(self, date, target_weights: dict[str, float],
                  prices: dict[str, float], *, reason: str = 'rebalance') -> list[dict]:
        """Trade to target weights using the current mark as the equity base.

        Buys are executed first in the returned order, sells first in cash
        terms; the implementation sells first for long-only books.  Fractional
        shares are allowed so the accounting is exact.
        """
        if any(not np.isfinite(w) or w < 0 for w in target_weights.values()):
            raise ValueError('目标权重必须非负')
        if sum(target_weights.values()) > 1 + 1e-12:
            raise ValueError('目标权重之和超过1')
        equity = self.mark(prices)['equity']
        orders = []
        for code in set(self.positions) | set(target_weights):
            current = self.position(code) * prices[code]
            target = equity * target_weights.get(code, 0.0)
            delta = target - current
            if abs(delta) > 1e-12:
                orders.append((delta, code))
        # Sells first so buys can use released cash.
        orders.sort(key=lambda x: (x[0] > 0, x[1]))
        fills = []
        for delta, code in orders:
            if delta < 0:
                fills.append(self.trade(date, code, 'SELL', -delta / prices[code], prices[code],
                                        reason=reason))
            elif delta > 0:
                fills.append(self.trade(date, code, 'BUY', delta / prices[code], prices[code],
                                        reason=reason))
        return fills


def fee_summary(rows: Iterable[dict], initial_cash: float) -> dict:
    rows = list(rows)
    buy = sum(r['gross'] for r in rows if r['side'] == 'BUY')
    sell = sum(r['gross'] for r in rows if r['side'] == 'SELL')
    fees = sum(r['fee'] for r in rows)
    return {
        'trades': len(rows),
        'buy_notional': buy,
        'sell_notional': sell,
        'two_sided_notional': buy + sell,
        'one_way_turnover_fraction': (buy + sell) / 2 / initial_cash,
        'two_sided_turnover_fraction': (buy + sell) / initial_cash,
        'fees': fees,
        'fee_fraction_initial_cash': fees / initial_cash,
    }


def rebalance_timeline(calendar, signal_date, *, execution_lag: int = 1) -> dict:
    """Resolve signal/decision/fill/effective/first-return dates explicitly.

    A close-t signal is decided after t's close and filled at the open of the
    session ``execution_lag`` sessions later.  The position is effective at
    that fill; the first mark-to-market P&L is the fill-day open-to-close
    return.  This removes the ambiguous ``i+lag+1`` indexing convention.
    """
    dates = pd.DatetimeIndex(pd.to_datetime(list(calendar))).normalize()
    if dates.empty or dates.has_duplicates or not dates.is_monotonic_increasing:
        raise ValueError('日历必须非空、唯一且递增')
    if not isinstance(execution_lag, int) or isinstance(execution_lag, bool) or execution_lag < 1:
        raise ValueError('execution_lag 必须为正整数')
    signal = pd.Timestamp(signal_date).normalize()
    idx = int(dates.searchsorted(signal))
    if idx >= len(dates) or dates[idx] != signal:
        raise ValueError('信号日不在交易日历中')
    fill_idx = idx + execution_lag
    if fill_idx >= len(dates):
        raise ValueError('成交日超出日历')
    fill = dates[fill_idx]
    return {'signal_date': signal, 'decision_date': signal, 'fill_date': fill,
            'effective_date': fill, 'first_return_date': fill,
            'fill_price': 'next session open', 'holding_age_sessions': 0}


def periodic_rebalances(calendar, *, every: int = 20, execution_lag: int = 1,
                        first_signal: str | None = None) -> list[dict]:
    """Every-``every``-session rebalance schedule with the same explicit lag."""
    dates = pd.DatetimeIndex(pd.to_datetime(list(calendar))).normalize()
    if not isinstance(every, int) or isinstance(every, bool) or every <= 0:
        raise ValueError('every 必须为正整数')
    start = int(dates.searchsorted(pd.Timestamp(first_signal).normalize())) if first_signal else 0
    out = []
    for i in range(start, len(dates) - execution_lag, every):
        out.append(rebalance_timeline(dates, dates[i], execution_lag=execution_lag))
    return out
