"""Pinned-version moneyflow signal path. No model, preselect, PK or legacy risk imports."""
from __future__ import annotations
import operator
import pandas as pd

from backend.services.factor_dsl import evaluate, panel
from backend.services.factor_pool import FactorStore, families

OPS = {'>': operator.gt, '>=': operator.ge, '<': operator.lt, '<=': operator.le}


def equivalent_spec():
    return next(v for f in families() for v in f['variants'] if v['id'] == 'moneyflow_factor_equivalent')


def moneyflow_panel(raw: pd.DataFrame) -> pd.DataFrame:
    """Sparse provider rows: use the explicit generic provider, never first-row dedup.

    Other sources must not contain generic columns; fail on definition drift.
    Frames without source are already normalized inputs (e.g. synthetic tests).
    """
    fields = ['moneyflow_net_z_5d', 'moneyflow_positive_ratio_10d']
    if 'source' in raw:
        selected = raw['source'].eq('moneyflow')
        if raw.loc[~selected, fields].notna().any().any():
            raise ValueError('非moneyflow来源包含通用字段，请审计数据契约')
        raw = raw.loc[selected].copy()
    return panel(raw)


def build_signals(bars: pd.DataFrame, moneyflow: pd.DataFrame, store: FactorStore,
                  universe: set[str], spec: dict | None = None) -> pd.DataFrame:
    """Returns full eligible set + deterministic daily rank, not orders/realized returns.

    universe is explicit input (caller must audit PIT membership). Empty set means
    zero assets, never accidental all-market. At least 21 prior observations required.
    """
    spec = spec or equivalent_spec()
    left, right = panel(bars), moneyflow_panel(moneyflow)
    left = left[left.stock_code.isin(universe)].reset_index(drop=True)
    fields = ['stock_code', 'trade_date', 'moneyflow_net_z_5d', 'moneyflow_positive_ratio_10d']
    frame = panel(left.merge(right[fields], on=['stock_code', 'trade_date'], how='left', validate='one_to_one'))
    values = {}
    refs = {r['factor_id']: r['version'] for r in spec['factor_refs']}
    for fid, ver in refs.items():
        factor = store.get(fid, ver)
        if factor['semantics']['result_type'] != 'number':
            raise ValueError('该策略需要数值因子')
        values[fid] = evaluate(factor['formula'], frame)
    eligible = pd.Series(True, index=frame.index)
    for gate in spec['gates']:
        a = values[gate['factor_id']]
        b = values[gate['other_factor_id']] if 'other_factor_id' in gate else gate['value']
        eligible &= OPS[gate['op']](a, b) & a.notna()
        if isinstance(b, pd.Series):
            eligible &= b.notna()
    frame['score'] = values[spec['rank']['factor_id']]
    frame['ret20'] = values['mf_return20']
    frame['amount_ma'] = values['mf_amount20']
    out = frame[eligible].copy().sort_values(
        ['trade_date', 'score', 'stock_code'], ascending=[True, spec['rank']['ascending'], True], kind='stable')
    out['rank'] = out.groupby('trade_date').cumcount() + 1
    out['cause_width'] = -out['score']
    out['rh'], out['rl'] = out['close'] * 1.5, out['close']
    return out.reset_index(drop=True)
