<template>
  <div class="turnover-chart" @mouseleave="hideTip">
    <div ref="containerRef" class="tc-container" :style="{ height: height + 'px' }"></div>
    <div v-if="tip.visible" class="tc-tip">
      <span class="mono">{{ tip.date }}</span>
      <span>成交额 <b class="mono">{{ tip.amount }}</b></span>
      <span class="dim">20日均 <b class="mono">{{ tip.ma20 }}</b></span>
    </div>
    <div class="tc-legend">
      <span><i class="dot amount" />每日成交额</span>
      <span><i class="dot ma" />20 日均值</span>
      <span class="dim">单位：亿元</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import {
  ColorType, CrosshairMode, createChart,
  type IChartApi, type ISeriesApi, type Time,
} from 'lightweight-charts'
import type { MarketTurnoverBar } from '@/types/api'

const props = withDefaults(defineProps<{ series: MarketTurnoverBar[]; height?: number }>(), {
  height: 280,
})

const containerRef = ref<HTMLElement | null>(null)
let chart: IChartApi | null = null
let amountSeries: ISeriesApi<'Area'> | null = null
let maSeries: ISeriesApi<'Line'> | null = null
let byDate: Record<string, MarketTurnoverBar> = {}

const tip = reactive({ visible: false, date: '', amount: '—', ma20: '—' })

function money(v: number | null | undefined) {
  return v == null ? '—' : `${v.toLocaleString('zh-CN', { maximumFractionDigits: 1 })} 亿`
}

function init() {
  if (!containerRef.value) return
  chart = createChart(containerRef.value, {
    autoSize: true,
    layout: {
      background: { type: ColorType.Solid, color: '#11161d' },
      textColor: '#a3a8af',
    },
    grid: {
      vertLines: { color: 'rgba(42, 46, 57, 0.5)' },
      horzLines: { color: 'rgba(42, 46, 57, 0.5)' },
    },
    crosshair: {
      mode: CrosshairMode.Normal,
      vertLine: { color: 'rgba(255, 209, 102, 0.85)', width: 1, style: 2, labelVisible: true },
      horzLine: { color: 'rgba(255, 209, 102, 0.5)', width: 1, style: 2, labelVisible: true },
    },
    rightPriceScale: {
      borderColor: 'rgba(197, 203, 206, 0.3)',
      entireTextOnly: true,
    },
    timeScale: {
      borderColor: 'rgba(197, 203, 206, 0.3)',
      timeVisible: false,
      secondsVisible: false,
    },
    localization: {
      priceFormatter: (p: number) => Math.round(p).toLocaleString('zh-CN'),
    },
    handleScroll: { vertTouchDrag: false },
  })

  amountSeries = chart.addAreaSeries({
    lineColor: '#ffd166',
    topColor: 'rgba(255, 209, 102, 0.24)',
    bottomColor: 'rgba(255, 209, 102, 0.02)',
    lineWidth: 2,
    priceLineVisible: false,
    priceFormat: { type: 'price', precision: 0, minMove: 1 },
  })
  maSeries = chart.addLineSeries({
    color: '#06d6a0',
    lineWidth: 1,
    priceLineVisible: false,
    lastValueVisible: false,
    lineStyle: 2,
  })

  chart.subscribeCrosshairMove((param) => {
    if (!param.time || !param.point) { hideTip(); return }
    const row = byDate[String(param.time)]
    if (!row) { hideTip(); return }
    tip.visible = true
    tip.date = row.date || ''
    tip.amount = money(row.amount_yi)
    tip.ma20 = money(row.ma20)
  })
}

function setData() {
  if (!chart || !amountSeries || !maSeries) return
  hideTip()
  byDate = Object.fromEntries(props.series.map((x) => [x.date || '', x]))
  const amount = props.series
    .filter((x) => x.date && x.amount_yi != null)
    .map((x) => ({ time: x.date as Time, value: Number(x.amount_yi) }))
  const ma = props.series
    .filter((x) => x.date && x.ma20 != null)
    .map((x) => ({ time: x.date as Time, value: Number(x.ma20) }))
  amountSeries.setData(amount)
  maSeries.setData(ma)
  chart.timeScale().fitContent()
}

function hideTip() { tip.visible = false }

onMounted(() => {
  init()
  setData()
})
onUnmounted(() => {
  chart?.remove()
  chart = null
  amountSeries = null
  maSeries = null
})
watch(() => props.series, setData, { deep: true })
watch(() => props.height, (h) => { chart?.applyOptions({ height: h }) })
</script>

<style scoped>
.turnover-chart { position: relative; width: 100%; }
.tc-container { width: 100%; }
.tc-tip {
  position: absolute; z-index: 10; top: 8px; left: 10px;
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 4px 9px; border-radius: 6px;
  background: rgba(20, 24, 32, 0.92);
  border: 1px solid rgba(255, 209, 102, 0.28);
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.4);
  font-size: 12px; pointer-events: none;
}
.tc-tip b { color: var(--accent-gold); }
.tc-legend {
  display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
  font-size: var(--fs-xs); color: var(--text-muted); margin-top: 4px;
}
.tc-legend span { display: inline-flex; align-items: center; gap: 5px; }
.dot { width: 10px; height: 3px; border-radius: 2px; display: inline-block; }
.dot.amount { background: #ffd166; }
.dot.ma { background: #06d6a0; }
</style>
