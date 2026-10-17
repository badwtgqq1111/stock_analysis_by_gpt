"""Read the P2.71 paired causal replay without legacy performance semantics."""
from pathlib import Path
import hashlib
import json

import numpy as np
import pandas as pd

from backend.services.factor_replay import metrics


def read_curve(root: Path, plan: dict, benchmark: pd.Series, strategy_id: str) -> dict:
    path, manifest_path = root / plan['nav_csv'], root / plan['manifest']
    manifest = json.loads(manifest_path.read_text())
    if manifest['status'] != 'PASS_CAUSAL_REPLAY_PARITY' or not manifest['ledger_daily_holdings_events_nav_parity_exact']:
        raise ValueError('公共回放等价审计未通过')
    frame = pd.read_csv(path, parse_dates=['trade_date']).set_index('trade_date')
    dates = frame.index
    if dates.empty or dates.has_duplicates or not dates.is_monotonic_increasing:
        raise ValueError('回放日期非法')
    if str(dates[0].date()) != manifest['start'] or str(dates[-1].date()) != manifest['end'] or len(dates) != manifest['nav_dates']:
        raise ValueError('回放区间与审计记录不一致')
    expected = benchmark.loc[dates.min():dates.max()]
    if not expected.index.equals(dates) or not np.isfinite(expected).all() or (expected <= 0).any():
        raise ValueError('中证500日期/价格不完整，不填补')
    columns = ['rule_nav', 'factor_nav', 'csi500_nav']
    if not np.isfinite(frame[columns]).all().all() or (frame[columns] <= 0).any().any():
        raise ValueError('回放净值非法')
    np.testing.assert_allclose(frame.rule_nav, frame.factor_nav, rtol=0, atol=0)
    np.testing.assert_allclose(frame.csi500_nav, expected / expected.iloc[0], rtol=1e-12, atol=1e-12)
    nav = frame[plan.get('nav_column', 'factor_nav')]
    calculated = metrics(nav)
    for key, value in manifest['equivalent_factor_metrics'].items():
        if value is not None and calculated.get(key) is not None:
            if not np.isclose(calculated[key], value, rtol=1e-10, atol=1e-10):
                raise ValueError(f'净值与manifest指标不一致: {key}')
    def display(series):
        m = metrics(series)
        n = len(series) - 1
        annual = float((series.iloc[-1] / series.iloc[0]) ** (252 / n) - 1) if n >= 252 else None
        return {'total_return': m['total_return'], 'max_drawdown': m['max_drawdown'],
                'calendar_cagr': m['calendar_cagr'], 'annual_return': annual,
                'sharpe_rf0': m['sharpe'] if n >= 252 else None,
                'calmar': annual / abs(m['max_drawdown']) if annual is not None and m['max_drawdown'] < 0 else None}
    return {'status': 'ok', 'kind': 'factor_causal_replay_net', 'strategy_id': strategy_id,
            'reason': '固定因子版本的回顾式研究回放；原规则同评测器逐笔/NAV等价；非生产或盲测业绩。',
            'methodology': 't收盘信号→次交易日开盘；最多5仓；信号价下8%止损/10个有效close；每边15.5bp；分数股；缺价留痕，期末不强平。当前池非PIT，涨跌停/ST/容量及发布时点待验证。年化主榜按252日；日历CAGR单列。',
            'benchmark_code': '000905.SH', 'benchmark_name': '中证500价格指数',
            'dates': [str(d.date()) for d in dates], 'nav': (nav / nav.iloc[0]).tolist(),
            'benchmark_nav': frame.csi500_nav.tolist(), 'metrics': display(nav),
            'benchmark_metrics': display(expected), 'observations': len(dates) - 1,
            'start_date': str(dates[0].date()), 'end_date': str(dates[-1].date()),
            'excess_return': float(nav.iloc[-1] / nav.iloc[0] / frame.csi500_nav.iloc[-1] - 1),
            'comparators': [{'id': 'moneyflow_rule_causal', 'name': '原规则（同公共评测器，与因子线重合）',
                             'nav': frame.rule_nav.tolist(), 'metrics': display(frame.rule_nav)}],
            'annualization': 'annual_return=252日复合；calendar_cagr=365.25日历天',
            'source': str(path.relative_to(root)), 'manifest': str(manifest_path.relative_to(root)),
            'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'factor_versions': manifest['factor_versions']}
