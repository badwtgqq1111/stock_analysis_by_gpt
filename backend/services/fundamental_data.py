"""PIT fundamental features for the AlphaQCM research path.

Financial statement fields are joined by **announcement date** (``ann_date``),
never by fiscal period end date.  A trade date can therefore only see a
financial value after the exchange publication date.  Valuation percentiles
come from the already materialized daily industry-relative panel.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
FINANCIAL_PATH = ROOT / 'assets/data/meta/cn_financial_indicators.parquet'
VALUATION_PATH = ROOT / 'assets/data/meta/cn_valuation_features_long.parquet'

FINANCIAL_FIELDS = (
    'roe', 'roa', 'grossprofit_margin', 'netprofit_margin',
    'debt_to_assets', 'netprofit_yoy', 'or_yoy', 'ocfps',
)
VALUATION_FIELDS = ('pe_ind_pct', 'pb_ind_pct', 'size_ind_pct')
NATIVE_FEATURES = (*FINANCIAL_FIELDS, *VALUATION_FIELDS)

NATIVE_FEATURE_METADATA = {
    'roe': {'label': '净资产收益率', 'unit': '%', 'window': '最新已公告报告期',
            'availability': 'ann_date收盘后', 'definition': 'PIT：只使用ann_date<=t的最新公告值'},
    'roa': {'label': '总资产收益率', 'unit': '%', 'window': '最新已公告报告期',
            'availability': 'ann_date收盘后', 'definition': 'PIT：只使用ann_date<=t的最新公告值'},
    'grossprofit_margin': {'label': '毛利率', 'unit': '%', 'window': '最新已公告报告期',
                           'availability': 'ann_date收盘后', 'definition': 'PIT：毛利率'},
    'netprofit_margin': {'label': '净利率', 'unit': '%', 'window': '最新已公告报告期',
                         'availability': 'ann_date收盘后', 'definition': 'PIT：净利率'},
    'debt_to_assets': {'label': '资产负债率', 'unit': '%', 'window': '最新已公告报告期',
                       'availability': 'ann_date收盘后', 'definition': 'PIT：负债/资产'},
    'netprofit_yoy': {'label': '净利润同比', 'unit': '%', 'window': '最新已公告报告期',
                      'availability': 'ann_date收盘后', 'definition': 'PIT：净利润同比增速'},
    'or_yoy': {'label': '营收同比', 'unit': '%', 'window': '最新已公告报告期',
               'availability': 'ann_date收盘后', 'definition': 'PIT：营业收入同比增速'},
    'ocfps': {'label': '每股经营现金流', 'unit': '元/股', 'window': '最新已公告报告期',
              'availability': 'ann_date收盘后', 'definition': 'PIT：每股经营现金流'},
    'pe_ind_pct': {'label': 'PE行业分位', 'unit': '分位[0,1]', 'window': '当日',
                   'availability': 't收盘后', 'definition': '材料化日频行业相对PE分位'},
    'pb_ind_pct': {'label': 'PB行业分位', 'unit': '分位[0,1]', 'window': '当日',
                   'availability': 't收盘后', 'definition': '材料化日频行业相对PB分位'},
    'size_ind_pct': {'label': '规模行业分位', 'unit': '分位[0,1]', 'window': '当日',
                     'availability': 't收盘后', 'definition': '材料化日频行业相对规模分位'},
}


def load_financial_indicators(path: Path | None = None) -> pd.DataFrame:
    frame = pd.read_parquet(path or FINANCIAL_PATH)
    required = {'ts_code', 'ann_date', 'end_date', *FINANCIAL_FIELDS}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f'财务表缺列: {sorted(missing)}')
    out = frame[list(required)].copy()
    out['stock_code'] = out.ts_code.astype(str)
    out['ann_date'] = pd.to_datetime(out.ann_date, errors='coerce').dt.normalize()
    out['end_date'] = pd.to_datetime(out.end_date, errors='coerce').dt.normalize()
    out = out.dropna(subset=['ann_date'])
    out = out.sort_values(['stock_code', 'ann_date', 'end_date']).drop_duplicates(
        ['stock_code', 'ann_date'], keep='last')
    return out[['stock_code', 'ann_date', 'end_date', *FINANCIAL_FIELDS]]


def load_valuation_features(path: Path | None = None) -> pd.DataFrame:
    frame = pd.read_parquet(path or VALUATION_PATH,
                            columns=['trade_date', 'stock_code', *VALUATION_FIELDS])
    frame['trade_date'] = pd.to_datetime(frame.trade_date).dt.normalize()
    frame['stock_code'] = frame.stock_code.astype(str)
    return frame.drop_duplicates(['trade_date', 'stock_code'], keep='last')


def attach_fundamentals(panel: pd.DataFrame, *,
                        financial: pd.DataFrame | None = None,
                        valuation: pd.DataFrame | None = None) -> pd.DataFrame:
    """Attach PIT financial and daily valuation fields to a bar panel."""
    required = {'stock_code', 'trade_date'}
    if not required <= set(panel.columns):
        raise ValueError('面板缺 stock_code/trade_date')
    left = panel.copy()
    left['stock_code'] = left.stock_code.astype(str)
    left['trade_date'] = pd.to_datetime(left.trade_date).dt.normalize()
    left = left.sort_values(['trade_date', 'stock_code'], kind='stable')

    fin = load_financial_indicators() if financial is None else financial.copy()
    fin['stock_code'] = fin.stock_code.astype(str)
    fin['ann_date'] = pd.to_datetime(fin.ann_date, errors='coerce').dt.normalize()
    fin = fin.dropna(subset=['ann_date']).sort_values(['ann_date', 'stock_code'])
    merged = pd.merge_asof(
        left, fin[['stock_code', 'ann_date', 'end_date', *FINANCIAL_FIELDS]],
        left_on='trade_date', right_on='ann_date', by='stock_code',
        direction='backward', allow_exact_matches=True)
    merged['financial_ann_date'] = merged['ann_date']
    merged['financial_end_date'] = merged['end_date']
    merged['financial_age_days'] = (merged.trade_date - merged.financial_ann_date).dt.days
    merged = merged.drop(columns=['ann_date', 'end_date'])

    val = load_valuation_features() if valuation is None else valuation.copy()
    val['stock_code'] = val.stock_code.astype(str)
    val['trade_date'] = pd.to_datetime(val.trade_date).dt.normalize()
    merged = merged.merge(
        val[['trade_date', 'stock_code', *VALUATION_FIELDS]],
        on=['trade_date', 'stock_code'], how='left', validate='many_to_one')
    merged = merged.sort_values(['stock_code', 'trade_date'], kind='stable').reset_index(drop=True)
    return merged
