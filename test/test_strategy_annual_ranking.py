"""年化榜为两页共同排序来源；不把短样本外推或累计收益混进年化。"""
import copy
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('annual_pool_test', ROOT / 'scripts/sync_strategy_pool.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def entry(ident, total=.2, annual=.1, obs=252, excess=0.):
    return {'id': ident, 'performance': {'research': {
        'nav': [1.] * (obs + 1), 'observations': obs,
        'metrics': {'total_return': total, 'annual_return': annual},
        'excess_return': excess, 'start_date': '2020-01-01', 'end_date': '2026-10-15'}}}


def ids(rows):
    return [r['id'] for r in rows]


def test_annual_not_total_and_zero_is_not_missing():
    rows = [entry('long', 2., .1, 2000), entry('fast', .8, .3, 504),
            entry('zero', 0., 0.), entry('loss', -.1, -.1)]
    result = module._return_ranking(rows)
    assert ids(result['by_annual_return']) == ['fast', 'long', 'zero', 'loss']
    assert ids(result['by_total_return']) == ['long', 'fast', 'zero', 'loss']
    assert all(r['annual_rank']['metric'] == 'annual_return' for r in rows)
    assert rows[0]['annual_rank']['window'] == '2020-01-01 ~ 2026-10-15'


@pytest.mark.parametrize('value', [None, float('nan'), float('inf'), -float('inf'), True, 'junk'])
def test_nonfinite_annual_has_no_rank(value):
    row = entry('invalid', annual=value)
    result = module._return_ranking([row])
    assert result['by_annual_return'] == []
    assert 'annual_rank' not in row
    assert result['annual_excluded'][0]['reason'] == '缺少有效年化收益'


def test_short_samples_not_ranked_even_with_extreme_annual():
    rows = [entry('short', annual=99., obs=251), entry('enough', obs=252)]
    result = module._return_ranking(rows)
    assert ids(result['by_annual_return']) == ['enough']
    assert '251/252' in rows[0]['annual_rank_reason']


def test_stable_ties_and_zero_excess_beats_negative():
    rows = [entry('b'), entry('negative', excess=-.1), entry('a'), entry('missing', excess=None)]
    result = module._return_ranking(rows)
    assert ids(result['by_annual_return']) == ['a', 'b', 'negative', 'missing']
    assert ids(result['by_excess_return']) == ['a', 'b', 'negative']
    assert ids(module._return_ranking(list(reversed(copy.deepcopy(rows))))['by_annual_return']) == ids(result['by_annual_return'])


def test_tracking_fallback_observation_fallback_and_stale_rank_cleared():
    row = entry('tracking')
    curve = row['performance'].pop('research')
    curve.pop('observations')
    row['performance'].update(research={'metrics': {'total_return': 999}}, tracking=curve)
    assert module._return_ranking([row])['by_annual_return'][0]['observations'] == 252
    row['performance'] = {}
    assert module._return_ranking([row])['by_annual_return'] == []
    assert not any(k in row for k in ('annual_rank', 'return_rank', 'excess_rank'))


def test_annual_does_not_require_total_and_total_excess_remain_independent():
    row = entry('annual_only', total=None)
    p = module._return_ranking([row])
    assert ids(p['by_annual_return']) == ['annual_only']
    assert p['by_total_return'] == []


def test_view_contracts_risk_below_strategies_and_shared_default_sort():
    pool = (ROOT / 'frontend/src/pages/StrategyPool.vue').read_text()
    watch = (ROOT / 'frontend/src/components/StockWatchlist.vue').read_text()
    assert pool.index('class="pool-layout"') < pool.index('<VolatilityRiskPanel />')
    assert pool.index('<StrategyCurveCompare') < pool.index('<VolatilityRiskPanel />')
    assert "'pool'>('annual')" in pool and "ref<GroupSortKey>('annual')" in watch
    assert 'annualRankOrder(a.annual_rank?.rank, b.annual_rank?.rank, a.id, b.id)' in pool
    assert "groupRows(sortedGroups.value, 'all')" in watch
    assert 'groupRows(sortedGroups.value, group.value)' in watch
    assert 'const ranked = visible.value' in pool
