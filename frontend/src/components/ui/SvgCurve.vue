<template>
  <div class="svg-curve">
    <div class="legend">
      <span v-for="s in series" :key="s.name" class="lg">
        <i :style="{ background: s.color }" />{{ s.name }}
        <b class="dim">{{ last(s) }}</b>
      </span>
    </div>
    <svg :viewBox="`0 0 1000 ${height}`" preserveAspectRatio="none" :style="{ height: height + 'px' }">
      <line v-for="n in 4" :key="n" :x1="0" :x2="1000" :y1="(height / 4) * n" :y2="(height / 4) * n"
            stroke="var(--border-color)" stroke-width="1" vector-effect="non-scaling-stroke" />
      <polyline v-for="s in paths" :key="s.name" :points="s.points" fill="none"
                :stroke="s.color" stroke-width="1.6" vector-effect="non-scaling-stroke" />
    </svg>
    <div class="axis"><span>{{ firstLabel }}</span><span>{{ lastLabel }}</span></div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

export interface CurveSeries { name: string; color: string; points: number[] }
const props = withDefaults(defineProps<{ series: CurveSeries[]; height?: number; labels?: string[] }>(), {
  height: 200,
})

const bounds = computed(() => {
  const all = props.series.flatMap((s) => s.points.filter((v) => Number.isFinite(v)))
  if (!all.length) return { min: 0, max: 1 }
  const min = Math.min(...all), max = Math.max(...all)
  return min === max ? { min: min - 1, max: max + 1 } : { min, max }
})

const paths = computed(() =>
  props.series.map((s) => {
    const n = s.points.length
    const { min, max } = bounds.value
    const span = max - min || 1
    const pts = s.points
      .map((v, i) => {
        const x = n > 1 ? (i / (n - 1)) * 1000 : 0
        const y = props.height - ((Number.isFinite(v) ? v : min) - min) / span * props.height
        return `${x.toFixed(1)},${y.toFixed(1)}`
      })
      .join(' ')
    return { name: s.name, color: s.color, points: pts }
  }),
)

const firstLabel = computed(() => props.labels?.[0] ?? '')
const lastLabel = computed(() => props.labels?.[props.labels.length - 1] ?? '')
function last(s: CurveSeries) {
  const v = s.points[s.points.length - 1]
  return Number.isFinite(v) ? v.toFixed(3) : '—'
}
</script>

<style scoped>
.svg-curve { width: 100%; }
.legend { display: flex; gap: var(--sp-3); flex-wrap: wrap; font-size: var(--fs-xs);
  color: var(--text-muted); margin-bottom: 4px; }
.lg { display: inline-flex; align-items: center; gap: 5px; }
.lg i { width: 10px; height: 3px; border-radius: 2px; display: inline-block; }
svg { width: 100%; display: block; background: var(--bg-input); border: 1px solid var(--border-color);
  border-radius: var(--r-2); }
.axis { display: flex; justify-content: space-between; font-size: var(--fs-xs);
  color: var(--text-dim); margin-top: 2px; }
</style>
