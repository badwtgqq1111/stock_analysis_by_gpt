"""P2/P3 timing, archival, API and promotion regression tests."""
import importlib.util
import json
from pathlib import Path
import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


def script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / f'{name}.py')
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


study = script('breakout_volatility_study')
forward = script('capture_volatility_forward')


def bars(n=130):
    rng = np.random.default_rng(7)
    close = 10*np.exp(np.cumsum(rng.normal(0,.005,n)))
    return pd.DataFrame(dict(trade_date=pd.bdate_range('2025-01-01',periods=n),stock_code='600000.SH',
                             open=close,high=close*1.01,low=close*.99,close=close,volume=1e7,amount=1e8))


def test_features_prefix_invariant_and_contraction_excludes_current_bar():
    f = bars(); a = study.signal_features(f)
    mutated = f.copy(); mutated.loc[100:,['open','high','low','close','volume']] *= 1.5
    b = study.signal_features(mutated)
    pd.testing.assert_frame_equal(a.iloc[:100],b.iloc[:100])
    assert a.contraction_ratio.iloc[100] == b.contraction_ratio.iloc[100]


def test_future_bars_do_not_revise_box_retest_signal():
    f = bars(); f[['open','close']] = 10.; f['high']=10.05; f['low']=9.95
    f.loc[90,['open','close','high']]=11.; f.loc[91,['open','close','high']]=10.02
    a = study.signal_features(f)
    assert a.box.iloc[91] and not a.box.iloc[90]
    b = study.signal_features(f.iloc[:92])
    pd.testing.assert_frame_equal(a.iloc[:92],b)


def test_donchian_and_wyckoff_positive_signal_fixtures():
    f = bars(); f[['open','close']]=10.; f['high']=10.05; f['low']=9.95
    f.loc[100,['open','close','high']]=10.2; f.loc[100,'volume']=2e7
    assert study.signal_features(f).donchian.iloc[100]
    f.loc[80,'low']=9.8
    assert study.signal_features(f).wyckoff.iloc[100]


def test_execution_next_open_and_exact_cost_cash_residual():
    f = bars(10); f[['open','close']]=10.; d=f.trade_date.iloc[0]
    result = study.execute_event('600000.SH',d,5,f.set_index('trade_date'),pd.DatetimeIndex(f.trade_date))
    assert result['entry_date'] == str(f.trade_date.iloc[1].date())
    assert result['exit_date'] == str(f.trade_date.iloc[5].date())
    assert result['quantity']==900 and result['status']=='closed'
    buy=900*10*1.0003; sell=900*10*.9997
    expected=(10000-buy-buy*.00025+sell-sell*.00125)/10000-1
    assert result['net_return']==pytest.approx(expected)


@pytest.mark.parametrize('kind',['missing','limit_up','zero_volume','too_expensive'])
def test_entry_failures_kept(kind):
    f=bars(10); f[['open','close']]=10.; cal=pd.DatetimeIndex(f.trade_date)
    if kind=='missing': f=f.drop(1)
    if kind=='limit_up': f.loc[1,'open']=11.
    if kind=='zero_volume': f.loc[1,'volume']=0
    if kind=='too_expensive': f[['open','close']]=1000.
    r=study.execute_event('600000.SH',cal[0],5,f.set_index('trade_date'),cal)
    assert r['status'].startswith('skip_') and r['net_return'] is None


def test_exit_limit_down_deferred_and_truncated_censored():
    f=bars(10); f[['open','close']]=10.; f.loc[5,'close']=9.; f.loc[6,'close']=9.1
    cal=pd.DatetimeIndex(f.trade_date)
    r=study.execute_event('600000.SH',cal[0],5,f.set_index('trade_date'),cal)
    assert r['status']=='closed' and r['exit_delay']==1
    r=study.execute_event('600000.SH',cal[0],20,f.set_index('trade_date'),cal)
    assert r['status']=='censored_exit' and r['net_return'] is None
    with pytest.raises(ValueError): study.execute_event('600000.SH',cal[0],1,f.set_index('trade_date'),cal)


def test_capture_historical_not_forward_and_same_day_postclose_only():
    assert not forward.eligible_capture('2026-10-15','2026-10-16T18:00:00+08:00')
    assert not forward.eligible_capture('2026-10-16','2026-10-16T14:59:59+08:00')
    assert forward.eligible_capture('2026-10-16','2026-10-16T15:01:00+08:00')
    with pytest.raises(ValueError): forward.eligible_capture('2026-10-16','2026-10-16T18:00:00')


def test_capture_immutable_hash_detects_mutation(tmp_path):
    payload={'captured_at':'2026-10-16T18:00:00+08:00','signals':[]}
    a=forward.write_capture(tmp_path,payload); saved=a.read_bytes()
    b=forward.write_capture(tmp_path,payload)
    assert a!=b and a.read_bytes()==saved and len(forward.read_captures(tmp_path))==2
    obj=json.loads(a.read_text()); obj['payload']['signals']=[1]; a.write_text(json.dumps(obj))
    with pytest.raises(ValueError,match='hash mismatch'): forward.read_captures(tmp_path)


def test_gate_cannot_promote_reconstructed_or_short_samples():
    from factor_engine.portfolio.volatility_promotion import evaluate_gate
    idx=pd.bdate_range('2025-01-01',periods=270)
    b=pd.Series(np.cumprod(1+np.tile([.01,-.005],135)),index=idx)
    c=pd.Series(np.cumprod(1+np.tile([.006,-.001],135)),index=idx)
    g=evaluate_gate(c,b,decisions=90,provenance_ok=False,coverage_ok=True)
    assert g['status']=='accumulating' and not g['automatic_promotion']
    assert not g['checks']['prospective_provenance_reviewed']
    g=evaluate_gate(c.head(10),b.head(10),decisions=1,provenance_ok=True,coverage_ok=True)
    assert g['observations']==9 and not g['checks']['returns_252']


def test_archive_replays_only_frozen_weights_and_prices(tmp_path):
    cal=pd.bdate_range('2026-01-05',periods=4)
    captures=[]
    for i, day in enumerate(cal):
        captures.append({'protocol_id':forward.protocol_id(), 'execution_assumptions':forward.load_assumptions(), 'captured_at':day.isoformat()+'+08:00','observed_date':day.strftime('%Y-%m-%d'),
          'prices_eligible':True,'prices':[{'stock_code':'600000.SH','trade_date':day.strftime('%Y-%m-%d'),'open':10.,'close':10.+i*.1}],
          'signals':[{'variant':v,'trade_date':cal[0].strftime('%Y-%m-%d'),'eligible':i==0,
                      'weights':[{'stock_code':'600000.SH','target_weight':w}]} for v,w in [('cn_ensemble_equal',1.),('cn_ensemble_volcap',.5)]]})
    r=forward.evaluate_archive(captures,cal,tmp_path)
    assert r['gates']['cn_ensemble_volcap']['checks']['frozen_protocol_consistent']
    assert r['eligible_decisions']==1 and r['gates']['cn_ensemble_volcap']['observations']==3
    assert r['gates']['cn_ensemble_volcap']['checks']['complete_daily_coverage']
    assert not r['gates']['cn_ensemble_volcap']['checks']['prospective_provenance_reviewed']
    r=forward.evaluate_archive([captures[0],captures[2],captures[3]],cal,tmp_path)
    assert not r['gates']['cn_ensemble_volcap']['checks']['complete_daily_coverage']


def risk_fixture(tmp_path):
    pd.DataFrame([{'trade_date':d,'variant':'cn_ensemble_volcap','status':'ready','error':'','exposure':.5,'predicted_annual_vol':.15} for d in ['2026-01-05','2026-01-08']]).to_csv(tmp_path/'risk_audit.csv',index=False)
    pd.DataFrame([dict(trade_date=d,variant='cn_ensemble_volcap',stock_code='600000.SH',weight=.5,marginal_vol=.3,component_vol=.15,portfolio_vol=.15) for d in ['2026-01-05','2026-01-08']]).to_csv(tmp_path/'risk_contributions.csv',index=False)
    p=tmp_path/'common/cn_ensemble_volcap';p.mkdir(parents=True)
    pd.DataFrame(dict(asof_date=pd.bdate_range('2026-01-05',periods=5),nav=[100.,101.,102.,103.,90.])).to_csv(p/'nav.csv',index=False)


def test_api_asof_missing_invalid_and_no_research_refresh(tmp_path,monkeypatch):
    from backend.services import volatility_risk
    from backend.api.strategies import router
    monkeypatch.setattr(volatility_risk,'OUT',tmp_path)
    app=FastAPI();app.include_router(router);client=TestClient(app)
    assert client.get('/api/strategies/volatility-risk').json()['available'] is False
    risk_fixture(tmp_path)
    r=client.get('/api/strategies/volatility-risk?date=2026-01-05')
    assert r.status_code==200 and r.json()['correlation']['observations']==0
    latest=client.get('/api/strategies/volatility-risk').json()
    assert latest['date']=='2026-01-08' and latest['correlation']['observations']==3
    assert latest['contributions'][0]['risk_share']==1.
    assert client.get('/api/strategies/volatility-risk?date=2099-01-01').status_code==422
    assert client.get('/api/strategies/volatility-risk?variant=../x').status_code==422
    (tmp_path/'risk_audit.csv').write_text('corrupt')
    assert client.get('/api/strategies/volatility-risk').status_code==503


def test_gate_good_long_sample_can_only_request_review():
    from factor_engine.portfolio.volatility_promotion import evaluate_gate
    idx=pd.bdate_range('2025-01-01',periods=270)
    b=pd.Series(np.cumprod(1+np.tile([.01,-.008],135)),index=idx)
    c=pd.Series(np.cumprod(1+np.tile([.006,-.003],135)),index=idx)
    g=evaluate_gate(c,b,decisions=90,provenance_ok=True,coverage_ok=True)
    assert all(g['checks'].values()) and g['status']=='review_eligible'
    assert g['automatic_promotion'] is False
    g=evaluate_gate(c,b,decisions=90,provenance_ok=True,coverage_ok=True,protocol_ok=False)
    assert g['status']=='accumulating' and not g['checks']['frozen_protocol_consistent']


def test_daily_forward_step_is_guarded_bounded_after_ledger():
    s=(ROOT/'scripts/run_daily_production.sh').read_text()
    assert s.index('scripts/build_selection_ledger.py') < s.index('scripts/capture_volatility_forward.py')
    assert 'DAILY_VOLATILITY_FORWARD:-1' in s and 'timeout=300' in s
    step=s[s.index('# 波动率独立研究留档：'):s.index('# ── PK 护栏日报：')]
    assert 'DRY_RUN' in step and 'FAILED' in step and 'else' in step


def test_reconcile_only_adds_skip_and_preserves_prior_rows(tmp_path,monkeypatch):
    import sys
    refresh=script('refresh_strategy_lists')
    old={'id':'old','status':'already_fresh','target_date':'2026-01-05'}
    (tmp_path/'status.json').write_text(json.dumps({'rows':[old]}))
    specs=[{'id':'old'},{'id':'new','daily_refresh':{'mode':'skip'}}]
    monkeypatch.setattr(refresh,'OUT',tmp_path)
    monkeypatch.setattr(refresh,'load_strategies',lambda:specs)
    monkeypatch.setattr(refresh,'latest_trade_date',lambda:'2026-01-06')
    monkeypatch.setattr(refresh,'to_md',lambda x:'test status\n')
    seen=[]
    def refresh_one(spec,*args,**kwargs):
        seen.append(spec['id']);return {'id':spec['id'],'status':'skipped'}
    monkeypatch.setattr(refresh,'refresh_one',refresh_one)
    monkeypatch.setattr(sys,'argv',['refresh','--reconcile'])
    assert refresh.main()==0 and seen==['new']
    assert json.loads((tmp_path/'status.json').read_text())['rows'][0]==old
    specs.append({'id':'not_run','daily_refresh':{'mode':'run'}})
    with pytest.raises(SystemExit, match='需要实际刷新'): refresh.main()
