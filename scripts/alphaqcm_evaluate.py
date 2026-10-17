#!/usr/bin/env python3
"""Evaluate frozen AlphaQCM expressions on a held-out panel (separate process)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.services.alphaqcm_data import prepare_vendor_path, stock_data_class
from backend.services.alphagen_expression import parse_expression
from backend.services.factor_evaluation import daily_ic_wide, summarize_ic

prepare_vendor_path()


def _signal(expr, data):
    frame = data.make_dataframe(expr.evaluate(data), columns=['signal'])
    frame.index = frame.index.set_names(['date', 'asset'])
    wide = frame.signal.unstack(level='asset')
    return wide


def run(request_path: Path) -> dict:
    req = json.loads(request_path.read_text())
    panel = pd.read_parquet(req['panel_path'])
    panel['trade_date'] = pd.to_datetime(panel.trade_date)
    out = Path(req['output'])
    out.mkdir(parents=True, exist_ok=True)
    PanelStockData = stock_data_class()
    data = PanelStockData(panel, req['start'], req['end'],
                          max_backtrack_days=int(req.get('max_backtrack_days', 100)),
                          max_future_days=int(req.get('max_future_days', 25)),
                          device=torch.device('cpu'), market='cn')
    factors_doc = json.loads(Path(req['factors_path']).read_text())
    signals, rows = {}, []
    for factor in factors_doc['factors']:
        expr = parse_expression(factor['expression'])
        wide = _signal(expr, data)
        signals[factor['name']] = wide
        rows.append({'name': factor['name'], 'expression': factor['expression'],
                     'weight': factor['weight'], 'coverage': float(wide.notna().to_numpy().mean())})
    if not signals:
        raise ValueError('没有可评测因子')
    close = panel.pivot(index='trade_date', columns='stock_code', values='close')
    if req.get('label', 'paper') == 'paper':
        label = close.shift(-20) / close - 1
    else:
        open_ = panel.pivot(index='trade_date', columns='stock_code', values='open')
        label = open_.shift(-21) / open_.shift(-1) - 1
    # Cross-sectional z-score then weighted ensemble.
    z = {k: v.sub(v.mean(axis=1), axis=0).div(v.std(axis=1).replace(0, np.nan), axis=0)
         for k, v in signals.items()}
    weights = {f['name']: float(f['weight']) for f in factors_doc['factors']}
    # Missing factors must not poison the whole cross-section.  Use a
    # weight-normalised sum over the factors that are present for each asset.
    numerator = sum((z[k].fillna(0.0) * weights[k]) for k in z)
    denominator = sum((z[k].notna().astype(float) * abs(weights[k])) for k in z)
    ensemble = numerator / denominator.replace(0, np.nan)
    metrics = []
    for name, wide in {**z, '_ensemble': ensemble}.items():
        for method in ('pearson', 'spearman'):
            values = daily_ic_wide(wide, label, method=method, min_assets=10)
            metrics.append({'name': name, 'method': method, **summarize_ic(values)})
    pd.DataFrame(rows).to_csv(out / 'expressions.csv', index=False)
    pd.DataFrame(metrics).to_csv(out / 'test_ic.csv', index=False)
    test = pd.DataFrame(metrics)
    ens = test[(test.name == '_ensemble')]
    payload = {
        'status': 'PASS_FROZEN_TEST_EVAL',
        'label': req.get('label', 'paper'),
        'test_start': req['start'], 'test_end': req['end'],
        'factor_count': len(signals),
        'ensemble': {row.method: {k: row[k] for k in ('dates', 'mean', 'icir', 'nw_t', 'ci95')}
                     for _, row in ens.iterrows()},
        'test_visible_to_miner': False,
    }
    (out / 'metrics.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                                 default=str) + '\n')
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', type=Path, required=True)
    args = parser.parse_args()
    run(args.request)


if __name__ == '__main__':
    main()
