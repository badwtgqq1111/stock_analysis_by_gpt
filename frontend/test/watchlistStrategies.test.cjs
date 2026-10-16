const assert = require('node:assert/strict')
const { strategyGroups, groupRows, sortGroups } = require(process.argv[2])

const strategies = ['cn_ensemble', 'arm_select', 'arm_concept', 'jq_multi_factor_size_roe', 'jq_roe_roa_diagnostic']
  .map((id) => ({ id, name: id, stage: id.startsWith('jq_') ? 'candidate' : 'production', selection_source: `source_${id}` }))
const groups = strategyGroups([...strategies,
  { id: 'future_strategy', name: '未来新策略', stage: 'research', selection_source: 'new_source' },
  { id: 'no_list', name: '无名单', stage: 'candidate' },
  { id: 'retired', name: '已下线', stage: 'retired', selection_source: 'retired_source' },
])
assert.equal(groups.length, 6)
assert.deepEqual(groups.slice(0, 5).map((g) => g.key), strategies.map((s) => s.id))
assert.equal(groups[5].source, 'new_source')
console.log('PASS registry: five current strategies plus future additions; retired/no-list excluded')

groups[0].rows = [{ code: '600001.SH', name: 'A' }, { code: '600002.SH', name: 'B' }]
groups[1].rows = [{ code: '600001.SH', name: 'A' }]
groups[4].rows = [{ code: '000001.SZ', name: 'C' }]
const paper = { ...groups[0], key: 'paper', label: '模拟盘持仓', rows: [{ code: 'paper-only', name: 'P' }] }
const all = groupRows([...groups, paper], 'all')
assert.equal(all.length, 3)
assert.equal(all[0].tag, 'cn_ensemble +1')
assert.ok(!all.some((row) => row.code === 'paper-only'))
assert.deepEqual(groupRows(groups, 'jq_roe_roa_diagnostic').map((r) => r.code), ['000001.SZ'])
assert.equal(groupRows(groups, 'local').length, 0)
assert.equal(groupRows([paper], 'paper').length, 1)
assert.ok(all.every((r) => !r.local))
console.log('PASS rows: deduplicated union uses compact primary strategy tag; per-strategy/personal/paper remain separate')

const rankedEntries = strategies.map((s, i) => ({ ...s,
  annual_rank: i < 3 ? { rank: [3, 1, 2][i], value: [.0, .3, .1][i], metric: 'annual_return', of: 3 } : undefined,
}))
const rankedGroups = strategyGroups(rankedEntries)
rankedGroups[0].rows = [{code: 'shared', name: 'same'}, {code: 'last', name: 'L'}]
rankedGroups[1].rows = [{code: 'first', name: 'F'}, {code: 'shared', name: 'same'}]
const before = rankedGroups.map(g => g.key)
const ordered = sortGroups([...rankedGroups, paper], 'annual')
assert.deepEqual(ordered.slice(0, 3).map(g => g.key), [strategies[1].id, strategies[2].id, strategies[0].id])
assert.equal(ordered.at(-1).key, 'paper')
assert.deepEqual(rankedGroups.map(g => g.key), before, 'sorting must not mutate pool order')
const union = groupRows(ordered, 'all')
assert.deepEqual(union.map(x => x.code), ['first', 'shared', 'last'])
assert.equal(union[1].tag, 'arm_select +1')
assert.deepEqual(groupRows(ordered, strategies[0].id).map(x => x.code), ['shared', 'last'])
const { annualRankOrder } = require(require('node:path').join(require('node:path').dirname(process.argv[2]), 'strategyRanking.js'))
const poolOrder = [...rankedEntries].sort((a,b) => annualRankOrder(a.annual_rank?.rank, b.annual_rank?.rank, a.id, b.id))
assert.deepEqual(ordered.filter(g => g.key !== 'paper').map(g => g.key), poolOrder.map(s => s.id))
assert.ok(annualRankOrder(NaN, 1, 'a', 'b') > 0)
assert.ok(annualRankOrder(undefined, undefined, 'a', 'b') < 0)
assert.deepEqual(sortGroups(rankedGroups, 'default').map(g => g.key), before)
console.log('PASS annual: shared pool/watchlist order; missing last; paper separate; union follows rank; stock rank unchanged')

const live = { ...groups[0], key: 'live_holdings', label: '实盘镜像', annual_rank: undefined,
  verdict: undefined, rows: [{code:'live-only', name:'L'}, {code:'shared',name:'same'}] }
const input = [...rankedGroups, paper, live]
const inputBefore = JSON.stringify(input)
for (const mode of ['annual', 'default', 'total', 'excess', 't', 'name']) {
  const pinned = sortGroups(input, mode)
  assert.equal(pinned[0].key, 'live_holdings', mode + ': live must always be first')
  assert.deepEqual(pinned.slice(1).map(g => g.key), sortGroups(input.filter(g => g.key !== 'live_holdings'), mode).map(g => g.key),
    mode + ': other groups must retain their sorting rules')
  assert.equal(new Set(pinned.map(g=>g.key)).size, input.length)
  assert.deepEqual(groupRows(pinned, 'all').slice(0, 2).map(r=>r.code), ['live-only','shared'])
  assert.equal(groupRows(pinned, 'all')[1].tag, '实盘镜像 +2')
  assert.deepEqual(groupRows(pinned, strategies[0].id).map(r=>r.code), ['shared','last'])
}
assert.equal(JSON.stringify(input), inputBefore, 'pinning must not mutate inputs')
assert.deepEqual(sortGroups([], 'annual'), [])
assert.equal(sortGroups([live], 'name')[0], live)
console.log('PASS live pin: all 6 sort modes; merged rows live first; other groups and stock order unchanged; no mutation')
