<template>
  <section class="performance">
    <div class="tabs">
      <button class="btn-ghost btn-sm" :class="{ active: mode === 'tracking' }" @click="mode = 'tracking'">名单跟踪</button>
      <button class="btn-ghost btn-sm" :disabled="!data?.common" :class="{ active: mode === 'common' }" @click="mode = 'common'">同窗比较</button>
      <button v-if="data?.research" class="btn-ghost btn-sm" :class="{ active: mode === 'research' }" @click="mode = 'research'">独立研究回测</button>
    </div>
    <p v-if="data?.stale" class="hint tiny">绩效快照待更新，请运行 build_strategy_pool_performance.py。</p>
    <template v-if="curve?.nav?.length">
      <SvgCurve :series="series" :labels="curve.dates" :height="170" />
      <p class="tiny dim">{{ curve.start_date }} — {{ curve.end_date }} · {{ curve.observations ?? curve.dates.length - 1 }} 个收益观测</p>
      <table class="data-table tiny">
        <thead><tr><th>指标</th><th>策略</th><th>中证500</th></tr></thead>
        <tbody><tr v-for="m in metricDefs" :key="m.key">
          <td>{{ m.label }}</td><td class="mono">{{ fmt(curve.metrics[m.key], m.pct) }}</td>
          <td class="mono">{{ fmt(curve.benchmark_metrics[m.key], m.pct) }}</td>
        </tr></tbody>
      </table>
      <p class="tiny">相对中证500累计超额：<b class="mono">{{ fmt(curve.excess_return, true) }}</b>（策略净值 / 指数净值 − 1）</p>
    </template>
    <p v-else class="hint tiny">{{ curve?.reason || '历史不足：等待归档名单和后续交易日行情，暂未生成中证500对比曲线。' }}</p>
    <p v-if="curve?.nav?.length && curve.reason" class="hint tiny">{{ curve.reason }}</p>
    <p class="tiny dim">{{ curve?.methodology || '统一基准：中证500（000905.SH）。' }}</p>
    <p class="tiny" v-if="curve?.kind">
      当前曲线：<b>{{ curve.kind === 'shadow_account_net' ? '模拟盘净值（含成本）'
        : curve.kind === 'strategy_rule_net' ? '策略原始口径（逐笔模拟）'
        : (mode === 'research' ? '独立研究回放' : mode === 'common' ? '同窗比较' : '名单跟踪（毛收益）') }}</b>
      · 基准 {{ curve.benchmark_name || '中证500' }}（{{ curve.benchmark_code || '000905.SH' }}） · {{ curve.observations ?? (curve.dates.length - 1) }} 个收益观测
    </p>
    <p v-if="curve?.metrics?.total_return != null && curve.metrics.total_return < 0" class="tiny warn-text">
      ⚠️ 该口径为<b>负收益</b>：曲线与指标只用于说明「这条规则不可上线」，不代表可交易收益。
    </p>
    <p v-if="mode === 'tracking'" class="tiny dim">各卡历史长度不同；跨策略排名请使用同窗比较。卡玛 = 年化收益 / |最大回撤|；短样本不作年化排名。</p>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import type { StrategyPerformance } from '@/types/api'
import SvgCurve from '@/components/ui/SvgCurve.vue'

const props = defineProps<{ data?: StrategyPerformance }>()
// 默认打开哪条曲线：名单跟踪（逐日毛收益）优先，但**样本太短时自动切到研究/模拟盘曲线** ——
// 稀疏决策日的策略（如 rot_arm 每周/每月才出一份名单）逐日跟踪只有 8 个观测，
// 拿它当默认就等于"打开卡片只看到一条几天的线"，而模拟盘曲线有 259 个观测（同执行假设、含成本）。
function defaultMode(data?: StrategyPerformance): 'tracking' | 'research' | 'common' {
  const tracking = data?.tracking
  const research = data?.research
  if (research?.nav?.length && (!tracking?.nav?.length || (tracking.observations ?? 0) < 60)) return 'research'
  return 'tracking'
}
const mode = ref<'tracking' | 'research' | 'common'>(defaultMode(props.data))
const curve = computed(() => props.data?.[mode.value])
const series = computed(() => curve.value ? [
  { name: mode.value === 'research' ? '研究回测净值' : '名单跟踪毛净值', color: 'var(--accent-gold)', points: curve.value.nav },
  { name: '中证500', color: 'var(--accent-blue)', points: curve.value.benchmark_nav },
] : [])
const metricDefs = [
  { key: 'total_return', label: '累计收益', pct: true },
  { key: 'max_drawdown', label: '最大回撤', pct: true },
  { key: 'annual_return', label: '年化收益', pct: true },
  { key: 'calmar', label: '卡玛', pct: false },
  { key: 'sharpe_rf0', label: '夏普（无风险=0）', pct: false },
]
function fmt(v: number | null | undefined, pct: boolean): string {
  if (v == null || !Number.isFinite(v)) return '—'
  return pct ? `${(v * 100).toFixed(2)}%` : v.toFixed(3)
}
</script>

<style scoped>
.performance { border-top: 1px solid var(--border-color); padding-top: var(--sp-2); }
.tabs { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: var(--sp-2); }
.active { color: var(--accent-gold); border-color: var(--accent-gold); }
.performance p { margin-top: 6px; line-height: 1.5; }
</style>
