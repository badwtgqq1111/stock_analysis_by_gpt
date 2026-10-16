"""Read-only, date-aware risk view. No subprocess or research refresh on GET."""
from __future__ import annotations
import json
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'output/research/volatility_shadow'


class RiskQueryError(ValueError):
    pass


def _rows(frame):
    return json.loads(frame.to_json(orient='records', date_format='iso'))


def read_risk(date: str | None = None, variant: str = 'cn_ensemble_volcap', out: Path | None = None) -> dict:
    out = out or OUT
    audit_path = out / 'risk_audit.csv'
    if not audit_path.exists():
        return {'available': False, 'hint': '研究风险产物待生成', 'dates': [], 'variants': []}
    audits = pd.read_csv(audit_path)
    audits['trade_date'] = pd.to_datetime(audits['trade_date']).dt.strftime('%Y-%m-%d')
    dates = sorted(audits['trade_date'].unique().tolist(), reverse=True)
    variants = sorted(audits.variant.unique().tolist())
    if variant not in variants:
        raise RiskQueryError('未知研究变体')
    if date is not None and date not in dates:
        raise RiskQueryError('该历史决策日没有风险记录')
    selected_date = date or audits[audits.variant == variant].trade_date.max()
    selected = audits[(audits['trade_date'] == selected_date) & (audits.variant == variant)]
    risks = pd.read_csv(out / 'risk_contributions.csv')
    risks['trade_date'] = pd.to_datetime(risks['trade_date']).dt.strftime('%Y-%m-%d')
    risks = risks[(risks['trade_date'] == selected_date) & (risks.variant == variant)].copy()
    risks['risk_share'] = risks.component_vol.div(risks.portfolio_vol.where(risks.portfolio_vol != 0))
    risks['reference_budget_share'] = 1 / len(risks) if len(risks) else 0
    risks['budget_deviation'] = risks.risk_share - risks.reference_budget_share
    # Recompute as-of: never show full-sample future correlations for an old date.
    navs = {}
    for name in variants:
        p = out / 'common' / name / 'nav.csv'
        if p.exists():
            f = pd.read_csv(p)
            idx = pd.to_datetime(f.asof_date)
            navs[name] = pd.Series(f.nav.to_numpy(), index=idx).loc[:selected_date]
    daily = pd.DataFrame(navs).pct_change(fill_method=None).dropna()
    corr = daily.corr() if len(daily) >= 2 else pd.DataFrame()
    metadata = json.loads((out / 'methodology.json').read_text()) if (out / 'methodology.json').exists() else {}
    promotion_path = out / 'forward' / 'latest.json'
    forward = json.loads(promotion_path.read_text()) if promotion_path.exists() else {'status': 'not_started', 'gates': {}}
    return {'available': True, 'date': selected_date, 'variant': variant, 'dates': dates, 'variants': variants,
            'audit': _rows(selected), 'contributions': _rows(risks),
            'target_vol': metadata.get('target_vol', .15) if variant == 'cn_ensemble_volcap' else None,
            'correlation': {'names': corr.columns.tolist(), 'rows': _rows(corr.reset_index(names='variant')),
                            'observations': len(daily), 'asof': selected_date, 'short_sample': len(daily) < 252},
            'forward': forward, 'hint': '等风险份额仅作参照，非实际优化约束；相关性按所选日截断。历史回放不计入前向晋级。'}
