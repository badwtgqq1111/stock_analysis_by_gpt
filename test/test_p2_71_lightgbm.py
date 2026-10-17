import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backend.services.factor_lightgbm import FEATURES, features, labels, fit_model, score, gate, freeze_digest

ROOT=Path(__file__).resolve().parents[1]


def panel():
    dates=pd.bdate_range('2024-01-01',periods=70)
    bars=pd.DataFrame({'stock_code':'A','trade_date':dates,'open':np.arange(70)+100.,'close':np.arange(70)+100.,'amount':2e7})
    s=bars.iloc[20:].copy();s['amount_ma']=2e7;s['ret20']=s.close/(s.close-20)-1
    s['moneyflow_net_z_5d']=1.2;s['moneyflow_positive_ratio_10d']=.7;s['rl']=s.close;s['cause_width']=-1.2
    return dates,bars,s


def test_fixed_features_are_causal_and_not_test_standardized():
    dates,bars,s=panel();a=features(s,bars)
    bars.loc[bars.index[-1],'close']=99999
    b=features(s,bars)
    pd.testing.assert_frame_equal(a.iloc[:-1],b.iloc[:-1])
    assert a.iloc[0].price_to_ma20==pytest.approx(120/110.5-1)
    assert a.iloc[0].log_amount20==pytest.approx(np.log(2e7))
    assert list(a[FEATURES].columns)==FEATURES


def test_label_time_line_and_purge_end_dates():
    dates,bars,s=panel();x=features(s,bars)
    result=labels(x,bars,dates,dates[20],dates[40],boundary=dates[45],horizon=10)
    assert result.iloc[0].label_start==dates[21]
    assert result.iloc[0].label_end==dates[31]
    assert result.iloc[0].label==pytest.approx(131/121-1)
    assert result.label_end.max()<dates[45]
    assert result.trade_date.max()==dates[33]


def test_future_price_changes_cannot_change_purged_train_labels():
    dates,bars,s=panel();x=features(s,bars)
    a=labels(x,bars,dates,dates[20],dates[40],boundary=dates[45])
    bars.loc[bars.trade_date>=dates[45],'open']=999999
    b=labels(x,bars,dates,dates[20],dates[40],boundary=dates[45])
    pd.testing.assert_frame_equal(a,b)


def test_missing_label_prices_are_excluded_not_filled():
    dates,bars,s=panel();x=features(s,bars)
    bars.loc[bars.trade_date==dates[21],'open']=np.nan
    r=labels(x,bars,dates,dates[20],dates[40],boundary=dates[45])
    assert dates[20] not in set(r.trade_date)


def test_model_reproducibility_and_score_no_test_label_dependency():
    dates,bars,s=panel();x=features(s,bars)
    train=labels(x,bars,dates,dates[20],dates[31],boundary=dates[43],horizon=2)
    val=labels(x,bars,dates,dates[43],dates[52],boundary=dates[60],horizon=2)
    params={'n_estimators':5,'num_leaves':3,'min_child_samples':2,'n_jobs':1,'random_state':71,'verbosity':-1,'deterministic':True,'force_col_wise':True}
    model=fit_model(train,val,params)
    test=x[x.trade_date>=dates[60]].copy()
    before=score(model,test)
    test['label']=-999999
    after=score(model,test)
    np.testing.assert_array_equal(before.score,after.score)
    model2=fit_model(train,val,params)
    np.testing.assert_array_equal(before.score,score(model2,test).score)
    assert freeze_digest(model.booster_.model_to_string(),{},[])==freeze_digest(model2.booster_.model_to_string(),{},[])
    with pytest.raises(ValueError):fit_model(val,train,params)


@pytest.mark.parametrize('cagr,dd,obs,checks,expected',[(.2,-.2,300,True,True),(.19,-.2,300,True,False),(.2,-.3,300,True,False),(.2,-.2,100,True,False),(.2,-.2,300,False,False)])
def test_non_degradation_is_a_gate_not_guaranteed_training(cagr,dd,obs,checks,expected):
    spec={'cagr_tolerance':0,'max_drawdown_extra':.03,'min_evaluation_returns':252}
    r=gate({'calendar_cagr':cagr,'max_drawdown':dd},[{'calendar_cagr':.15,'max_drawdown':-.2},{'calendar_cagr':.2,'max_drawdown':-.2}],spec,obs,checks)
    assert r['promote'] is expected


def test_protocol_frozen_and_new_training_has_no_legacy_dependencies():
    protocol=json.loads((ROOT/'config/p2_71_lightgbm.json').read_text())
    assert protocol['gate']['cagr_tolerance']==0 and protocol['features']==FEATURES
    assert protocol['parameters']['n_estimators']==100
    for name in ['backend/services/factor_lightgbm.py','scripts/p2_71_lightgbm_study.py']:
        source=(ROOT/name).read_text()
        for forbidden in ['import moneyflow_study','import wyckoff_study','from factor_engine','run_cn_pipeline','import preselect']:
            assert forbidden not in source
    source=(ROOT/'scripts/p2_71_lightgbm_study.py').read_text()
    assert source.index("(output/'frozen.json').write_text")<source.index("test_report = compare")
