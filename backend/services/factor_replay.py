"""Independent P2.71 research replay; no legacy model, selector or replay imports.

Close-t signals execute next calendar session open. Slots use that opening equity,
never the execution-day close. Fees apply to actual gross each side. Missing opens
postpone exits; missing closes carry the last mark. Fractional shares, qfq prices,
no end liquidation: this is not a production A-share execution simulator.
"""
from __future__ import annotations

from dataclasses import dataclass
from numbers import Integral
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ReplayConfig:
    max_positions: int = 5
    max_hold: int = 10
    stop_pct: float = 0.08
    cost_one_way: float = 15.5 / 1e4
    initial_cash: float = 1.0
    stop_basis: str = 'signal_close'
    holding_clock: str = 'valid_close'
    # Research-only drawdown throttle.  None preserves the frozen baseline;
    # when set, new entries are scaled between soft and hard drawdown levels.
    drawdown_soft: float | None = None
    drawdown_hard: float | None = None
    drawdown_floor: float = 1.0

    def __post_init__(self):
        for name in ('max_positions', 'max_hold'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, Integral) or value <= 0:
                raise ValueError(f'{name}须为正整数')
        for name in ('stop_pct', 'cost_one_way', 'initial_cash'):
            value = getattr(self, name)
            if isinstance(value, bool) or not np.isfinite(value):
                raise ValueError(f'{name}须为有限数')
        if not 0 <= self.stop_pct < 1 or not 0 <= self.cost_one_way < 1 or self.initial_cash <= 0:
            raise ValueError('回放金额/费用/止损配置非法')
        if self.stop_basis not in ('signal_close', 'entry_price'):
            raise ValueError('未知止损基准')
        if self.holding_clock not in ('valid_close', 'market_sessions'):
            raise ValueError('未知持仓计时方式')
        if self.drawdown_soft is None and self.drawdown_hard is None:
            if self.drawdown_floor != 1.0:
                raise ValueError('未启用回撤节流时floor必须为1')
        elif self.drawdown_soft is None or self.drawdown_hard is None:
            raise ValueError('回撤节流必须同时设置soft和hard')
        elif not (np.isfinite(self.drawdown_soft) and np.isfinite(self.drawdown_hard)
                  and 0 <= self.drawdown_soft < self.drawdown_hard < 1):
            raise ValueError('回撤节流阈值非法')
        if not np.isfinite(self.drawdown_floor) or not 0 <= self.drawdown_floor <= 1:
            raise ValueError('回撤节流floor非法')


def _dates(values, name):
    dates = pd.DatetimeIndex(pd.to_datetime(values, errors='raise'))
    if dates.hasnans or dates.tz is not None or not dates.equals(dates.normalize()):
        raise ValueError(f'{name}须为无时区、无缺失的日频日期')
    return dates


def _panel(frame, required, name):
    if not required <= set(frame.columns):
        raise ValueError(f'{name}缺列: {sorted(required-set(frame.columns))}')
    out = frame[list(sorted(required))].copy()
    if out.stock_code.isna().any() or (out.stock_code.astype(str).str.len() == 0).any():
        raise ValueError(f'{name}股票代码为空')
    out['stock_code'] = out.stock_code.astype(str)
    out['trade_date'] = _dates(out.trade_date, name)
    if out.duplicated(['stock_code', 'trade_date']).any():
        raise ValueError(f'{name}面板键重复')
    for column in required - {'stock_code', 'trade_date'}:
        out[column] = pd.to_numeric(out[column], errors='raise')
    return out


def _valid(value):
    return value is not None and np.isfinite(value) and value > 0


def replay(signals: pd.DataFrame, bars: pd.DataFrame, benchmark: pd.Series | None = None,
           config: ReplayConfig = ReplayConfig(), calendar=None) -> dict[str, Any]:
    bars = _panel(bars, {'stock_code', 'trade_date', 'open', 'close'}, '行情')
    raw_target_weights = None
    if 'target_weight' in signals.columns:
        raw_target_weights = signals[['stock_code', 'trade_date', 'target_weight']].copy()
        raw_target_weights['stock_code'] = raw_target_weights.stock_code.astype(str)
        raw_target_weights['trade_date'] = _dates(raw_target_weights.trade_date, '信号权重')
        raw_target_weights['target_weight'] = pd.to_numeric(raw_target_weights.target_weight, errors='raise')
        if raw_target_weights.duplicated(['stock_code', 'trade_date']).any() or not np.isfinite(raw_target_weights.target_weight).all() or (raw_target_weights.target_weight < 0).any():
            raise ValueError('信号权重非法')
    signals = _panel(signals, {'stock_code', 'trade_date', 'cause_width', 'rl'}, '信号')
    if raw_target_weights is not None:
        signals = signals.merge(raw_target_weights, on=['stock_code', 'trade_date'], how='left', validate='one_to_one')
        signals['target_weight'] = signals.target_weight.fillna(0.0)
    if not np.isfinite(signals[['cause_width', 'rl']].to_numpy(dtype=float)).all() or (signals.rl <= 0).any():
        raise ValueError('信号排序和止损基准须为有限数且止损基准为正')
    dates = _dates(sorted(bars.trade_date.unique()) if calendar is None else calendar, '日历')
    if dates.empty or dates.has_duplicates or not dates.is_monotonic_increasing:
        raise ValueError('回放日历须非空、严格递增且无重复')
    bnav = None
    if benchmark is not None:
        benchmark = pd.Series(benchmark).copy()
        benchmark.index = _dates(benchmark.index, '基准')
        if benchmark.index.has_duplicates:
            raise ValueError('基准日期重复')
        aligned = pd.to_numeric(benchmark.reindex(dates), errors='raise')
        if not np.isfinite(aligned).all() or (aligned <= 0).any():
            raise ValueError('基准必须覆盖每个回放日期且价格为有限正数；不填补')
        bnav = aligned / aligned.iloc[0]
        bnav.index.name = 'trade_date'
    bars_by_day = bars.groupby('trade_date', sort=False)
    signals = signals.sort_values(['trade_date', 'cause_width', 'stock_code'], kind='stable')
    sig_by_day = {d: g.to_dict('records') for d, g in signals.groupby('trade_date', sort=False)}
    cash = float(config.initial_cash)
    positions: dict[str, dict[str, Any]] = {}
    pending: dict[str, dict[str, Any]] = {}
    daily, ledger, holdings, events = [], [], [], []
    high_water_equity = float(config.initial_cash)

    for i, day in enumerate(dates):
        if day in bars_by_day.groups:
            today = bars_by_day.get_group(day).set_index('stock_code')
            opens, closes = today.open.to_dict(), today.close.to_dict()
        else:
            opens, closes = {}, {}
        start_ledger = len(ledger)
        # Exit requests persist over missing opens, rather than being silently lost.
        for code in list(pending):
            pos, order = positions[code], pending[code]
            px = opens.get(code)
            if not _valid(px):
                events.append({'trade_date': day, 'code': code, 'event': 'deferred_exit_missing_open'})
                continue
            gross = pos['shares'] * float(px)
            fee = gross * config.cost_one_way
            proceeds = gross - fee
            cash += proceeds
            ledger.append({'side': 'SELL', 'code': code, 'signal_date': pos['signal_date'],
                           'decision_date': order['decision_date'], 'trade_date': day, 'price': float(px),
                           'shares': pos['shares'], 'gross': gross, 'fee': fee, 'net_cash': proceeds,
                           'cash_after': cash, 'entry_cost': pos['entry_cost'],
                           'realized_pnl': proceeds - pos['entry_cost'],
                           'return_after_cost': proceeds / pos['entry_cost'] - 1,
                           'holding_sessions': pos['holding_sessions'], 'reason': order['reason']})
            del positions[code], pending[code]
        opening_equity = cash + sum(p['shares'] * (float(opens[c]) if _valid(opens.get(c)) else p['mark'])
                                    for c, p in positions.items())
        opening_drawdown = max(0.0, 1.0 - opening_equity / high_water_equity) if high_water_equity > 0 else 0.0
        if config.drawdown_soft is None:
            risk_scale = 1.0
        elif opening_drawdown <= config.drawdown_soft:
            risk_scale = 1.0
        elif opening_drawdown >= config.drawdown_hard:
            risk_scale = config.drawdown_floor
        else:
            span = config.drawdown_hard - config.drawdown_soft
            risk_scale = 1.0 - (opening_drawdown - config.drawdown_soft) / span * (1.0 - config.drawdown_floor)
        # Signal expiration: only t-1 calendar signals can buy today. Unfilled buys expire.
        for row in sig_by_day.get(dates[i - 1], []) if i > 0 else []:
            code = row['stock_code']
            if code in positions or len(positions) >= config.max_positions:
                continue
            px = opens.get(code)
            if not _valid(px):
                events.append({'trade_date': day, 'code': code, 'event': 'expired_buy_missing_open'})
                continue
            requested = (opening_equity * float(row['target_weight']) * risk_scale
                         if 'target_weight' in row and float(row['target_weight']) > 0
                         else opening_equity / config.max_positions * risk_scale)
            gross = min(requested, max(cash, 0) / (1 + config.cost_one_way))
            if gross <= 0:
                continue
            fee = gross * config.cost_one_way
            cash -= gross + fee
            if cash < -1e-12 * config.initial_cash:
                raise AssertionError('现金透支')
            cash = max(cash, 0.0)
            price = float(px)
            shares = gross / price
            positions[code] = {'shares': shares, 'entry_px': price, 'entry_cost': gross + fee,
                               'signal_date': dates[i - 1], 'holding_sessions': 0, 'mark': price,
                               'mark_date': day, 'stop_price': float(row['rl']) if config.stop_basis == 'signal_close' else price}
            ledger.append({'side': 'BUY', 'code': code, 'signal_date': dates[i - 1],
                           'decision_date': dates[i - 1], 'trade_date': day, 'price': price,
                           'shares': shares, 'gross': gross, 'fee': fee, 'net_cash': -(gross + fee),
                           'cash_after': cash, 'entry_cost': gross + fee, 'realized_pnl': None,
                           'return_after_cost': None, 'holding_sessions': 0, 'reason': 'next_open'})
        equity, stale_count = cash, 0
        for code, pos in positions.items():
            close = closes.get(code)
            valid_close = _valid(close)
            if valid_close:
                pos['mark'], pos['mark_date'] = float(close), day
            else:
                stale_count += 1
            if valid_close or config.holding_clock == 'market_sessions':
                pos['holding_sessions'] += 1
            equity += pos['shares'] * pos['mark']
            reason = None
            if valid_close and float(close) < pos['stop_price'] * (1 - config.stop_pct):
                reason = f'stop_{config.stop_basis}'
            elif pos['holding_sessions'] >= config.max_hold:
                reason = f'max_hold_{config.holding_clock}'
            if reason and code not in pending:
                pending[code] = {'reason': reason, 'decision_date': day}
            holdings.append({'trade_date': day, 'code': code, **pos, 'stale_close': not valid_close,
                             'market_value': pos['shares'] * pos['mark']})
        fills = ledger[start_ledger:]
        daily.append({'trade_date': day, 'nav': equity / config.initial_cash, 'equity': equity, 'cash': cash,
                      'market_value': equity - cash, 'n_positions': len(positions), 'stale_positions': stale_count,
                      'pending_exits': len(pending), 'opening_equity': opening_equity,
                      'gross_traded': sum(x['gross'] for x in fills), 'fees': sum(x['fee'] for x in fills),
                      'two_sided_turnover': sum(x['gross'] for x in fills) / opening_equity,
                      'opening_drawdown': opening_drawdown, 'risk_scale': risk_scale,
                      'high_water_equity': high_water_equity})
        high_water_equity = max(high_water_equity, equity)
    daily_frame = pd.DataFrame(daily).set_index('trade_date')
    ledger_columns = ['side', 'code', 'signal_date', 'decision_date', 'trade_date', 'price', 'shares',
                      'gross', 'fee', 'net_cash', 'cash_after', 'entry_cost', 'realized_pnl',
                      'return_after_cost', 'holding_sessions', 'reason']
    return {'nav': daily_frame.nav, 'benchmark_nav': bnav, 'daily': daily_frame,
            'ledger': pd.DataFrame(ledger, columns=ledger_columns), 'holdings': pd.DataFrame(holdings),
            'events': pd.DataFrame(events, columns=['trade_date', 'code', 'event']),
            'open_positions': positions, 'pending_exits': pending, 'config': config}


def metrics(nav: pd.Series) -> dict[str, float | int | None]:
    nav = pd.Series(nav).astype(float)
    if nav.empty:
        raise ValueError('净值为空')
    nav.index = _dates(nav.index, '净值')
    if nav.index.has_duplicates or not nav.index.is_monotonic_increasing or not np.isfinite(nav).all() or (nav <= 0).any():
        raise ValueError('净值须按唯一日期递增，数值须有限且为正')
    relative = nav / nav.iloc[0]
    ret = nav.pct_change().dropna()
    days = (nav.index[-1] - nav.index[0]).days
    std = float(ret.std(ddof=1)) if len(ret) > 1 else 0.0
    return {'observations': len(nav), 'total_return': float(relative.iloc[-1] - 1),
            'calendar_cagr': float(relative.iloc[-1] ** (365.25 / days) - 1) if days > 0 else None,
            'max_drawdown': float((relative / relative.cummax() - 1).min()),
            'annual_vol': std * float(np.sqrt(252)),
            'sharpe': float(ret.mean() / std * np.sqrt(252)) if std > 0 else None,
            'win_rate_daily': float((ret > 0).mean()) if len(ret) else None}
