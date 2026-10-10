const assert = require('node:assert/strict')
const { strategyGroups, groupRows } = require(process.argv[2])

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
assert.equal(all[0].tag, 'cn_ensemble · arm_select')
assert.ok(!all.some((row) => row.code === 'paper-only'))
assert.deepEqual(groupRows(groups, 'jq_roe_roa_diagnostic').map((r) => r.code), ['000001.SZ'])
assert.equal(groupRows(groups, 'local').length, 0)
assert.equal(groupRows([paper], 'paper').length, 1)
assert.ok(all.every((r) => !r.local))
console.log('PASS rows: deduplicated union retains all strategy labels; per-strategy/personal/paper remain separate')
