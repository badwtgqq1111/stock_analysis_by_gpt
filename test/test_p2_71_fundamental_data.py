import numpy as np
import pandas as pd

from backend.services.fundamental_data import attach_fundamentals
from backend.services.alphaqcm_data import prepare_vendor_path, stock_data_class


def test_financial_asof_uses_announcement_date_not_period_end():
    dates = pd.bdate_range('2024-01-02', periods=5)
    panel = pd.DataFrame({
        'stock_code': ['A'] * 5,
        'trade_date': dates,
        'open': 10.0, 'high': 10.1, 'low': 9.9, 'close': 10.0,
    })
    financial = pd.DataFrame({
        'stock_code': ['A', 'A'],
        'ann_date': pd.to_datetime(['2023-04-30', '2024-01-04']),
        'end_date': pd.to_datetime(['2023-03-31', '2023-12-31']),
        'roe': [10.0, 20.0],
        'roa': [5.0, 6.0],
        'grossprofit_margin': [30.0, 31.0],
        'netprofit_margin': [12.0, 13.0],
        'debt_to_assets': [40.0, 41.0],
        'netprofit_yoy': [5.0, 8.0],
        'or_yoy': [4.0, 7.0],
        'ocfps': [1.0, 1.2],
    })
    valuation = pd.DataFrame({
        'trade_date': dates,
        'stock_code': ['A'] * 5,
        'pe_ind_pct': np.linspace(.1, .5, 5),
        'pb_ind_pct': np.linspace(.2, .6, 5),
        'size_ind_pct': np.linspace(.3, .7, 5),
    })
    out = attach_fundamentals(panel, financial=financial, valuation=valuation)
    assert out.loc[out.trade_date < '2024-01-04', 'roe'].eq(10.0).all()
    assert out.loc[out.trade_date >= '2024-01-04', 'roe'].eq(20.0).all()
    assert out.financial_ann_date.min() == pd.Timestamp('2023-04-30')
    assert out.pe_ind_pct.notna().all()
    # no future announcement is used
    assert (out.financial_ann_date <= out.trade_date).all()


def test_missing_financials_are_preserved_not_zero_filled():
    panel = pd.DataFrame({
        'stock_code': ['B', 'B'],
        'trade_date': pd.to_datetime(['2024-01-02', '2024-01-03']),
        'open': 10.0, 'high': 10.1, 'low': 9.9, 'close': 10.0,
    })
    financial = pd.DataFrame({
        'stock_code': ['A'], 'ann_date': pd.to_datetime(['2024-01-01']),
        'end_date': pd.to_datetime(['2023-12-31']), 'roe': [10.0],
        'roa': [1.0], 'grossprofit_margin': [2.0], 'netprofit_margin': [3.0],
        'debt_to_assets': [4.0], 'netprofit_yoy': [5.0], 'or_yoy': [6.0], 'ocfps': [7.0],
    })
    valuation = pd.DataFrame({
        'trade_date': pd.to_datetime(['2024-01-02', '2024-01-03']),
        'stock_code': ['B', 'B'], 'pe_ind_pct': [np.nan, np.nan],
        'pb_ind_pct': [np.nan, np.nan], 'size_ind_pct': [np.nan, np.nan],
    })
    out = attach_fundamentals(panel, financial=financial, valuation=valuation)
    assert out.roe.isna().all()
    assert out.pe_ind_pct.isna().all()


def test_alphaqcm_tensor_exposes_financial_features_when_present():
    dates = pd.bdate_range('2024-01-01', periods=300)
    rows = []
    for code in ('A', 'B'):
        for i, d in enumerate(dates):
            rows.append({'trade_date': d, 'stock_code': code, 'open': 10.0,
                         'high': 10.1, 'low': 9.9, 'close': 10.0 + i * .001,
                         'volume': 1000.0, 'vwap': 10.0, 'roe': 10.0,
                         'roa': 5.0, 'grossprofit_margin': 30.0,
                         'netprofit_margin': 12.0, 'debt_to_assets': 40.0,
                         'netprofit_yoy': 8.0, 'or_yoy': 7.0, 'ocfps': 1.0,
                         'pe_ind_pct': .5, 'pb_ind_pct': .4, 'size_ind_pct': .3})
    panel = pd.DataFrame(rows)
    prepare_vendor_path()
    PanelStockData = stock_data_class()
    data = PanelStockData(panel, '2024-06-03', '2024-12-31',
                          max_backtrack_days=100, max_future_days=25)
    names = [f.name.lower() for f in data._features]
    assert 'roe' in names and 'pe_ind_pct' in names
    assert data.data.shape[1] == len(names)
