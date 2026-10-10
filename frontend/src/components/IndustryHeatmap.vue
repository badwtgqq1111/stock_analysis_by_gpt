<template>
  <div ref="wrapRef" class="heatmap-wrap">
    <div class="heatmap-bar">
      <button v-if="drill" class="btn-ghost btn-sm" @click="back">← 全部行业</button>
      <span class="crumb">
        <b>{{ drill || '全部行业' }}</b>
        <span class="dim tiny"> {{ cells.length }} 个格子</span>
      </span>
      <span class="tiny dim legend-text">
        面积 = 流通市值 · 颜色 = {{ periodLabel }}涨跌（红涨绿跌）· 点行业下钻、点个股进 K 线
      </span>
    </div>

    <canvas ref="canvasRef" class="heatmap-canvas"
            @mousemove="onMove" @mouseleave="onLeave" @click="onClick" />

    <div v-if="tip" class="heatmap-tip" :style="{ left: tip.x + 'px', top: tip.y + 'px' }">
      <div class="tip-title">{{ tip.title }}</div>
      <div v-for="line in tip.lines" :key="line" class="tip-line">{{ line }}</div>
      <div class="tip-hint tiny dim">{{ tip.hint }}</div>
    </div>

    <div v-if="!industries.length" class="empty">没有热力图数据</div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { IndustryHeatmapIndustry, IndustryHeatmapStock } from '@/types/api'
import { hitTest, squarify } from '@/lib/treemap'

const props = withDefaults(defineProps<{
  industries: IndustryHeatmapIndustry[]
  period?: 'daily' | 'weekly' | 'monthly'
  periodLabel?: string
  selected?: string
  height?: number
}>(), { period: 'daily', periodLabel: '当日', selected: '', height: 620 })

const emit = defineEmits<{
  select: [industry: string]
  open: [code: string]
}>()

const wrapRef = ref<HTMLElement | null>(null)
const canvasRef = ref<HTMLCanvasElement | null>(null)
const drill = ref('')
const hoverKey = ref('')

interface Tip { x: number; y: number; title: string; lines: string[]; hint: string }
const tip = ref<Tip | null>(null)

/** 每期用不同的颜色饱和刻度：日 ±5%、周 ±10%、月 ±20% 打满 */
const RET_SCALE: Record<string, number> = { daily: 0.05, weekly: 0.10, monthly: 0.20 }

// ── 格式化助手：必须是**函数声明**并放在 computeds 之前 ─────────────────────
// 踩坑（2026-10-10 实测）：下面 `cells` 这个 computed 被 `watch(cells, ...)` 引用，
// 而 `watch` 为了拿 oldValue 会在 **setup 阶段同步跑一次 getter** —— 如果助手写成
// 文件末尾的 `const fmtPct = () => ...`，就会撞上 TDZ：
// `ReferenceError: Cannot access 'E' before initialization`，整个热力图组件渲染不出来。
const fmt = (v: number | null | undefined, nd = 2) =>
  v === null || v === undefined || Number.isNaN(Number(v)) ? '—' : Number(v).toFixed(nd)

function fmtPct(v: number | null | undefined) {
  return v === null || v === undefined || Number.isNaN(Number(v))
    ? '—'
    : `${Number(v) >= 0 ? '+' : ''}${(Number(v) * 100).toFixed(2)}%`
}

const CN_NUM = ['零', '一', '两', '三', '四', '五', '六', '七', '八', '九', '十']

function cn(n: number) {
  return CN_NUM[n] || String(n)
}

interface Cell {
  key: string
  rect: { x: number; y: number; w: number; h: number }
  label: string
  sub: string
  ret: number | null
  kind: 'industry' | 'stock'
  industry: string
  code?: string
  stock?: IndustryHeatmapStock
  mktcap: number | null
  n?: number
}

const drillIndustry = computed(() =>
  props.industries.find((item) => item.industry === drill.value) || null,
)

/** 画布尺寸（CSS px）。resize / 下钻都会改它，`cells` 依赖它做布局。 */
const canvasSize = ref({ w: 0, h: props.height })

/** 当前层要铺的格子（0 层 = 行业，1 层 = 该行业成分股）。 */
const cells = computed<Cell[]>(() => {
  const bounds = { x: 0, y: 0, w: canvasSize.value.w, h: canvasSize.value.h }
  if (!drill.value) {
    const items = props.industries.map((item) => ({
      value: Math.max(item.mktcap_yi ?? 0, item.n || 1),
      data: item,
    }))
    return squarify(items, bounds).map((cell) => ({
      key: `i:${cell.data.industry}`,
      rect: cell.rect,
      label: cell.data.industry,
      sub: fmtPct(cell.data.ret),
      ret: cell.data.ret,
      kind: 'industry' as const,
      industry: cell.data.industry,
      mktcap: cell.data.mktcap_yi,
      n: cell.data.n,
    }))
  }
  const target = drillIndustry.value
  if (!target) return []
  const items = target.stocks.map((stock) => ({ value: Math.max(stock.mktcap_yi ?? 0, 0.5), data: stock }))
  return squarify(items, bounds).map((cell) => ({
    key: `s:${cell.data.stock_code}`,
    rect: cell.rect,
    label: cell.data.name || cell.data.stock_code,
    sub: `${fmtPct(cell.data.ret)}${cell.data.boards >= 2 ? ` ${cn(cell.data.boards)}板` : ''}`,
    ret: cell.data.ret,
    kind: 'stock' as const,
    industry: target.industry,
    code: cell.data.stock_code,
    stock: cell.data,
    mktcap: cell.data.mktcap_yi,
  }))
})

// ── 颜色：红涨绿跌（A 股口径），中性底 + 按 |涨跌幅| 加深 ──────────────────
const UP: [number, number, number] = [239, 71, 111]
const DOWN: [number, number, number] = [0, 212, 170]
const BASE: [number, number, number] = [30, 36, 45]

function retColor(ret: number | null): string {
  if (ret === null || ret === undefined || Number.isNaN(Number(ret))) return 'rgb(38,43,52)'
  const scale = RET_SCALE[props.period] ?? 0.05
  const ratio = Math.max(-1, Math.min(1, Number(ret) / scale))
  const target = ratio >= 0 ? UP : DOWN
  const k = Math.pow(Math.abs(ratio), 0.65)
  const rgb = BASE.map((base, i) => Math.round(base + (target[i] - base) * k))
  return `rgb(${rgb[0]},${rgb[1]},${rgb[2]})`
}

function draw() {
  const canvas = canvasRef.value
  const wrap = wrapRef.value
  if (!canvas || !wrap) return
  const width = Math.max(320, wrap.clientWidth)
  const height = props.height
  // 只在尺寸真的变了时才写 ref：否则每次 draw 都换一个新对象 → cells 失效 → 无限重绘
  if (canvasSize.value.w !== width || canvasSize.value.h !== height) {
    canvasSize.value = { w: width, h: height }
  }
  const dpr = window.devicePixelRatio || 1
  canvas.width = Math.round(width * dpr)
  canvas.height = Math.round(height * dpr)
  canvas.style.width = `${width}px`
  canvas.style.height = `${height}px`
  const ctx = canvas.getContext('2d')
  if (!ctx) return
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.clearRect(0, 0, width, height)

  const gap = 1
  for (const cell of cells.value) {
    const { x, y, w, h } = cell.rect
    if (w <= 0 || h <= 0) continue
    const active = cell.key === hoverKey.value
    ctx.fillStyle = retColor(cell.ret)
    ctx.fillRect(x + gap / 2, y + gap / 2, Math.max(0, w - gap), Math.max(0, h - gap))

    if (cell.kind === 'industry' && cell.industry === props.selected) {
      ctx.strokeStyle = '#ffd166'
      ctx.lineWidth = 2
      ctx.strokeRect(x + 1.5, y + 1.5, Math.max(0, w - 3), Math.max(0, h - 3))
    }
    if (active) {
      ctx.strokeStyle = 'rgba(255,255,255,0.85)'
      ctx.lineWidth = 1.5
      ctx.strokeRect(x + 1, y + 1, Math.max(0, w - 2), Math.max(0, h - 2))
    }

    const minW = cell.kind === 'industry' ? 46 : 40
    const minH = cell.kind === 'industry' ? 30 : 26
    if (w < minW || h < minH) continue

    ctx.save()
    ctx.beginPath()
    ctx.rect(x, y, w, h)
    ctx.clip()
    const pad = 4
    ctx.fillStyle = 'rgba(255,255,255,0.95)'
    ctx.font = `${cell.kind === 'industry' ? '700 12px' : '600 11px'} -apple-system, "PingFang SC", sans-serif`
    ctx.textBaseline = 'top'
    ctx.fillText(truncate(ctx, cell.label, w - pad * 2), x + pad, y + pad)
    ctx.fillStyle = 'rgba(255,255,255,0.78)'
    ctx.font = '600 10px ui-monospace, Menlo, monospace'
    ctx.fillText(truncate(ctx, cell.sub, w - pad * 2), x + pad, y + pad + 14)
    ctx.restore()
  }
}

function truncate(ctx: CanvasRenderingContext2D, text: string, maxWidth: number): string {
  if (maxWidth <= 6) return ''
  if (ctx.measureText(text).width <= maxWidth) return text
  let out = text
  while (out.length > 1 && ctx.measureText(`${out}…`).width > maxWidth) out = out.slice(0, -1)
  return `${out}…`
}

function locate(event: MouseEvent): { x: number; y: number } | null {
  const canvas = canvasRef.value
  if (!canvas) return null
  const box = canvas.getBoundingClientRect()
  return { x: event.clientX - box.left, y: event.clientY - box.top }
}

function onMove(event: MouseEvent) {
  const point = locate(event)
  if (!point) return
  const found = hitTest(cells.value, point.x, point.y)
  hoverKey.value = found?.key ?? ''
  if (!found) { tip.value = null; return }
  const lines: string[] = []
  if (found.kind === 'industry') {
    lines.push(`成分 ${found.n ?? '—'} 只 · 市值 ${fmt(found.mktcap, 0)} 亿`)
    lines.push(`区间涨跌 ${fmtPct(found.ret)}`)
  } else {
    lines.push(`${found.code}`)
    lines.push(`收盘 ${fmt(found.stock?.close ?? null, 2)} · 市值 ${fmt(found.mktcap, 0)} 亿`)
    lines.push(`区间涨跌 ${fmtPct(found.ret)} · 当日 ${fmtPct(found.stock?.pct_chg ?? null)}`)
  }
  tip.value = {
    x: Math.min(point.x + 14, Math.max(0, canvasSize.value.w - 240)),
    y: Math.max(4, point.y - 8),
    title: found.label,
    lines,
    hint: found.kind === 'industry' ? '点击下钻看成分股' : '点击进入 K 线',
  }
}

function onLeave() {
  hoverKey.value = ''
  tip.value = null
}

function onClick(event: MouseEvent) {
  const point = locate(event)
  if (!point) return
  const found = hitTest(cells.value, point.x, point.y)
  if (!found) return
  if (found.kind === 'industry') {
    drill.value = found.industry
    emit('select', found.industry)
  } else if (found.code) {
    emit('open', found.code)
  }
}

function back() {
  drill.value = ''
  hoverKey.value = ''
  tip.value = null
}

// 父级切换行业（例如点热度网格）时，热力图跟着下钻，保持两处视图一致
watch(() => props.selected, (value) => {
  if (value && drill.value && value !== drill.value) drill.value = value
})

watch(() => props.industries, () => { draw() }, { deep: false })
watch([cells, () => props.selected], () => { draw() })

let observer: ResizeObserver | null = null
onMounted(() => {
  observer = new ResizeObserver(() => draw())
  if (wrapRef.value) observer.observe(wrapRef.value)
  draw()
})
onBeforeUnmount(() => { observer?.disconnect() })
</script>

<style scoped>
.heatmap-wrap { position: relative; }
.heatmap-bar { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; margin-bottom: var(--sp-2); }
.crumb { font-size: var(--fs-sm); }
.legend-text { margin-left: auto; }
.heatmap-canvas { display: block; width: 100%; border: 1px solid var(--border-color);
  border-radius: var(--r-3); background: var(--bg-input); cursor: pointer; }
.heatmap-tip {
  position: absolute; z-index: var(--z-overlay); pointer-events: none;
  min-width: 170px; padding: 6px 8px; border-radius: var(--r-2);
  background: rgba(6, 6, 6, 0.94); border: 1px solid var(--border-color);
  box-shadow: var(--shadow-2); font-size: var(--fs-xs); line-height: 1.5;
}
.tip-title { font-weight: 700; margin-bottom: 2px; }
.tip-line { font-family: var(--font-mono); color: var(--text-muted); }
.tip-hint { margin-top: 3px; }
.empty { padding: var(--sp-5); text-align: center; color: var(--text-muted); }
</style>
