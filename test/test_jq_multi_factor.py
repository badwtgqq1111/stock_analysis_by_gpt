"""Regression checks for the standalone JoinQuant research replay."""
from pathlib import Path
import sys
import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.validate_jq_multi_factor import nw_t, finance_asof, replay, self_test


def test_original_score_and_transaction_guards():
    self_test()


def test_finance_announced_later_never_reaches_earlier_signal():
    grid = pd.DataFrame({'trade_date':pd.to_datetime(['2020-01-02','2020-08-04']), 'stock_code':['A','A']})
    fin = pd.DataFrame({'ts_code':['A','A'],'ann_date':pd.to_datetime(['2019-10-01','2020-08-03']),
                        'end_date':pd.to_datetime(['2019-06-30','2020-06-30']),'roe':[2.,99.]})
    assert finance_asof(grid,fin).roe.tolist()==[2.,99.]


def test_delayed_old_report_does_not_replace_newer_report():
    grid=pd.DataFrame({'trade_date':pd.to_datetime(['2020-08-05']),'stock_code':['A']})
    fin=pd.DataFrame({'ts_code':['A','A'],'ann_date':pd.to_datetime(['2020-08-03','2020-08-04']),
        'end_date':pd.to_datetime(['2020-06-30','2019-12-31']),'roe':[10.,3.]})
    assert finance_asof(grid,fin).roe.iloc[0]==10.


def test_nw_ignores_pandas_index_alignment():
    a=pd.Series(np.arange(12.)/10,index=np.arange(12)*7)
    assert nw_t(a)==nw_t(a.reset_index(drop=True))


def test_missing_first_close_retains_position_at_open_mark():
    dates=pd.date_range('2020-01-01',periods=5)
    bm=pd.DataFrame({'000905.SH':[10.]*5,'000300.SH':[10.]*5},index=dates)
    matrices={k:np.full((5,1),10.) for k in ['open','close','high','low','volume','amount']}
    matrices['close'][1,0]=np.nan
    tiny=pd.DataFrame({'trade_date':[dates[0]],'stock_code':['A']})
    nav,m,_=replay(tiny,pd.Series([1.]),matrices,bm,pd.Index(['A']),top_n=1)
    assert .99<nav.loc[dates[1],'nav']<1 and m['stale_position_marks']==1
