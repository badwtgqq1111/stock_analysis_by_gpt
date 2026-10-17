#!/usr/bin/env python3
"""Compare a research-only institutional portfolio layer with the rule baseline."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from p2_71_model_matrix import ROOT, load_data
from backend.services.factor_lightgbm import labels
from backend.services.factor_moneyflow import equivalent_spec
from backend.services.factor_portfolio_optimizer import OptimizerConfig, attach_target_weights
from backend.services.factor_probability import EmpiricalProbability
from backend.services.factor_replay import ReplayConfig, metrics, replay
from p2_71_probability_study import score_frame


FACTORS = ("mf_strength", "mf_persistence", "mf_return20", "price_to_ma20", "log_amount20", "log_price")


def build_returns(bars: pd.DataFrame, days: pd.DatetimeIndex) -> pd.DataFrame:
    close = bars[bars.trade_date.isin(days)].pivot(index='trade_date', columns='stock_code', values='close').sort_index()
    return close.pct_change().replace([np.inf, -np.inf], np.nan).dropna(how='all')


def run(output: Path):
    protocol = json.loads((ROOT / "config/p2_71_lightgbm.json").read_text())
    output.mkdir(parents=True, exist_ok=True)
    bars, signals, x, benchmark, calendar, _ = load_data(protocol)
    train = labels(x, bars, calendar, *protocol["train"], boundary=protocol["validation"][0], horizon=protocol["horizon_sessions"])
    mapper = EmpiricalProbability.fit(train, FACTORS)
    scored = score_frame(x, mapper, "logit_sum")
    spec = equivalent_spec(); execution = spec["execution"]
    replay_kwargs = dict(max_positions=execution["max_positions"], max_hold=execution["max_hold"], stop_pct=execution["stop_pct"], stop_basis=execution["stop_basis"], holding_clock=execution["holding_clock"], cost_one_way=execution["cost_one_way"])
    valid_days = calendar[(calendar >= pd.Timestamp(protocol["validation"][0])) & (calendar <= pd.Timestamp(protocol["validation"][1]))]
    eval_days = calendar[(calendar >= pd.Timestamp(protocol["evaluation"][0])) & (calendar <= pd.Timestamp(protocol["evaluation"][1]))]
    # Covariance is fitted from the immediately preceding realized returns only.
    all_days = calendar
    returns = build_returns(bars, all_days)
    variants = [("rule_baseline", signals, None), ("institutional_equal_risk", scored, OptimizerConfig(risk_aversion=4.0, turnover_penalty=.20, max_weight=.30, target_gross=.90)), ("institutional_conservative", scored, OptimizerConfig(risk_aversion=8.0, turnover_penalty=.35, max_weight=.25, target_gross=.90))]
    rows = []
    for name, frame, opt in variants:
        if opt is not None:
            frame = frame.copy()
            cov_by_day = {}
            for day, group in frame.groupby('trade_date', sort=True):
                names = list(group.stock_code.astype(str))
                prior = returns.loc[returns.index < day, names].tail(60) if set(names) <= set(returns.columns) else pd.DataFrame()
                if len(prior) >= 10:
                    cov_by_day[day] = prior.cov().to_numpy() + np.eye(len(names)) * 1e-4
            frame = attach_target_weights(frame, cov_by_day, config=opt)
        else:
            frame = frame.copy()
        vr = replay(frame[frame.trade_date.between(protocol["validation"][0], protocol["validation"][1])], bars[bars.trade_date.isin(valid_days)], benchmark, ReplayConfig(**replay_kwargs), valid_days)
        er = replay(frame[frame.trade_date.between(protocol["evaluation"][0], protocol["evaluation"][1])], bars[bars.trade_date.isin(eval_days)], benchmark, ReplayConfig(**replay_kwargs), eval_days)
        vm, em = metrics(vr['nav']), metrics(er['nav'])
        rows.append({'variant': name, 'validation_cagr': vm['calendar_cagr'], 'validation_drawdown': vm['max_drawdown'], 'validation_sharpe': vm['sharpe'], 'evaluation_cagr': em['calendar_cagr'], 'evaluation_drawdown': em['max_drawdown'], 'evaluation_sharpe': em['sharpe'], 'evaluation_returns': len(eval_days)-1, 'status': 'RETAIN_BASELINE'})
        pd.DataFrame({'nav': er['nav'], 'benchmark_nav': er['benchmark_nav']}).to_csv(output / f'{name}_evaluation_nav.csv')
    result = {'status': 'RETAIN_BASELINE', 'variants': rows, 'objective': 'mean return - risk_aversion*variance - turnover_penalty*turnover', 'holdout_not_used_for_selection': True, 'production_changed': False, 'created_at': datetime.now(timezone.utc).isoformat(), 'remaining': ['>=252 holdout returns', 'PIT/publication-time checks', 'capacity and forward shadow']}
    # A single comparison artifact makes the annualized curve directly
    # consumable by the strategy-pool chart without mixing evaluation windows.
    legacy = pd.read_csv(ROOT / 'output/research/p2_71/replay_audit/nav.csv', parse_dates=['trade_date'])
    eq_curve = pd.read_csv(output / 'institutional_equal_risk_evaluation_nav.csv', parse_dates=['trade_date'])
    comparison = legacy.merge(eq_curve[['trade_date', 'nav']], on='trade_date', how='inner').rename(columns={'nav': 'institutional_nav'})
    comparison.to_csv(output / 'comparison_nav.csv', index=False)
    result['comparison_curve'] = str((output / 'comparison_nav.csv').relative_to(ROOT))
    (output/'manifest.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    lines = ['# P2.71 机构式因子组合研究', '', '状态：**RETAIN_BASELINE**。目标权重优化只在独立因子链路中运行；公共回放负责成交、止损、费用。', '', '| 版本 | 验证CAGR | 验证回撤 | 留出CAGR | 留出回撤 | 留出Sharpe | 状态 |', '|---|---:|---:|---:|---:|---:|---|']
    for r in rows: lines.append(f"| {r['variant']} | {r['validation_cagr']:+.4%} | {r['validation_drawdown']:+.4%} | {r['evaluation_cagr']:+.4%} | {r['evaluation_drawdown']:+.4%} | {r['evaluation_sharpe']:+.3f} | {r['status']} |")
    lines += ['', f"留出观测：{len(eval_days)-1}（少于252，任何版本均不晋级）。机构式版本优化的是组合风险、集中度和换手，而非保证收益超过传统规则。", '', '[manifest](manifest.json)']
    (output/'REPORT.md').write_text('\n'.join(lines)+'\n')
    print(f"RETAIN_BASELINE variants={len(rows)} holdout_returns={len(eval_days)-1}")
    return result


if __name__ == '__main__':
    ap=argparse.ArgumentParser(); ap.add_argument('--output', type=Path, default=ROOT/'output/research/p2_71/institutional_v1'); run(ap.parse_args().output.resolve())
