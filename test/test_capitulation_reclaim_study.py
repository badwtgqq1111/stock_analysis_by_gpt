"""Frozen retrospective research rule: causal, valid inputs, T+1 and censor checks."""
import numpy as np
import pandas as pd
import pytest
from scripts.capitulation_reclaim_study import features, dedupe_positions, risk_on, summarize
from scripts.breakout_volatility_study import execute_event


def fixture():
    n=160;c=np.full(n,100.);c[100:125]=80.;c[125]=74.;c[126]=82.;c[127:]=105.
    f=pd.DataFrame({'trade_date':pd.bdate_range('2025-01-01',periods=n),'stock_code':'600000.SH',
          'open':c,'high':c+1,'low':c-1,'close':c,'volume':1e6,'amount':1e8})
    f.loc[125,['open','high','low','close','volume']]=[73,75,70,74,3e6]
    return f


def test_synthetic_state_machine():
    f=features(fixture())
    assert f.panic.iloc[125]
    assert f.early_reclaim.iloc[126]
    assert f.panic_confirm.iloc[127]
    assert f.panic_confirm_setup.iloc[127]==f.trade_date.iloc[125]


def test_future_perturbation_and_truncation_invariant():
    a=fixture();b=a.copy();b.loc[140:,['open','high','low','close','volume']]*=3
    cols=['panic','early_reclaim','panic_confirm','no_volume_confirm','ma20','vr20']
    pd.testing.assert_frame_equal(features(a)[cols].iloc[:140],features(b)[cols].iloc[:140])
    pd.testing.assert_frame_equal(features(a)[cols].iloc[:130],features(a.iloc[:130])[cols])


def test_break_of_panic_low_invalidates_pending_setup():
    a=fixture();a.loc[126,['open','high','low','close']]=[70,72,69,71]
    f=features(a)
    assert not f.panic_confirm.iloc[126:146].any()


def test_zero_volume_or_missing_price_never_signals():
    for column,value in [('volume',0),('close',np.nan),('amount',0)]:
        a=fixture();a.loc[125,column]=value;f=features(a)
        assert not f.panic.iloc[125]
        assert not f.panic_confirm.iloc[126:146].any()


def test_duplicate_dates_and_multiple_codes_rejected():
    a=fixture()
    with pytest.raises(ValueError):features(pd.concat([a,a.iloc[[1]]]))
    a.loc[1,'stock_code']='600001.SH'
    with pytest.raises(ValueError):features(a)


def test_no_volume_ablation_not_volume_optimized():
    a=fixture();a.loc[125,'volume']=1e6;f=features(a)
    assert not f.panic_confirm.any()
    assert f.no_volume_confirm.iloc[127]


def test_market_session_dedupe():
    dates=pd.Series(pd.bdate_range('2025-01-01',periods=100));mask=np.ones(100,dtype=bool)
    assert dedupe_positions(mask,dates,pd.DatetimeIndex(dates))==[0,40,80]


def test_risk_names_use_announcement_and_effective_dates():
    names=pd.DataFrame({'start_date':pd.to_datetime(['2025-01-01','2025-06-01']),
                        'end_date':pd.to_datetime(['2025-05-31',None]),
                        'ann_date':pd.to_datetime(['2025-01-01','2025-06-03']),'is_risk':[False,True]})
    assert risk_on(names,'2025-05-01') is False
    assert risk_on(names,'2025-06-02') is None
    assert risk_on(names,'2025-06-03') is True
    assert risk_on(names.iloc[:0],'2025-01-01') is None


def test_next_open_execution_and_tplus1_exit():
    dates=pd.bdate_range('2025-01-01',periods=6)
    d=pd.DataFrame({'open':[10,10.5,11,11,11,11],'close':[10,10.6,11,11,11,11],'volume':1e6},index=dates)
    r=execute_event('600000.SH',dates[0],2,d,dates)
    assert r['entry_date']==str(dates[1].date()) and r['exit_date']==str(dates[2].date())
    assert 0<r['net_return']<11/10.5-1
    with pytest.raises(ValueError):execute_event('600000.SH',dates[0],1,d,dates)


def test_limit_entry_and_delayed_exit_and_tail_censor():
    dates=pd.bdate_range('2025-01-01',periods=5)
    d=pd.DataFrame({'open':[10,11,10,10,10],'close':[10,10,9,9.5,9.5],'volume':1e6},index=dates)
    assert execute_event('600000.SH',dates[0],2,d,dates)['status']=='skip_limit_up'
    d.loc[dates[1],'open']=10
    r=execute_event('600000.SH',dates[0],2,d,dates)
    assert r['exit_delay']==1 and r['exit_date']==str(dates[3].date())
    assert execute_event('600000.SH',dates[0],10,d,dates)['status']=='censored_exit'
    assert execute_event('600000.SH',dates[0],2,d.drop(dates[1]),dates)['status']=='skip_missing_entry'


def test_confidence_summary_reproducible_and_counts_censors():
    dates=pd.bdate_range('2025-01-01',periods=80)
    e=pd.DataFrame({'trade_date':dates,'strategy':'panic_confirm','horizon':20,'period':'2025',
         'status':['closed']*79+['censored_exit'],'net_return':[.01]*79+[np.nan],
         'index_reference_excess':0.})
    a=summarize(e,dates);b=summarize(e,dates)
    pd.testing.assert_frame_equal(a,b)
    assert a.closed.iloc[0]==79 and a.signals.iloc[0]==80
    assert a.block_ci_low.iloc[0]==pytest.approx(.01)
