"""Predeclared research gates. Never writes strategy configuration or weights."""
from __future__ import annotations
import numpy as np
import pandas as pd

GATES = dict(min_returns=252, min_decisions=63, min_quarters=3,
             max_vol_ratio=1.0, max_return_shortfall=0.02)
PAIRS = {'cn_ensemble_volcap': 'cn_ensemble_equal', 'arm_select_volrisk': 'arm_select_equal'}


def evaluate_gate(candidate: pd.Series, baseline: pd.Series, *, decisions: int,
                  provenance_ok: bool, coverage_ok: bool, protocol_ok: bool = True) -> dict:
    """Input = paired, genuinely captured daily NAV, never historical reconstruction."""
    pair = pd.concat([candidate.rename('candidate'), baseline.rename('baseline')], axis=1).dropna().sort_index()
    returns = pair.pct_change(fill_method=None).dropna()
    quarters = []
    for quarter, frame in returns.groupby(returns.index.to_period('Q')):
        if len(frame) >= 40:
            quarters.append({'quarter': str(quarter), 'observations': len(frame),
                             'risk_improved': bool(frame.candidate.std() <= frame.baseline.std())})
    n = len(returns)
    vol_ratio = float(returns.candidate.std() / returns.baseline.std()) if n > 1 and returns.baseline.std() > 0 else None
    shortfall = float(pair.baseline.iloc[-1] / pair.baseline.iloc[0] - pair.candidate.iloc[-1] / pair.candidate.iloc[0]) if len(pair) else None
    dd = pair / pair.cummax() - 1
    checks = {'frozen_protocol_consistent': bool(protocol_ok), 'prospective_provenance_reviewed': bool(provenance_ok), 'complete_daily_coverage': bool(coverage_ok),
              'returns_252': n >= GATES['min_returns'], 'decisions_63': decisions >= GATES['min_decisions'],
              'stable_quarters': len(quarters) >= GATES['min_quarters'] and sum(q['risk_improved'] for q in quarters) >= np.ceil(len(quarters)*2/3),
              'risk_not_higher': vol_ratio is not None and vol_ratio <= GATES['max_vol_ratio'],
              'return_shortfall_le_2pct': shortfall is not None and shortfall <= GATES['max_return_shortfall'],
              'drawdown_not_worse': bool(len(pair) > 1 and dd.candidate.min() >= dd.baseline.min())}
    return {'status': 'review_eligible' if all(checks.values()) else 'accumulating',
            'observations': n, 'decisions': decisions, 'vol_ratio': vol_ratio,
            'return_shortfall': shortfall, 'quarters': quarters, 'checks': checks,
            'thresholds': GATES, 'automatic_promotion': False}
