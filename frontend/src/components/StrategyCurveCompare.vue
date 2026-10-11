<!--
  多策略对比曲线：把所有选定策略的净值与**中证500** 画在同一张图上。

  两个必须做对的点（否则对比是假的）：
    1. **共同窗口**：各策略曲线起止不同（rot_arm 从 2025-09、first_ban_lowxi 从 2024-01、
       生产策略只有几天），所以先取所有曲线的**日期交集**，再各自归一到窗口起点=1；
    2. **口径要标出来**：每条线的口径不同（名单跟踪毛收益 / 模拟盘净值含成本 / 策略原始口径逐笔），
       图例里逐条写明，避免把毛收益和净收益画在一起还当同一件事比。
-->
<template>
  <div class="compare">
    <div class="legend">
      <span class="lg" v-if="benchmarkPoints">
        <i style="background:#e0e0e0" />中证500 <b class="dim">{{ fmt(benchmarkTotal) }}</b>
      </span>
      <span class="lg" v-for="s in shown" :key="s.id" :title="s.kindLabel">
        <i :style="{ background: s.color }" />{{ s.label }}
        <b :class="s.total < 0 ? 'down' : 'up'">{{ fmt(s.total) }}</b>
        <span class="tiny dim">相对中证500 {{ fmt(s.excess) }}</span>
      </span>
    </div>
    <svg :viewBox="`0 0 1000 ${height}`" preserveAspectRatio="none" :style="{ height: height + 'px' }">
      <line v-for="n in 4" :key="n" :x1="0" :x2="1000" :y1="(height / 4) * n" :y2="(height / 4) * n"
            stroke="var(--border-color)" stroke-width="1" vector-effect="non-scaling-stroke" />
      <polyline v-if="benchmarkPoints" :points="benchmarkPoints" fill="none" stroke="#e0e0e0"
                stroke-width="2" stroke-dasharray="6 4" vector-effect="non-scaling-stroke" />
      <polyline v-for="s in paths" :key="s.id" :points="s.points" fill="none" :stroke="s.color"
                stroke-width="1.8" vector-effect="non-scaling-stroke" />
    </svg>
    <div class="axis">
      <span>{{ windowLabel }}</span>
      <span class="dim">共同窗口内归一到 1；{{ observations }} 个交易日</span>
    </div>
    <p class="hint tiny warn-text" v-if="paths.length && observations < 20">
      ⚠️ 共同窗口只有 {{ observations }} 个交易日：这说明被勾选的策略里有的历史太短 ——
      点掉它们（或在左侧列表里选样本更长的策略）再看对比。
    </p>
    <p class="hint tiny" v-if="!paths.length">还没有可对比的曲线：勾选左侧策略（或先跑
      <code>build_strategy_pool_performance.py</code>）。</p>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { KIND_LABEL } from './strategyDetail'

export interface CompareSeries {
  id: string
  label: string
  kind: string
  dates: string[]
  nav: number[]
}

const props = withDefaults(defineProps<{ series: CompareSeries[]; height?: number }>(), { height: 260 })

const COLORS = ['#ffd166', '#118ab2', '#06d6a0', '#ef476f', '#9d4edd', '#ff9f1c', '#8ecae6', '#b5e48c']
const colorOf = (index: number) => COLORS[index % COLORS.length]

/** 共同窗口：所有曲线日期范围的交集（没有交集就不画对比，避免"看起来能比"）。 */
const windowRange = computed<[string, string] | null>(() => {
  const usable = props.series.filter((s) => s.dates.length > 1)
  if (usable.length < 2) return null
  const starts = usable.map((s) => s.dates[0]).sort()
  const ends = usable.map((s) => s.dates[s.dates.length - 1]).sort()
  const start = starts[starts.length - 1]
  const end = ends[0]
  return start < end ? [start, end] : null
})

function slice(series: CompareSeries): { dates: string[]; nav: number[] } | null {
  const range = windowRange.value
  if (!range) return null
  const keep: number[] = []
  series.dates.forEach((d, i) => { if (d >= range[0] && d <= range[1]) keep.push(i) })
  if (keep.length < 2 || series.nav[keep[0]] <= 0) return null
  const base = series.nav[keep[0]]
  return { dates: keep.map((i) => series.dates[i]), nav: keep.map((i) => series.nav[i] / base) }
}

const shown = computed(() =>
  props.series
    .filter((s) => s.id !== '__benchmark__')          // 基准单独画，别混进"策略"图例
    .map((s, i) => {
      const sliced = slice(s)
      if (!sliced) return null
      return {
        id: s.id, label: s.label, color: colorOf(i),
        kindLabel: KIND_LABEL[s.kind] || s.kind || '曲线',
        nav: sliced.nav, dates: sliced.dates,
        total: sliced.nav[sliced.nav.length - 1] - 1,
        excess: null as number | null,
      }
    })
    .filter((s): s is NonNullable<typeof s> => s !== null))

const benchmark = computed(() => {
  const bench = props.series.find((s) => s.id === '__benchmark__')
  return bench ? slice(bench) : null
})

const observations = computed(() => shown.value[0]?.nav.length ?? 0)

const bounds = computed(() => {
  const all = [...shown.value.flatMap((s) => s.nav), ...(benchmark.value?.nav ?? [])].filter(Number.isFinite)
  if (!all.length) return { min: 0, max: 1 }
  const min = Math.min(...all), max = Math.max(...all)
  return min === max ? { min: min - 0.05, max: max + 0.05 } : { min, max }
})

function polyline(values: number[]): string {
  const n = values.length
  const { min, max } = bounds.value
  const span = max - min || 1
  return values
    .map((v, i) => {
      const x = n > 1 ? (i / (n - 1)) * 1000 : 0
      const y = props.height - ((Number.isFinite(v) ? v : min) - min) / span * props.height
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
}

const paths = computed(() => shown.value.map((s) => ({ id: s.id, color: s.color, points: polyline(s.nav) })))
const benchmarkPoints = computed(() => (benchmark.value ? polyline(benchmark.value.nav) : ''))
const benchmarkTotal = computed(() => (benchmark.value ? benchmark.value.nav[benchmark.value.nav.length - 1] - 1 : null))
const windowLabel = computed(() => {
  const range = windowRange.value
  return range ? `${range[0]} — ${range[1]}` : '（曲线之间没有重叠区间，暂不对比）'
})

const fmt = (v: number | null) => (v === null || !Number.isFinite(v) ? '—' : `${v > 0 ? '+' : ''}${(v * 100).toFixed(2)}%`)
</script>

<style scoped>
.compare { width: 100%; }
.legend { display: flex; gap: var(--sp-3); flex-wrap: wrap; font-size: var(--fs-xs);
  color: var(--text-muted); margin-bottom: 4px; }
.lg { display: inline-flex; align-items: center; gap: 5px; }
.lg i { width: 12px; height: 3px; border-radius: 2px; display: inline-block; }
svg { width: 100%; display: block; background: var(--bg-input); border: 1px solid var(--border-color);
  border-radius: var(--r-2); }
.axis { display: flex; justify-content: space-between; font-size: var(--fs-xs);
  color: var(--text-dim); margin-top: 2px; }
</style>
