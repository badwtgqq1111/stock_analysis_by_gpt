import numpy as np
import pandas as pd
import pytest

from backend.services.execution_ledger import (
    ExecutionCost, PortfolioLedger, fee_summary, periodic_rebalances, rebalance_timeline,
)


def test_cash_to_single_and_initial_build_charge():
    cost = ExecutionCost(commission_bps=0, stamp_duty_bps=0, transfer_fee_bps=0,
                         slippage_bps=10, impact_bps=0)
    ledger = PortfolioLedger(100.0, cost)
    ledger.trade('t0', 'A', 'BUY', 100 / 20 / 1.001, 20.0)
    assert ledger.cash == pytest.approx(0.0)
    assert ledger.position('A') == pytest.approx(100 / 20 / 1.001)
    assert ledger.rows[0]['fee'] == pytest.approx(0.1, abs=1e-4)
    assert fee_summary(ledger.rows, 100.0)['one_way_turnover_fraction'] == pytest.approx(.4995, rel=1e-3)
    assert fee_summary(ledger.rows, 100.0)['two_sided_turnover_fraction'] == pytest.approx(.999, rel=1e-3)


def test_partial_reduction_realizes_pnl_and_keeps_average_cost():
    ledger = PortfolioLedger(100.0, ExecutionCost(commission_bps=0, stamp_duty_bps=0,
                                                  transfer_fee_bps=0, slippage_bps=0,
                                                  impact_bps=0))
    ledger.trade('t0', 'A', 'BUY', 10, 10.0)
    sold = ledger.trade('t1', 'A', 'SELL', 4, 12.0)
    assert ledger.position('A') == pytest.approx(6.0)
    assert sold['realized_pnl'] == pytest.approx(8.0)
    assert ledger.positions['A']['avg_cost'] == pytest.approx(10.0)


def test_switch_from_a_to_b_is_cash_neutral_in_notional_and_has_two_sided_cost():
    cost = ExecutionCost(commission_bps=0, stamp_duty_bps=0, transfer_fee_bps=0,
                         slippage_bps=0, impact_bps=0)
    ledger = PortfolioLedger(100.0, cost)
    ledger.trade('t0', 'A', 'BUY', 10, 10.0)
    fills = ledger.rebalance('t1', {'B': 1.0}, {'A': 10.0, 'B': 10.0})
    assert [f['side'] for f in fills] == ['SELL', 'BUY']
    summary = fee_summary(ledger.rows, 100.0)
    # Initial build 100 + switch 200 = 300 gross notional; one-way is half.
    assert summary['one_way_turnover_fraction'] == pytest.approx(1.5)
    assert summary['two_sided_turnover_fraction'] == pytest.approx(3.0)


def test_long_short_transition_uses_absolute_notional_and_borrow_is_separate():
    ledger = PortfolioLedger(100.0, ExecutionCost(commission_bps=3, stamp_duty_bps=5,
                                                  transfer_fee_bps=1, slippage_bps=5,
                                                  impact_bps=3, borrow_bps_annual=50))
    ledger.trade('t0', 'A', 'BUY', 5, 10.0)
    ledger.trade('t1', 'A', 'SELL', 10, 10.0)
    assert ledger.position('A') == pytest.approx(-5.0)
    assert ledger.rows[-1]['fee'] > ledger.rows[0]['fee']
    assert ledger.rows[-1]['fee_bps'] == pytest.approx(
        ledger.rows[-1]['fee'] / ledger.rows[-1]['gross'] * 1e4)


def test_invalid_sell_and_weights_rejected():
    ledger = PortfolioLedger(100.0)
    ledger.trade('t0', 'A', 'SELL', 1, 10.0)
    assert ledger.position('A') == pytest.approx(-1.0)
    with pytest.raises(ValueError):
        ledger.rebalance('t0', {'A': .7, 'B': .7}, {'A': 10.0, 'B': 10.0})
    with pytest.raises(ValueError):
        ledger.rebalance('t0', {'A': -.1}, {'A': 10.0})


def test_explicit_next_open_timeline_and_20_session_schedule():
    dates = pd.bdate_range('2025-01-01', periods=60)
    timeline = rebalance_timeline(dates, dates[0], execution_lag=1)
    assert timeline['signal_date'] == dates[0]
    assert timeline['fill_date'] == dates[1]
    assert timeline['effective_date'] == dates[1]
    assert timeline['first_return_date'] == dates[1]
    schedule = periodic_rebalances(dates, every=20, execution_lag=1)
    assert [x['signal_date'] for x in schedule[:3]] == [dates[0], dates[20], dates[40]]
    assert [x['fill_date'] for x in schedule[:3]] == [dates[1], dates[21], dates[41]]
    with pytest.raises(ValueError):
        rebalance_timeline(dates, dates[-1], execution_lag=1)
