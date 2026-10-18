<!--
  行业热度动态曲线（2026-10-18 最终版）。

  结论：3D 排名折线 / 3D 地形在"行业一多"时都不可读——遮挡、颜色趋同、标签重叠。
  改成最符合人类习惯的 **时间 × 热度 多线图**：
    X = 时间，Y = 热度（0~100），每条线一个行业（默认前 10），颜色区分行业；
    悬停竖线显示当天热度前 5（含排名），播放按时间推进。
  行业顺序/排名仍保留在右侧标签（#平均排名）和悬停明细里。
-->
<template>
  <div class="heat3d" ref="wrapRef" @mouseleave="hover = null">
    <div class="h3d-toolbar">
      <button class="btn-ghost btn-sm" @click="togglePlay">{{ playing ? '⏸ 暂停' : '▶ 播放' }}</button>
      <input v-model.number="active" type="range" min="0" :max="Math.max(0, dates.length - 1)" />
      <span class="mono tiny">{{ dates[active] || '—' }}</span>
      <span class="dim tiny">显示</span>
      <select v-model.number="visibleTop" class="h3d-select">
        <option :value="8">当前前 8 行业</option>
        <option :value="12">当前前 12 行业</option>
        <option :value="16">当前前 16 行业</option>
      </select>
      <span class="dim tiny">Y = 热度 0~100 · 颜色区分行业</span>
    </div>

    <svg :viewBox="`0 0 ${W} ${H}`" class="h3d-svg" @mousemove="onMove">
      <!-- Y 轴网格 -->
      <g class="h3d-grid">
        <line v-for="v in heatTicks" :key="'h' + v" :x1="padL" :y1="yOf(v)" :x2="W - padR" :y2="yOf(v)" />
        <line v-for="i in timeTicks" :key="'t' + i" :x1="xOf(i)" :y1="padT" :x2="xOf(i)" :y2="H - padB" />
      </g>

      <!-- 行业曲线 -->
      <g>
        <path v-for="(industry, j) in industries" :key="industry" :d="linePath(j)"
              fill="none" :stroke="palette(j)" :stroke-width="hover?.j === j ? 2.6 : 1.6"
              :opacity="hover && hover.j !== j ? 0.35 : 0.95" />
      </g>

      <!-- 行业标签（右侧，按最后一天热度上下避让） -->
      <g class="h3d-labels">
        <text v-for="lab in labels" :key="lab.j" :x="W - padR + 4" :y="lab.y"
              :fill="palette(lab.j)" :opacity="hover && hover.j !== lab.j ? 0.45 : 1">
          #{{ lab.rank }} {{ lab.industry }}
        </text>
      </g>

      <!-- 当前日期 -->
      <line class="h3d-active" :x1="xOf(active)" :y1="padT" :x2="xOf(active)" :y2="H - padB" />
      <text class="h3d-active-label" :x="xOf(active)" :y="padT - 8" text-anchor="middle">{{ dates[active] }}</text>

      <!-- 轴 -->
      <g class="h3d-axis">
        <text v-for="v in heatTicks" :key="'hy' + v" :x="padL - 8" :y="yOf(v) + 3" text-anchor="end">{{ v }}</text>
        <text v-for="i in timeTicks" :key="'tx' + i" :x="xOf(i)" :y="H - padB + 16" text-anchor="middle">{{ dates[i]?.slice(5) }}</text>
        <text :x="(padL + W - padR) / 2" :y="H - 6" text-anchor="middle" class="axis-title">时间</text>
        <text :x="padL - 8" :y="padT - 10" text-anchor="end" class="axis-title">热度</text>
      </g>
    </svg>

    <div v-if="hover" class="h3d-tip" :style="tipStyle">
      <div class="tip-date">{{ dates[hover.i] }}</div>
      <div v-for="row in hoverRows" :key="row.industry" class="tip-row">
        <span class="dot" :style="{ background: palette(row.j) }" />
        <span class="tip-name">#{{ row.rank }} {{ row.industry }}</span>
        <b class="tip-heat">{{ row.heat.toFixed(1) }}</b>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import type { IndustryHeatHistoryResponse } from '@/types/api'

const props = withDefaults(defineProps<{ data: IndustryHeatHistoryResponse; height?: number }>(), { height: 580 })
const wrapRef = ref<HTMLElement | null>(null)
const active = ref(0)
const playing = ref(false)
const visibleTop = ref(8)
const hover = ref<{ i: number; j: number | null } | null>(null)
let timer: ReturnType<typeof setInterval> | null = null

const W = 1000
const H = computed(() => props.height)
const padL = 46, padR = 178, padT = 30, padB = 42
const dates = computed(() => props.data.dates || [])
const industries = computed(() => (props.data.industries || []).slice(0, visibleTop.value))
const heats = computed(() => (props.data.heats || []).slice(0, visibleTop.value))
const ranks = computed(() => (props.data.ranks || []).slice(0, visibleTop.value))
const avgRanks = computed(() => industries.value.map((_, j) => {
  const vals = (ranks.value[j] || []).filter((x): x is number => x != null)
  return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : 999
}))
const heatTicks = [0, 25, 50, 75, 100]
const timeTicks = computed(() => {
  const n = dates.value.length
  if (n <= 1) return [0]
  const step = Math.max(1, Math.ceil(n / 8))
  const out: number[] = []
  for (let i = 0; i < n; i += step) out.push(i)
  if (out[out.length - 1] !== n - 1) out.push(n - 1)
  return out
})

function xOf(i: number) { return padL + (dates.value.length > 1 ? i / (dates.value.length - 1) : 0) * (W - padL - padR) }
function yOf(heat: number) { return padT + (1 - Math.max(0, Math.min(100, heat)) / 100) * (H.value - padT - padB) }
function palette(j: number) { return `hsl(${(j * 137.508) % 360} 72% 62%)` }

function linePath(j: number) {
  const pts = dates.value.map((_, i) => ({ x: xOf(i), y: yOf(heats.value[j]?.[i] ?? 0) }))
  if (!pts.length) return ''
  let d = `M ${pts[0].x.toFixed(1)} ${pts[0].y.toFixed(1)}`
  for (let i = 1; i < pts.length; i++) {
    const p = pts[i - 1], q = pts[i]
    const mx = ((p.x + q.x) / 2).toFixed(1), my = ((p.y + q.y) / 2).toFixed(1)
    d += ` Q ${p.x.toFixed(1)} ${p.y.toFixed(1)} ${mx} ${my}`
  }
  const last = pts[pts.length - 1]
  d += ` L ${last.x.toFixed(1)} ${last.y.toFixed(1)}`
  return d
}

const labels = computed(() => {
  const raw = industries.value.map((industry, j) => ({
    industry, j,
    // 标签用**最新交易日**的排名（与热度榜一致）；历史曲线仍保留
    rank: (ranks.value[j]?.[dates.value.length - 1] ?? avgRanks.value[j]).toString(),
    y: yOf(heats.value[j]?.[dates.value.length - 1] ?? 0),
  })).sort((a, b) => a.y - b.y)
  // 简单避让：相邻标签至少 12px
  for (let i = 1; i < raw.length; i++) if (raw[i].y - raw[i - 1].y < 12) raw[i].y = raw[i - 1].y + 12
  return raw
})

const hoverRows = computed(() => {
  if (!hover.value) return []
  const i = hover.value.i
  return industries.value.map((industry, j) => ({
    industry, j, heat: heats.value[j]?.[i] ?? 0, rank: ranks.value[j]?.[i] ?? '—',
  })).sort((a, b) => b.heat - a.heat).slice(0, 6)
})

const tipStyle = computed(() => {
  if (!hover.value) return {}
  return { left: `${(xOf(hover.value.i) / W) * 100}%`, top: `${(padT / H.value) * 100}%` }
})

function onMove(event: MouseEvent) {
  const wrap = wrapRef.value
  if (!wrap) return
  const rect = wrap.getBoundingClientRect()
  const px = ((event.clientX - rect.left) / rect.width) * W
  const py = ((event.clientY - rect.top) / rect.height) * H.value
  if (px < padL || px > W - padR || py < padT || py > H.value - padB) { hover.value = null; return }
  const n = dates.value.length
  const i = Math.max(0, Math.min(n - 1, Math.round(((px - padL) / (W - padL - padR)) * (n - 1))))
  // 最近的一条曲线（按像素距离）高亮；其余变淡
  let bestJ: number | null = null
  let bestDist = Infinity
  for (let j = 0; j < industries.value.length; j++) {
    const heat = heats.value[j]?.[i]
    if (heat == null) continue
    const d = Math.abs(yOf(heat) - py)
    if (d < bestDist) { bestDist = d; bestJ = j }
  }
  hover.value = { i, j: bestJ }
}

function togglePlay() {
  playing.value = !playing.value
  if (timer) { clearInterval(timer); timer = null }
  if (playing.value) {
    timer = setInterval(() => {
      active.value = (active.value + 1) % Math.max(1, dates.value.length)
    }, 650)
  }
}

watch(() => props.data, () => { active.value = Math.max(0, dates.value.length - 1) }, { immediate: true })
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<style scoped>
.heat3d { position: relative; }
.h3d-toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 4px; }
.h3d-toolbar input[type='range'] { width: 140px; }
.h3d-select { background: var(--bg-input); border: 1px solid var(--border-color); color: var(--text-primary);
  border-radius: 4px; font-size: 11px; padding: 1px 4px; }
.h3d-svg { width: 100%; height: auto; display: block; }
.h3d-grid line { stroke: rgba(255, 255, 255, 0.07); stroke-width: 1; }
.h3d-labels text { font-size: 9px; }
.h3d-axis text { fill: var(--text-muted); font-size: 10px; }
.h3d-axis .axis-title { fill: var(--text-secondary); font-size: 11px; font-weight: 600; }
.h3d-active { stroke: var(--accent-gold); stroke-width: 1.2; stroke-dasharray: 4 3; }
.h3d-active-label { fill: var(--accent-gold); font-size: 10px; }
.h3d-tip {
  position: absolute; transform: translate(10px, 0); pointer-events: none; z-index: 8;
  background: rgba(20, 24, 32, 0.96); border: 1px solid var(--border-color);
  border-radius: 5px; padding: 6px 8px; font-size: 11px; line-height: 1.5; white-space: nowrap;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.45);
}
.tip-date { color: var(--text-muted); margin-bottom: 2px; }
.tip-row { display: flex; align-items: center; gap: 5px; }
.tip-row .dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }
.tip-name { color: var(--text-secondary); }
.tip-heat { margin-left: auto; color: var(--text-primary); }
</style>
