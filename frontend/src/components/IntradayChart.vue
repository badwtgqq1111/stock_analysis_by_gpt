<template>
  <div ref="rootRef" class="ic" @mousemove="onMove" @mouseleave="hover = null">
    <div class="ic-legend">
      <span><i class="lg-price" :style="{ background: up ? 'var(--accent-up)' : 'var(--accent-down)' }" />价格</span>
      <span v-if="hasAvg"><i class="lg-avg" />均价</span>
      <span><i class="lg-prev" />前收 {{ fmt(prevClose) }}</span>
      <span class="dim tiny">共 {{ points.length }} 个点</span>
    </div>

    <div class="ic-body" :style="{ height: H + 'px' }">
      <svg :viewBox="`0 0 1000 ${H}`" preserveAspectRatio="none" :style="{ height: H + 'px' }">
        <!-- 网格 -->
        <line v-for="n in 4" :key="'g' + n" x1="0" x2="1000" :y1="(priceH / 4) * n" :y2="(priceH / 4) * n"
              stroke="var(--border-color)" stroke-width="1" vector-effect="non-scaling-stroke" />
        <!-- 前收基准线 -->
        <line v-if="prevY !== null" x1="0" x2="1000" :y1="prevY" :y2="prevY"
              stroke="var(--text-dim)" stroke-width="1" stroke-dasharray="4 4" vector-effect="non-scaling-stroke" />
        <!-- 价格面积 + 线 -->
        <polygon v-if="priceArea" :points="priceArea" :fill="fillColor" opacity="0.14" />
        <polyline v-if="priceLine" :points="priceLine" fill="none"
                  :stroke="up ? 'var(--accent-up)' : 'var(--accent-down)'"
                  stroke-width="1.6" vector-effect="non-scaling-stroke" />
        <!-- 均价线 -->
        <polyline v-if="avgLine" :points="avgLine" fill="none" stroke="var(--accent-gold)"
                  stroke-width="1.2" vector-effect="non-scaling-stroke" />
        <!-- 成交量 -->
        <rect v-for="(b, i) in volumeBars" :key="'v' + i" :x="b.x" :y="b.y" :width="b.w" :height="b.h"
              :fill="b.up ? 'var(--accent-up)' : 'var(--accent-down)'" opacity="0.55" />
        <!-- 十字光标 -->
        <line v-if="hover" :x1="hover.x" :x2="hover.x" y1="0" :y2="H"
              stroke="var(--accent-gold)" stroke-width="1" stroke-dasharray="3 3" vector-effect="non-scaling-stroke" />
      </svg>

      <!-- 价格刻度（HTML 覆盖层，避免 SVG 里文字被 preserveAspectRatio 拉扁）-->
      <div class="ic-scale">
        <span v-for="s in scaleMarks" :key="s.label" :style="{ top: s.top }">{{ s.label }}</span>
      </div>

      <div v-if="hover" class="ic-tip" :style="tipStyle">
        <div class="tip-time">{{ hover.point.clock }}</div>
        <div class="tip-row"><span>价格</span><b :class="toneOf(hover.point.price)">{{ fmt(hover.point.price) }}</b></div>
        <div class="tip-row"><span>均价</span><span>{{ fmt(hover.point.avg) }}</span></div>
        <div class="tip-row"><span>涨跌</span>
          <b :class="toneOf(hoverDiff)">{{ hoverDiff === null ? '—' : (hoverDiff > 0 ? '+' : '') + hoverDiff.toFixed(2)
            + '%' }}</b>
        </div>
        <div class="tip-row"><span>成交量</span><span>{{ vol(hover.point.volume_shares) }}</span></div>
      </div>
    </div>

    <div class="ic-axis">
      <span>09:30</span><span>10:30</span><span>11:30/13:00</span><span>14:00</span><span>15:00</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import type { IntradayPoint } from '@/types/api'

const props = withDefaults(
  defineProps<{ points: IntradayPoint[]; prevClose?: number | null; height?: number }>(),
  { prevClose: null, height: 320 },
)

const H = computed(() => props.height)
const priceH = computed(() => Math.round(H.value * 0.72))
const volTop = computed(() => priceH.value + 8)
const volH = computed(() => H.value - volTop.value)
const GAP = 6

const rootRef = ref<HTMLElement | null>(null)
const hover = ref<{ x: number; index: number; point: IntradayPoint } | null>(null)

/** 时间 → x 轴百分位（09:30~11:30 归一成 0~120，13:00~15:00 归一成 120~240，跳过午休）。 */
function minuteOf(clock: string): number {
  const [h, m] = clock.split(':').map(Number)
  const minutes = h * 60 + m
  if (minutes <= 11 * 60 + 30) return Math.max(0, minutes - (9 * 60 + 30))
  return 120 + Math.min(120, minutes - 13 * 60)
}

const valid = computed(() => props.points.filter((p) => p.price !== null))
const hasAvg = computed(() => valid.value.some((p) => p.avg !== null))

const bounds = computed(() => {
  const prices = valid.value.map((p) => p.price as number)
  const lows = valid.value.map((p) => (p.low ?? p.price) as number)
  const highs = valid.value.map((p) => (p.high ?? p.price) as number)
  const pool = [...prices, ...lows, ...highs]
  if (props.prevClose) pool.push(props.prevClose)
  if (!pool.length) return { min: 0, max: 1 }
  let min = Math.min(...pool)
  let max = Math.max(...pool)
  const pad = (max - min || Math.max(max * 0.005, 0.01)) * 0.08
  min -= pad; max += pad
  if (props.prevClose) {
    // 以昨收为轴，上下对称一点，分时的"红绿面积"才不会被压扁
    const span = Math.max(Math.abs(max - props.prevClose), Math.abs(props.prevClose - min))
    min = Math.min(min, props.prevClose - span)
    max = Math.max(max, props.prevClose + span)
  }
  return { min, max }
})

const xOf = (clock: string) => (minuteOf(clock) / 240) * 1000
const yOf = (price: number) => {
  const { min, max } = bounds.value
  const span = max - min || 1
  return priceH.value - ((price - min) / span) * priceH.value
}

const priceLine = computed(() => {
  if (!valid.value.length) return ''
  return valid.value.map((p) => `${xOf(p.clock).toFixed(1)},${yOf(p.price as number).toFixed(1)}`).join(' ')
})

const priceArea = computed(() => {
  if (valid.value.length < 2) return ''
  const base = props.prevClose ? yOf(props.prevClose) : priceH.value
  const first = valid.value[0], last = valid.value[valid.value.length - 1]
  return `${xOf(first.clock).toFixed(1)},${base.toFixed(1)} ${priceLine.value} `
    + `${xOf(last.clock).toFixed(1)},${base.toFixed(1)}`
})

const avgLine = computed(() => {
  const rows = valid.value.filter((p) => p.avg !== null)
  if (rows.length < 2) return ''
  return rows.map((p) => `${xOf(p.clock).toFixed(1)},${yOf(p.avg as number).toFixed(1)}`).join(' ')
})

const prevY = computed(() => (props.prevClose ? yOf(props.prevClose) : null))
const last = computed(() => valid.value[valid.value.length - 1])
const up = computed(() => (props.prevClose ? (last.value?.price ?? 0) >= props.prevClose : true))
const fillColor = computed(() => (up.value ? 'var(--accent-up)' : 'var(--accent-down)'))

const volumeBars = computed(() => {
  const maxVolume = Math.max(...valid.value.map((p) => p.volume_shares), 1)
  const width = Math.max(1000 / Math.max(valid.value.length, 1) - 0.6, 0.8)
  return valid.value.map((p, i) => {
    const h = (p.volume_shares / maxVolume) * (volH.value - GAP)
    const prev = i > 0 ? valid.value[i - 1].price as number : p.price as number
    return {
      x: xOf(p.clock) - width / 2,
      y: volTop.value + (volH.value - GAP - h),
      w: width,
      h: Math.max(h, 0.5),
      up: (p.price as number) >= prev,
    }
  })
})

const scaleMarks = computed(() => {
  const { min, max } = bounds.value
  const marks = []
  for (let i = 0; i <= 2; i++) {
    const value = max - ((max - min) / 2) * i
    marks.push({ label: value.toFixed(2), top: `${((yOf(value) / H.value) * 100).toFixed(2)}%` })
  }
  return marks
})

function onMove(event: MouseEvent) {
  const root = rootRef.value
  if (!root || !valid.value.length) return
  const rect = root.getBoundingClientRect()
  const ratio = Math.min(Math.max((event.clientX - rect.left) / rect.width, 0), 1)
  const targetMinute = ratio * 240
  let best = 0
  let bestDelta = Infinity
  valid.value.forEach((p, i) => {
    const delta = Math.abs(minuteOf(p.clock) - targetMinute)
    if (delta < bestDelta) { bestDelta = delta; best = i }
  })
  const point = valid.value[best]
  hover.value = { x: xOf(point.clock), index: best, point }
}

const tipStyle = computed(() => {
  if (!hover.value) return {}
  const left = Math.min(Math.max(hover.value.x / 10, 2), 72)
  return { left: `${left}%` }
})

const hoverDiff = computed(() => {
  if (!hover.value || !props.prevClose || !hover.value.point.price) return null
  return (hover.value.point.price / props.prevClose - 1) * 100
})

const toneOf = (value: number | null) => {
  if (value === null || props.prevClose === null) return ''
  return value >= props.prevClose ? 'up' : 'down'
}
const fmt = (v: number | null | undefined) => (v === null || v === undefined ? '—' : Number(v).toFixed(2))
function vol(shares: number) {
  if (!shares) return '—'
  if (shares >= 1e8) return (shares / 1e8).toFixed(2) + '亿股'
  if (shares >= 1e4) return (shares / 1e4).toFixed(0) + '万股'
  return shares + '股'
}
</script>

<style scoped>
.ic { position: relative; }
.ic-legend { display: flex; gap: var(--sp-4); font-size: var(--fs-xs); color: var(--text-muted); margin-bottom: 4px; flex-wrap: wrap; }
.ic-legend i { display: inline-block; width: 10px; height: 3px; border-radius: 2px; margin-right: 5px; vertical-align: middle; }
.lg-price { background: var(--accent-up); }
.lg-avg { background: var(--accent-gold); }
.lg-prev { background: var(--text-dim); }
.ic-body { position: relative; }
.ic-body svg { width: 100%; display: block; background: var(--bg-input); border: 1px solid var(--border-color); border-radius: var(--r-2) var(--r-2) 0 0; }
.ic-scale { position: absolute; top: 0; right: 4px; height: 72%; pointer-events: none; }
.ic-scale span { position: absolute; right: 0; transform: translateY(-50%); font-size: 10px; color: var(--text-dim); font-family: var(--font-mono); }
.ic-tip {
  position: absolute; top: 6px; width: 148px; padding: 6px 8px; border-radius: var(--r-2);
  background: rgba(17, 22, 29, 0.96); border: 1px solid var(--border-color); font-size: var(--fs-xs);
  pointer-events: none; z-index: var(--z-overlay);
}
.tip-time { color: var(--text-muted); margin-bottom: 3px; font-family: var(--font-mono); }
.tip-row { display: flex; justify-content: space-between; gap: 8px; }
.tip-row span { color: var(--text-muted); }
.ic-axis { display: flex; justify-content: space-between; font-size: 10px; color: var(--text-dim); margin-top: 2px; }
</style>
