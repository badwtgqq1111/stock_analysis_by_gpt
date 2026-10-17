import numpy as np
import pandas as pd
import pytest
from backend.services.factor_replay import ReplayConfig, metrics, replay


def bars():
    dates = pd.bdate_range('2025-01-01', periods=8)
    return pd.DataFrame({'stock_code':['A']*8, 'trade_date':dates,
                         'open':[10,10,9,9,9,9,9,9], 'close':[10,10,9,9,9,9,9,9]})


def signals():
    dates = pd.bdate_range('2025-01-01', periods=8)
    return pd.DataFrame({'stock_code':['A','A'], 'trade_date':[dates[0],dates[3]],
                         'cause_width':[0,0], 'rl':[10,9]})


def test_next_open_not_same_close_and_buy_fee_reserved():
    result = replay(signals(), bars(), config=ReplayConfig(max_positions=1, max_hold=2, cost_one_way=.1))
    ledger = result['ledger']
    assert ledger.iloc[0].side == 'BUY' and ledger.iloc[0].trade_date == pd.Timestamp('2025-01-02')
    assert ledger.iloc[0].price == 10 and result['nav'].iloc[0] == pytest.approx(1)
    assert ledger.iloc[0].fee == pytest.approx(ledger.iloc[0].gross * .1)
    assert ledger.iloc[0].cash_after >= 0


def test_default_stop_uses_signal_close_and_sells_next_open():
    b = bars(); b.loc[b.trade_date == pd.Timestamp('2025-01-03'), 'close'] = 8
    result = replay(signals().iloc[:1], b, config=ReplayConfig(max_positions=1, max_hold=99, cost_one_way=0, stop_pct=.1))
    sells = result['ledger'][result['ledger'].side == 'SELL']
    assert len(sells) == 1 and sells.iloc[0].trade_date == pd.Timestamp('2025-01-06')
    assert sells.iloc[0].reason == 'stop_signal_close'


def test_entry_price_stop_is_explicit_variant():
    b = bars(); b.loc[b.trade_date == pd.Timestamp('2025-01-03'), 'close'] = 8
    r = replay(signals().iloc[:1], b, config=ReplayConfig(max_positions=1, max_hold=99, cost_one_way=0, stop_pct=.1, stop_basis='entry_price'))
    assert r['ledger'].query("side == 'SELL'").iloc[0].reason == 'stop_entry_price'


def test_missing_close_keeps_last_mark_and_no_fake_zero():
    b = bars(); b.loc[b.trade_date == pd.Timestamp('2025-01-03'), 'close'] = np.nan
    r = replay(signals().iloc[:1], b, config=ReplayConfig(max_positions=1, max_hold=99, cost_one_way=0))
    assert r['nav'].loc[pd.Timestamp('2025-01-03')] == pytest.approx(r['nav'].loc[pd.Timestamp('2025-01-02')])
    assert r['daily'].loc[pd.Timestamp('2025-01-03'), 'stale_positions'] == 1


def test_missing_exit_open_is_deferred_not_lost():
    b = bars(); b.loc[b.trade_date == pd.Timestamp('2025-01-06'), 'open'] = np.nan
    r = replay(signals().iloc[:1], b, config=ReplayConfig(max_positions=1, max_hold=2, cost_one_way=0))
    sells = r['ledger'].query("side == 'SELL'")
    assert len(sells) == 1 and sells.iloc[0].trade_date == pd.Timestamp('2025-01-07')
    assert (r['events'].event == 'deferred_exit_missing_open').any()


def test_exact_fee_ledger_and_no_cash_overdraft():
    b = bars(); s = signals().iloc[:1]
    r = replay(s, b, config=ReplayConfig(max_positions=1, max_hold=2, cost_one_way=.01))
    buy, sell = r['ledger'].iloc[0], r['ledger'].query("side == 'SELL'").iloc[0]
    assert buy.gross + buy.fee == pytest.approx(1.0)
    assert buy.cash_after >= 0
    assert sell.net_cash == pytest.approx(sell.gross - sell.fee)
    assert sell.return_after_cost == pytest.approx(sell.net_cash / buy.entry_cost - 1)


def test_current_close_perturbation_does_not_change_prior_orders():
    r1 = replay(signals(), bars(), config=ReplayConfig(max_positions=1, max_hold=99, cost_one_way=0))
    b = bars(); b.loc[b.trade_date == pd.Timestamp('2025-01-08'), 'close'] = 999
    r2 = replay(signals(), b, config=ReplayConfig(max_positions=1, max_hold=99, cost_one_way=0))
    cols = ['side', 'code', 'signal_date', 'trade_date', 'price', 'reason']
    pd.testing.assert_frame_equal(r1['ledger'][cols], r2['ledger'][cols])


def test_duplicate_keys_and_metrics():
    with pytest.raises(ValueError): replay(signals(), pd.concat([bars(), bars().iloc[:1]]))
    m = metrics(pd.Series([1, 1.1, 1.0], index=pd.bdate_range('2025-01-01', periods=3)))
    assert m['total_return'] == pytest.approx(0) and m['max_drawdown'] < 0 and m['sharpe'] is not None


def test_invalid_benchmark_is_not_forward_filled():
    with pytest.raises(ValueError): replay(signals(), bars(), benchmark=pd.Series([1.0], index=[pd.Timestamp('2025-01-01')]))


def test_hand_calculated_round_trip_and_cost_reserved_exactly_once():
    b = bars().iloc[:3].copy()
    b.loc[b.index[2], ['open', 'close']] = 12
    r = replay(signals().iloc[:1], b, config=ReplayConfig(max_positions=1, max_hold=1, cost_one_way=.1))
    buy, sell = r['ledger'].iloc[0], r['ledger'].iloc[1]
    assert buy.gross == pytest.approx(1 / 1.1)
    assert buy.shares == pytest.approx(1 / 11)
    assert buy.entry_cost == pytest.approx(1)
    assert r['nav'].iloc[1] == pytest.approx(1 / 1.1)
    assert sell.gross == pytest.approx(12 / 11)
    assert sell.net_cash == pytest.approx(12 / 11 * .9)
    assert sell.realized_pnl == pytest.approx(12 / 11 * .9 - 1)
    assert r['nav'].iloc[-1] == pytest.approx(12 / 11 * .9)


def test_slot_target_is_gross_not_double_fee_discounted():
    b = bars(); b2 = b.assign(stock_code='B')
    s = pd.concat([signals().iloc[:1], signals().iloc[:1].assign(stock_code='B')])
    r = replay(s, pd.concat([b, b2]), config=ReplayConfig(max_positions=2, max_hold=99, cost_one_way=.1))
    buy = r['ledger'].query("side == 'BUY'")
    assert buy.iloc[0].gross == pytest.approx(.5)
    assert buy.iloc[1].gross == pytest.approx(.45 / 1.1)
    assert (r['daily'].cash >= 0).all()
    assert r['daily'].n_positions.max() == 2


def test_opening_allocation_uses_current_open_not_current_close():
    b = bars(); b.loc[b.index[2], 'open'] = 5
    b2 = bars().assign(stock_code='B')
    s = pd.concat([signals().iloc[:1], signals().iloc[:1].assign(stock_code='B', trade_date=b.trade_date.iloc[1])])
    cfg = ReplayConfig(max_positions=2, max_hold=99, stop_pct=.99, cost_one_way=0)
    r1 = replay(s, pd.concat([b, b2]), config=cfg)
    b.loc[b.index[2], 'close'] = 1000
    r2 = replay(s, pd.concat([b, b2]), config=cfg)
    for result in (r1, r2):
        buy_b = result['ledger'].query("side == 'BUY' and code == 'B'").iloc[0]
        assert buy_b.gross == pytest.approx(.375)  # A .05 shares*5 + .5 cash -> .75 / 2
        assert result['daily'].opening_equity.iloc[2] == pytest.approx(.75)
    pd.testing.assert_frame_equal(r1['ledger'], r2['ledger'])


def test_signal_close_stop_differs_from_entry_variant_on_gap():
    b = bars().iloc[:4].copy(); b.loc[b.index[1:], ['open', 'close']] = 8
    cfg = dict(max_positions=1, max_hold=99, stop_pct=.1, cost_one_way=0)
    r1 = replay(signals().iloc[:1], b, config=ReplayConfig(**cfg))
    r2 = replay(signals().iloc[:1], b, config=ReplayConfig(**cfg, stop_basis='entry_price'))
    assert (r1['ledger'].side == 'SELL').sum() == 1
    assert (r2['ledger'].side == 'SELL').sum() == 0


def test_explicit_calendar_does_not_skip_market_session_without_stock_bar():
    b = bars().iloc[:4].copy(); calendar = b.trade_date.tolist()
    b = b.drop(b.index[2])
    r = replay(signals().iloc[:1], b, calendar=calendar, config=ReplayConfig(max_positions=1, max_hold=2, holding_clock='market_sessions', cost_one_way=0))
    assert len(r['daily']) == 4
    assert r['daily'].stale_positions.iloc[2] == 1
    assert r['ledger'].query("side == 'SELL'").trade_date.iloc[0] == calendar[3]
    r2 = replay(signals().iloc[:1], b, calendar=calendar, config=ReplayConfig(max_positions=1, max_hold=2, holding_clock='valid_close', cost_one_way=0))
    assert (r2['ledger'].side == 'SELL').sum() == 0
    assert r2['pending_exits']['A']['decision_date'] == calendar[3]


def test_missing_buy_open_expires_and_is_not_filled_later():
    b = bars(); b.loc[b.index[1], 'open'] = 0
    r = replay(signals().iloc[:1], b)
    assert r['ledger'].empty and (r['nav'] == 1).all()
    assert r['events'].event.tolist() == ['expired_buy_missing_open']


def test_no_forced_liquidation_and_daily_cash_holdings_reconcile():
    r = replay(signals().iloc[:1], bars(), config=ReplayConfig(max_positions=1, max_hold=99, stop_pct=.99))
    assert len(r['open_positions']) == 1 and (r['ledger'].side == 'SELL').sum() == 0
    for date, row in r['daily'].iterrows():
        fills = r['ledger'][r['ledger'].trade_date <= date]
        assert row.cash == pytest.approx(1 + fills.net_cash.sum())
        h = r['holdings'][r['holdings'].trade_date == date]
        assert row.equity == pytest.approx(row.cash + h.market_value.sum())
    assert r['daily'].fees.sum() == pytest.approx(r['ledger'].fee.sum())


def test_permutations_and_tie_policy_are_deterministic():
    b = pd.concat([bars(), bars().assign(stock_code='B')])
    s = pd.concat([signals().iloc[:1], signals().iloc[:1].assign(stock_code='B')])
    cfg = ReplayConfig(max_positions=1, max_hold=99, stop_pct=.99)
    r = replay(s, b, config=cfg)
    shuffled = replay(s.iloc[::-1], b.sample(frac=1, random_state=2), config=cfg)
    assert r['ledger'].iloc[0].code == 'A'
    pd.testing.assert_frame_equal(r['ledger'], shuffled['ledger'])
    pd.testing.assert_series_equal(r['nav'], shuffled['nav'])


@pytest.mark.parametrize('kwargs', [
    {'max_positions':0}, {'max_positions':True}, {'max_positions':1.2},
    {'max_hold':-1}, {'max_hold':False}, {'initial_cash':0}, {'initial_cash':float('nan')},
    {'cost_one_way':-1}, {'cost_one_way':1}, {'cost_one_way':float('inf')},
    {'stop_pct':1}, {'stop_pct':-.1}, {'stop_basis':'bad'}, {'holding_clock':'bad'},
])
def test_invalid_config(kwargs):
    with pytest.raises(ValueError): ReplayConfig(**kwargs)


def test_invalid_signal_and_calendar_rejected():
    s = signals(); s.loc[s.index[0], 'cause_width'] = np.nan
    with pytest.raises(ValueError): replay(s, bars())
    with pytest.raises(ValueError): replay(pd.concat([signals(), signals().iloc[:1]]), bars())
    for calendar in ([], list(bars().trade_date)[::-1], [bars().trade_date.iloc[0]] * 2):
        with pytest.raises(ValueError): replay(signals(), bars(), calendar=calendar)


def test_benchmark_calendar_is_strict_and_metrics_normalise_start():
    dates = bars().trade_date
    bench = pd.Series(np.arange(8) + 100., index=dates)
    r = replay(signals(), bars(), benchmark=bench, calendar=dates)
    assert r['benchmark_nav'].iloc[0] == 1
    assert r['benchmark_nav'].iloc[-1] == pytest.approx(1.07)
    for bad in (bench.drop(bench.index[2]), pd.concat([bench, bench.iloc[:1]]), bench * 0):
        with pytest.raises(ValueError): replay(signals(), bars(), benchmark=bad, calendar=dates)
    m = metrics(pd.Series([2., 3.], index=pd.to_datetime(['2024-01-01', '2025-01-01'])))
    assert m['total_return'] == pytest.approx(.5)
    assert m['calendar_cagr'] == pytest.approx(1.5 ** (365.25 / 366) - 1)
    assert m['sharpe'] is None


def test_drawdown_throttle_scales_new_entries_and_preserves_default_baseline():
    cfg = ReplayConfig(max_positions=1, max_hold=99, cost_one_way=0,
                       drawdown_soft=.01, drawdown_hard=.02, drawdown_floor=.25)
    b = bars().copy()
    # mark a loss before the second signal; no forced liquidation is expected
    b.loc[b.trade_date == pd.Timestamp('2025-01-03'), 'close'] = 8
    s = pd.concat([signals().iloc[:1], signals().iloc[1:]])
    throttled = replay(s, b, config=cfg)
    assert (throttled['daily'].risk_scale <= 1).all()
    assert throttled['daily'].risk_scale.max() == pytest.approx(1.0)
    assert throttled['daily'].risk_scale.iloc[-1] < 1.0
    with pytest.raises(ValueError): ReplayConfig(drawdown_soft=.1, drawdown_floor=.5)


def test_target_weight_signal_controls_gross_entry_size():
    s = signals().iloc[:1].copy()
    s['target_weight'] = .25
    r = replay(s, bars(), config=ReplayConfig(max_positions=1, max_hold=99, cost_one_way=0))
    assert r['ledger'].iloc[0].gross == pytest.approx(.25)
