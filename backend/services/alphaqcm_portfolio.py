"""Simple long-only portfolio replay for frozen AlphaQCM expressions.

This is deliberately separate from the production/LightGBM replay.  It is a
research comparison against the CSI500 price index:

* signal at close t -> select top-N -> execute at next session open
* equal weight, 20-session rebalance, 15.5bp one-way cost by default
* fractional shares, no forced liquidation, close marks

It is not a complete A-share execution simulator (no lots, limit/ST/suspension
masks, borrow or impact model).  The report states that boundary.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import json

import numpy as np
import pandas as pd

from backend.services.alphagen_native import evaluate_native
from backend.services.factor_replay import metrics
from backend.services.market_contract import benchmark_series


@dataclass(frozen=True)
class PortfolioConfig:
    top_n: int = 20
    rebalance_every: int = 20
    cost_one_way: float = 15.5 / 1e4
    initial_cash: float = 1.0

    def __post_init__(self):
        if self.top_n <= 0 or self.rebalance_every <= 0:
            raise ValueError('top_n/rebalance_every 必须为正')
        if not 0 <= self.cost_one_way < 1 or self.initial_cash <= 0:
            raise ValueError('成本/初始现金非法')


def load_factors(run_dir: Path) -> dict:
    path = Path(run_dir) / 'factors.json'
    if not path.exists():
        raise FileNotFoundError(path)
    doc = json.loads(path.read_text())
    if not doc.get('factors'):
        raise ValueError('factors.json 没有候选')
    return doc


def composite_signal(factors: list[dict], panel: pd.DataFrame) -> pd.Series:
    """Cross-sectional z-score each native factor, then weighted sum."""
    pieces = []
    for factor in factors:
        values = evaluate_native(factor['expression'], panel).rename(factor['name'])
        pieces.append(values)
    frame = pd.concat(pieces, axis=1)
    z = frame.groupby(level=0, group_keys=False).apply(
        lambda g: (g - g.mean()) / g.std(ddof=0).replace(0, np.nan),
        include_groups=False)
    weights = {f['name']: float(f.get('weight', 1.0)) for f in factors}
    numerator = sum(z[name].fillna(0.0) * weights[name] for name in z.columns)
    denominator = sum(z[name].notna().astype(float) * abs(weights[name]) for name in z.columns)
    out = numerator / denominator.replace(0, np.nan)
    out.name = 'alphaqcm_score'
    return out.replace([np.inf, -np.inf], np.nan)


def _selected(score: pd.Series, top_n: int) -> list[str]:
    if score.empty:
        return []
    ranked = score.dropna().sort_index()
    # deterministic tie-break by stock code
    ranked = ranked.sort_values(ascending=False, kind='stable')
    return list(ranked.index[:top_n])


def backtest_signal(panel: pd.DataFrame, signal: pd.Series, benchmark: pd.Series,
                    config: PortfolioConfig = PortfolioConfig()) -> dict:
    frame = panel.copy()
    frame['stock_code'] = frame.stock_code.astype(str)
    frame['trade_date'] = pd.to_datetime(frame.trade_date).dt.normalize()
    frame = frame.sort_values(['stock_code', 'trade_date'])
    dates = pd.DatetimeIndex(sorted(frame.trade_date.unique()))
    if dates.empty:
        raise ValueError('面板为空')
    b = pd.Series(benchmark).copy()
    b.index = pd.DatetimeIndex(pd.to_datetime(b.index)).normalize()
    b = b.reindex(dates)
    if b.isna().any() or (b <= 0).any():
        raise ValueError('中证500未覆盖全部回测日期')
    opens = frame.pivot(index='trade_date', columns='stock_code', values='open')
    closes = frame.pivot(index='trade_date', columns='stock_code', values='close')
    score = signal.unstack('stock_code') if isinstance(signal.index, pd.MultiIndex) else signal
    score = score.reindex(index=dates).reindex(columns=opens.columns)

    cash = float(config.initial_cash)
    shares: dict[str, float] = {}
    pending: list[str] | None = None
    rows, trades = [], []
    decision_dates = set(dates[::config.rebalance_every])
    for i, day in enumerate(dates):
        # execute the prior close's target at today's open
        if pending is not None:
            equity = cash + sum(q * float(opens.loc[day, c]) for c, q in shares.items()
                                if np.isfinite(opens.loc[day, c]))
            valid = [c for c in pending if c in opens.columns and np.isfinite(opens.loc[day, c])]
            target = equity / len(valid) if valid else 0.0
            # sells first, then buys
            for code in list(shares):
                if code not in valid:
                    px = opens.loc[day, code]
                    if np.isfinite(px):
                        gross = shares[code] * float(px)
                        fee = gross * config.cost_one_way
                        cash += gross - fee
                        trades.append({'trade_date': day, 'code': code, 'side': 'SELL',
                                       'price': float(px), 'gross': gross, 'fee': fee})
                        del shares[code]
            for code in valid:
                px = float(opens.loc[day, code])
                desired = target / px
                current = shares.get(code, 0.0)
                delta = desired - current
                if abs(delta * px) < 1e-12:
                    continue
                gross = abs(delta) * px
                fee = gross * config.cost_one_way
                if delta > 0:
                    cash -= gross + fee
                else:
                    cash += gross - fee
                shares[code] = desired
                trades.append({'trade_date': day, 'code': code,
                               'side': 'BUY' if delta > 0 else 'SELL',
                               'price': px, 'gross': gross, 'fee': fee})
            pending = None

        close_equity = cash + sum(q * float(closes.loc[day, c]) for c, q in shares.items()
                                  if np.isfinite(closes.loc[day, c]))
        rows.append({'trade_date': day, 'nav': close_equity / config.initial_cash,
                     'cash': cash, 'n_positions': len(shares),
                     'benchmark_nav': float(b.loc[day] / b.iloc[0])})
        if day in decision_dates:
            selected = _selected(score.loc[day].dropna(), config.top_n)
            pending = selected

    nav = pd.DataFrame(rows).set_index('trade_date')
    nav['portfolio_nav'] = nav.nav / nav.nav.iloc[0]
    port_metrics = metrics(nav.portfolio_nav)
    bench_metrics = metrics(nav.benchmark_nav)
    n = len(nav) - 1
    annual = ((nav.portfolio_nav.iloc[-1] / nav.portfolio_nav.iloc[0]) ** (252 / n) - 1) if n > 0 else None
    bench_annual = ((nav.benchmark_nav.iloc[-1] / nav.benchmark_nav.iloc[0]) ** (252 / n) - 1) if n > 0 else None
    return {
        'config': asdict(config),
        'nav': nav,
        'trades': pd.DataFrame(trades),
        'portfolio_metrics': {**port_metrics, 'annual_return_252': float(annual) if annual is not None else None,
                              'calmar': float(annual / abs(port_metrics['max_drawdown']))
                              if annual is not None and port_metrics['max_drawdown'] < 0 else None},
        'benchmark_metrics': {**bench_metrics, 'annual_return_252': float(bench_annual) if bench_annual is not None else None,
                              'calmar': float(bench_annual / abs(bench_metrics['max_drawdown']))
                              if bench_annual is not None and bench_metrics['max_drawdown'] < 0 else None},
        'excess_return': float(nav.portfolio_nav.iloc[-1] / nav.benchmark_nav.iloc[-1] - 1),
        'observations': n,
    }


def run_from_run(run_dir: Path, panel: pd.DataFrame, *, start: str, end: str,
                 output: Path, config: PortfolioConfig = PortfolioConfig()) -> dict:
    doc = load_factors(run_dir)
    sub = panel[(panel.trade_date >= start) & (panel.trade_date <= end)].copy()
    if sub.empty:
        raise ValueError('回测区间为空')
    signal = composite_signal(doc['factors'], panel)
    bench = benchmark_series('000905.SH', start, end)
    result = backtest_signal(sub, signal, bench, config)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    nav = result['nav']
    nav.to_csv(output / 'nav.csv')
    result['trades'].to_csv(output / 'trades.csv', index=False)
    payload = {
        'status': 'PASS_ALPHAQCM_PORTFOLIO_REPLAY',
        'run_dir': str(run_dir), 'start': start, 'end': end,
        'config': asdict(config),
        'portfolio': result['portfolio_metrics'],
        'csi500': result['benchmark_metrics'],
        'excess_return': result['excess_return'],
        'observations': result['observations'],
        'factor_count': len(doc['factors']),
        'limitations': [
            'research long-only replay on the selected liquid panel, not full-market PIT',
            'fractional shares; no lots/limit/ST/suspension/capacity/borrow model',
            'CSI500 is a price index; dividends are not included',
            'retrospective window; not a forward shadow or production performance',
        ],
    }
    (output / 'metrics.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                                    default=str) + '\n')
    lines = [
        '# P2.71 AlphaQCM 组合回测与中证500对比', '',
        f'回测区间：{start} → {end}；{len(doc["factors"])}个冻结表达式；'
        f'top{config.top_n}等权、{config.rebalance_every}日调仓、每边{config.cost_one_way*1e4:.1f}bp。', '',
        '| 指标 | AlphaQCM组合 | 中证500 |', '|---|---:|---:|',
    ]
    for key, label, pct in [('total_return', '累计收益', True),
                            ('annual_return_252', '年化收益(252日)', True),
                            ('max_drawdown', '最大回撤', True),
                            ('sharpe', 'Sharpe(rf=0)', False),
                            ('calmar', 'Calmar', False)]:
        a, b = result['portfolio_metrics'].get(key), result['benchmark_metrics'].get(key)
        fmt = lambda v: f'{v*100:+.2f}%' if pct and v is not None else (
            f'{v:+.3f}' if v is not None else '—')
        lines.append(f'| {label} | {fmt(a)} | {fmt(b)} |')
    lines += ['', f'相对中证500累计超额：**{result["excess_return"]*100:+.2f}%**。', '',
              '[净值曲线](nav.csv) · [逐笔交易](trades.csv) · [指标](metrics.json)', '',
              '## 边界', '',
              '- 组合使用冻结表达式的研究分数，不因test结果回选。',
              '- 这是流动性面板上的长仓研究回放，不是中证500全成分PIT或生产策略。',
              '- 含15.5bp单边成本；无整手、涨跌停/ST/停牌、容量和冲击模型。',
              '- 中证500为价格指数，未计股息；不是前向影子。']
    (output / 'REPORT.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps({k: payload[k] for k in ('observations', 'excess_return', 'factor_count')},
                     ensure_ascii=False, indent=2))
    return payload
