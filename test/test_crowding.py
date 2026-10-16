"""Causal crowding definitions, execution isolation and archive contracts."""
import numpy as np
import pandas as pd
import pytest
from scipy.stats import skew, kurtosis
from factor_engine.risk.crowding import features, historical_percentile, breadth, soft_weights
from scripts.crowding_study import holm, block_stats


def bars(n=420):
    rng=np.random.default_rng(70);c=10*np.exp(np.cumsum(rng.normal(0,.01,n)));v=rng.uniform(1e5,1e6,n)
    return pd.DataFrame({'stock_code':'600000.SH','trade_date':pd.bdate_range('2023-01-02',periods=n),
         'open':c,'high':c*1.01,'low':c*.99,'close':c,'volume':v,'amount':v*c,'turnover':v/1e8,'source':'verified'})


def calc(f):return features(f,pd.DatetimeIndex(f.trade_date),['verified'])


def test_rank_excludes_current_and_ties_are_midrank():
    x=pd.Series([1.,2.,3.,2.]);assert historical_percentile(x,3).iloc[-1]==.5
    assert historical_percentile(pd.Series([1.,1.,1.,1.]),3).iloc[-1]==.5
    assert historical_percentile(pd.Series([1.,2.,3.,100.]),3).iloc[-1]==1
    assert historical_percentile(pd.Series([1.,np.nan,3.,100.]),3).isna().all()


def test_bias_correlation_sample_std_and_moments():
    raw=bars();f=calc(raw);r=raw.close.pct_change().tail(60)
    assert f.volume_bias20.iloc[-1]==pytest.approx(raw.volume.iloc[-1]/raw.volume.tail(20).mean()-1)
    assert f.vol20.iloc[-1]==pytest.approx(r.tail(20).std(ddof=1)*np.sqrt(252))
    assert f.return_skew60.iloc[-1]==pytest.approx(skew(r,bias=True))
    assert f.return_kurt60.iloc[-1]==pytest.approx(kurtosis(r,bias=True,fisher=False))
    assert f.price_amount_corr40.iloc[-1]==pytest.approx(raw.close.tail(40).corr(raw.amount.tail(40)))


def test_future_mutation_and_truncation_prefix_invariant():
    a=bars();b=a.copy();b.loc[350:,['open','high','low','close','volume','amount','turnover']]*=2
    pd.testing.assert_frame_equal(calc(a).iloc[:350],calc(b).iloc[:350])
    pd.testing.assert_frame_equal(calc(a).iloc[:350].reset_index(drop=True),calc(a.iloc[:350]).reset_index(drop=True))


def test_invalid_bar_and_missing_session_interrupt_warmups():
    a=bars();cal=pd.DatetimeIndex(a.trade_date);a=a.drop(300)
    f=features(a,cal,['verified']);assert not f.valid_bar.iloc[300]
    assert f.turnover_pct252.iloc[300:420].isna().all()
    assert f.vol20.iloc[300:321].isna().all()


def test_unknown_source_zero_turnover_and_invalid_prices_are_not_ready():
    for col,val in [('source','baostock'),('turnover',0.),('amount',0.),('close',np.nan)]:
        a=bars();a.loc[300,col]=val;f=calc(a)
        assert pd.isna(f.turnover_pct252.iloc[300])
        assert f.data_status.iloc[300]!='ready'


def test_units_scaling_no_effect_on_scale_free_proxies():
    a=bars();b=a.copy();b.volume*=100;b.amount*=100;b.turnover*=100
    cols=['turnover_pct252','volume_bias20','amount_bias20','price_amount_corr40']
    np.testing.assert_allclose(calc(a)[cols],calc(b)[cols],equal_nan=True,atol=1e-10)


def test_constant_and_zero_denominators_have_no_fake_corr_or_moments():
    a=bars();a[['open','high','low','close']]=10.;a[['volume','amount']]=1e6;a.turnover=.01
    f=calc(a)
    assert f.price_amount_corr40.isna().all() and f.return_kurt60.isna().all()
    assert f.ret_dlogamount_corr40.isna().all()
    assert f.turnover_pct252.iloc[-1]==.5
    assert not f.high_crowding.iloc[-1]


def test_duplicates_calendar_and_multiple_symbols_rejected():
    a=bars()
    with pytest.raises(ValueError):calc(pd.concat([a,a.iloc[[1]]]))
    with pytest.raises(ValueError):features(a,pd.DatetimeIndex(a.trade_date[::-1]),['verified'])
    a.loc[2,'stock_code']='600001.SH'
    with pytest.raises(ValueError):calc(a)


def test_breadth_denominator_and_new_extrema_only_prior_window():
    a=bars(300);a[['open','high','low','close']]=np.repeat(np.arange(1,301)[:,None],4,axis=1)
    f=calc(a);assert not f.breadth_eligible.iloc[251];assert f.new_high252.iloc[252]==1
    b=breadth(f);assert (b.eligible==1).all();assert (b.hindenburg_high==1).all();assert (b.hindenburg_sqrt==0).all()


def test_soft_cap_keeps_cash_and_unknown_exposure():
    w=pd.Series({'a':.4,'b':.3,'c':.3});r=soft_weights(w,{'a':True,'b':False,'c':pd.NA})
    assert r.to_dict()=={'a':.2,'b':.3,'c':.3};assert r.sum()==.8


def test_holm_and_block_ci_reproducible_and_short_samples():
    np.testing.assert_allclose(holm([.01,.04,.03,np.nan]),[.03,.06,.06,np.nan],equal_nan=True)
    cal=pd.bdate_range('2025-01-01',periods=100);s=pd.Series(np.sin(np.arange(100))*.01,index=cal)
    assert block_stats(s,cal,25)==block_stats(s,cal,25)
    assert np.isnan(block_stats(s.iloc[:10],cal,25)['p_value'])


def test_shadow_api_contract():
    from scripts.run_crowding_shadow import variant_weights
    w=pd.Series({'600000.SH':.5,'600001.SH':.5});r=variant_weights(w,{'600000.SH':True,'600001.SH':False})
    pd.testing.assert_series_equal(r['A'],r['B']);assert r['C'].sum()==.75


def test_forward_contract_rejects_historical_and_frozen_input_mutation(tmp_path):
    from scripts.capture_crowding_forward import validate_payload
    assert not validate_payload({'captured_at':'2026-10-16T16:00:00+08:00','decision_date':'2026-10-15','as_of':'2026-10-15','inputs_ready':True})
    assert validate_payload({'captured_at':'2026-10-16T16:00:00+08:00','decision_date':'2026-10-16','as_of':'2026-10-16','inputs_ready':True})


@pytest.mark.parametrize('patch',[
    {'captured_at':'2026-10-16T14:59:59+08:00'},
    {'captured_at':'2026-10-16T16:00:00'},
    {'as_of':'2026-10-15'}, {'inputs_ready':False}, {'captured_at':'bad'}])
def test_forward_rejects_preclose_naive_stale_and_invalid(patch):
    from scripts.capture_crowding_forward import validate_payload
    p={'captured_at':'2026-10-16T16:00:00+08:00','decision_date':'2026-10-16','as_of':'2026-10-16','inputs_ready':True}
    assert not validate_payload({**p,**patch})


def test_forward_utc_is_converted_and_protocol_dates_deduplicate(tmp_path):
    from scripts.capture_crowding_forward import validate_payload,evaluate_archive,write_capture,read_captures
    s={'source':'test','captured_at':'2026-10-16T08:00:00+00:00','decision_date':'2026-10-16','as_of':'2026-10-16','inputs_ready':True}
    assert validate_payload(s)
    p={'captured_at':s.pop('captured_at'),'protocol_id':'v1','signals':[s]}
    write_capture(tmp_path,p);write_capture(tmp_path,p)
    write_capture(tmp_path,{**p,'protocol_id':'other'})
    r=evaluate_archive(read_captures(tmp_path),'v1')
    assert r['eligible_decisions']=={'test':1} and r['foreign_protocol_archives']==1
    assert r['automatic_promotion'] is False and r['forward_nav_available'] is False


def test_forward_detects_modified_archive(tmp_path):
    import json
    from scripts.capture_crowding_forward import write_capture,read_captures
    p=write_capture(tmp_path,{'captured_at':'2026-10-16T16:00:00+08:00','inputs_ready':False})
    e=json.loads(p.read_text());e['payload']['inputs_ready']=True;p.write_text(json.dumps(e))
    with pytest.raises(ValueError,match='hash mismatch'):read_captures(tmp_path)


def test_completed_months_exclude_partial_tail():
    from scripts.crowding_study import completed_month_ends
    cal=pd.bdate_range('2026-08-01','2026-10-15')
    assert completed_month_ends(cal)=={pd.Timestamp('2026-08-31'),pd.Timestamp('2026-09-30')}
    assert completed_month_ends([])==set()


def test_same_day_unit_identities_and_snapshot_change_not_forwardfill():
    from scripts.audit_crowding_inputs import audit_matches,share_changes
    b=pd.DataFrame({'trade_date':pd.to_datetime(['2026-09-30']),'stock_code':['600000.SH'],
                    'close':[10.],'volume':[1000000.],'amount':[10000000.],'turnover':[.01]})
    d=pd.DataFrame({'trade_date':b.trade_date,'ts_code':['600000.SH'],'close':[10.],
                    'float_share':[10000.],'free_share':[5000.],'total_share':[20000.],
                    'turnover_rate':[1.],'turnover_rate_f':[2.],'total_mv':[200000.],'circ_mv':[100000.]})
    m=audit_matches(b,d)
    for c in ['volume_float_turnover','volume_free_turnover','bar_turnover_float','total_mv_identity','circ_mv_identity']:
        assert m[c].iloc[0]==pytest.approx(1)
    d2=d.copy();d2.trade_date=pd.to_datetime(['2026-10-30']);d2.float_share*=2
    changes=share_changes(pd.concat([d,d2]))
    assert len(changes)==1 and changes.float_share_ratio.iloc[0]==2
    assert changes.previous_observed_date.iloc[0]==pd.Timestamp('2026-09-30')


def test_gate_never_promotes_historical_reconstruction():
    from scripts.run_crowding_shadow import gate
    ix=pd.bdate_range('2025-01-01',periods=300);b=pd.Series(np.exp(np.arange(300)*.001),index=ix)
    r=gate(b,b,100,False)
    assert r['status']=='not_eligible' and not r['automatic_promotion']
    assert not r['checks']['independent_pit_review'] and not r['checks']['prospective_not_reconstruction']


def test_frozen_protocol_initialization_never_overwrites(tmp_path):
    from scripts.crowding_study import ensure_methodology
    p=ensure_methodology(tmp_path);assert 'crowding.v1' in p.read_text()
    p.write_text('{"custom_protocol":true}')
    assert ensure_methodology(tmp_path).read_text()=='{"custom_protocol":true}'
