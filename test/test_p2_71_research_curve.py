from pathlib import Path
import importlib.util
import json

import numpy as np
import pandas as pd
import pytest

from backend.services.factor_replay import metrics
from backend.services.factor_research_curve import read_curve
from backend.services import strategy_registry

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def artifact(tmp_path):
    dates = pd.bdate_range('2024-01-01',periods=270)
    nav = pd.Series(np.linspace(1,1.5,270),index=dates)
    benchmark = pd.Series(np.linspace(100,120,270),index=dates)
    pd.DataFrame({'trade_date':dates,'rule_nav':nav.values,'factor_nav':nav.values,'csi500_nav':benchmark.values/100}).to_csv(tmp_path/'nav.csv',index=False)
    manifest = {'status':'PASS_CAUSAL_REPLAY_PARITY','ledger_daily_holdings_events_nav_parity_exact':True,
                'start':str(dates[0].date()),'end':str(dates[-1].date()),'nav_dates':270,
                'equivalent_factor_metrics':metrics(nav),'factor_versions':[{'factor_id':'x','version':1,'sha256':'abc'}]}
    (tmp_path/'manifest.json').write_text(json.dumps(manifest))
    return tmp_path,{'nav_csv':'nav.csv','manifest':'manifest.json'},benchmark


def test_causal_curve_has_explicit_annualization_and_rule_comparator(artifact):
    root,plan,bm=artifact
    result=read_curve(root,plan,bm,'example')
    assert result['kind']=='factor_causal_replay_net'
    assert result['metrics']['total_return']==pytest.approx(.5)
    assert result['metrics']['annual_return']==pytest.approx(1.5**(252/269)-1)
    assert result['metrics']['calendar_cagr']!=result['metrics']['annual_return']
    assert result['comparators'][0]['nav']==result['nav']
    assert len(result['benchmark_nav'])==270
    assert result['factor_versions'][0]['version']==1
    assert '15交易日' not in result['methodology']


@pytest.mark.parametrize('column,value', [('factor_nav',2),('rule_nav',0),('csi500_nav',np.nan)])
def test_invalid_or_mismatched_curves_are_rejected(artifact,column,value):
    root,plan,bm=artifact
    df=pd.read_csv(root/'nav.csv');df.loc[10,column]=value;df.to_csv(root/'nav.csv',index=False)
    with pytest.raises((ValueError,AssertionError)):read_curve(root,plan,bm,'example')


def test_no_benchmark_forward_fill(artifact):
    root,plan,bm=artifact
    with pytest.raises(ValueError):read_curve(root,plan,bm.drop(bm.index[10]),'example')


def test_tampered_manifest_metrics_are_rejected(artifact):
    root,plan,bm=artifact
    p=root/'manifest.json';m=json.loads(p.read_text());m['equivalent_factor_metrics']['calendar_cagr']=99;p.write_text(json.dumps(m))
    with pytest.raises(ValueError):read_curve(root,plan,bm,'example')


def test_factor_is_registered_research_not_production_or_old_selection():
    spec=strategy_registry.spec('moneyflow_factor_equivalent')
    assert spec['stage']=='research' and spec['kind']=='factor_pipeline'
    assert spec['refresh']['mode']=='skip'
    assert not spec.get('selection_source') and not spec.get('model_arms')
    assert spec['curve']['replay']['format']=='p2_71_causal'
    assert not strategy_registry.validate()
    assert not [r for r in strategy_registry.coverage()['gaps'] if r['strategy']==spec['id']]
    assert not spec.get('history_dir')


def test_performance_builder_wires_causal_reader():
    text=(ROOT/'scripts/build_strategy_pool_performance.py').read_text()
    assert 'p2_71_causal' in text and 'read_curve(ROOT' in text
    front=(ROOT/'frontend/src/components/StrategyPerformance.vue').read_text()
    assert 'calendar_cagr' in front and 'comparators' in front
