<template>
  <div class="chart-wrapper">
    <div ref="containerRef" class="chart-container" :style="{ height: height + 'px' }"></div>
    <div v-if="isLoading" class="chart-loading">加载中...</div>
    <div v-if="error" class="chart-error">{{ error }}</div>
    <div v-if="tooltip.visible" class="kline-tooltip" :style="{ left: tooltip.x + 'px', top: tooltip.y + 'px' }">
      <div class="tt-date">{{ tooltip.date }}</div>
      <div class="tt-row"><span>开</span><span>{{ tooltip.open }}</span></div>
      <div class="tt-row"><span>高</span><span class="tt-high">{{ tooltip.high }}</span></div>
      <div class="tt-row"><span>低</span><span class="tt-low">{{ tooltip.low }}</span></div>
      <div class="tt-row"><span>收</span><span :class="tooltip.chgUp ? 'tt-up' : 'tt-down'">{{ tooltip.close }}</span></div>
      <div class="tt-divider"></div>
      <div class="tt-row"><span>涨跌</span><span :class="tooltip.chgUp ? 'tt-up' : 'tt-down'">{{ tooltip.changePct }}</span></div>
      <div class="tt-row"><span>振幅</span><span>{{ tooltip.amplitude }}</span></div>
      <div class="tt-row"><span>成交量</span><span>{{ tooltip.volume }}</span></div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, watch, onMounted, onUnmounted, nextTick } from 'vue'
import {
  createChart, CrosshairMode, ColorType,
  type IChartApi, type ISeriesApi, type CandlestickData, type LineData, type HistogramData, type Time,
} from 'lightweight-charts'
import type { OhlcvResponse, OhlcvDataPoint, ChartMarker } from '@/types/api'

const props = defineProps<{
  data: OhlcvResponse | null
  isLoading: boolean
  error: string | null
  showSignals: boolean
  height: number
}>()

const emit = defineEmits<{ priceRange: [range: [number, number]] }>()

const containerRef = ref<HTMLElement | null>(null)
let chart: IChartApi | null = null
let candleSeries: ISeriesApi<'Candlestick'> | null = null
let volumeSeries: ISeriesApi<'Histogram'> | null = null
let ma5Series: ISeriesApi<'Line'> | null = null
let ma10Series: ISeriesApi<'Line'> | null = null
let ma20Series: ISeriesApi<'Line'> | null = null
let ma60Series: ISeriesApi<'Line'> | null = null
let volMa5Series: ISeriesApi<'Line'> | null = null
let volMa20Series: ISeriesApi<'Line'> | null = null

let dataMap: Record<string, OhlcvDataPoint> = {}

const COLORS = {
  bg: '#11161d', text: '#a3a8af', grid: 'rgba(42, 46, 57, 0.5)',
  border: 'rgba(197, 203, 206, 0.3)', up: '#ef476f', down: '#00d4aa',
  ma5: '#ffd166', ma10: '#ff9f1c', ma20: '#06d6a0', ma60: '#118ab2',
  crosshair: 'rgba(255, 209, 102, 0.85)', crosshairHz: 'rgba(255, 209, 102, 0.50)',
}

const tooltip = reactive({
  visible: false, x: 0, y: 0,
  date: '', open: '', high: '', low: '', close: '',
  changePct: '', amplitude: '', volume: '', chgUp: true,
})

function fmtNum(v: number | null | undefined): string {
  if (v == null) return '-'
  return v.toFixed(3)
}

function fmtVol(v: number): string {
  if (v >= 1e8) return (v / 1e8).toFixed(2) + '亿'
  if (v >= 1e4) return (v / 1e4).toFixed(0) + '万'
  return v.toFixed(0)
}

function showTooltip(param: any, container: HTMLElement) {
  if (!param.point || !param.time) {
    tooltip.visible = false
    return
  }
  const dp = dataMap[param.time as string]
  if (!dp) { tooltip.visible = false; return }

  const rect = container.getBoundingClientRect()
  const x = param.point.x + 16
  const y = param.point.y - 10
  const maxX = rect.width - 180

  tooltip.visible = true
  tooltip.x = Math.min(x, maxX > 0 ? maxX : x)
  tooltip.y = Math.max(y, 10)
  tooltip.date = dp.time
  tooltip.open = fmtNum(dp.open)
  tooltip.high = fmtNum(dp.high)
  tooltip.low = fmtNum(dp.low)
  tooltip.close = fmtNum(dp.close)
  tooltip.chgUp = (dp.change_pct ?? 0) >= 0
  tooltip.changePct = dp.change_pct != null ? (dp.change_pct >= 0 ? '+' : '') + dp.change_pct.toFixed(2) + '%' : '-'
  tooltip.amplitude = dp.amplitude != null ? dp.amplitude.toFixed(2) + '%' : '-'
  tooltip.volume = fmtVol(dp.volume)
}

function initChart() {
  if (!containerRef.value) return

  chart = createChart(containerRef.value, {
    layout: {
      background: { type: ColorType.Solid, color: COLORS.bg },
      textColor: COLORS.text,
    },
    grid: {
      vertLines: { color: COLORS.grid },
      horzLines: { color: COLORS.grid },
    },
    crosshair: {
      mode: CrosshairMode.Normal,
      vertLine: { color: COLORS.crosshair, width: 1, style: 2, labelVisible: true },
      horzLine: { color: COLORS.crosshairHz, width: 1, style: 2, labelVisible: true },
    },
    rightPriceScale: { borderColor: COLORS.border, entireTextOnly: true },
    timeScale: { borderColor: COLORS.border, timeVisible: true, secondsVisible: false },
    handleScroll: { vertTouchDrag: false },
  })

  candleSeries = chart.addCandlestickSeries({
    upColor: COLORS.up, downColor: COLORS.down,
    borderUpColor: COLORS.up, borderDownColor: COLORS.down,
    wickUpColor: COLORS.up, wickDownColor: COLORS.down,
  })

  ma5Series = chart.addLineSeries({ color: COLORS.ma5, lineWidth: 1, priceLineVisible: false, lastValueVisible: false })
  ma10Series = chart.addLineSeries({ color: COLORS.ma10, lineWidth: 1, priceLineVisible: false, lastValueVisible: false })
  ma20Series = chart.addLineSeries({ color: COLORS.ma20, lineWidth: 1, priceLineVisible: false, lastValueVisible: false })
  ma60Series = chart.addLineSeries({ color: COLORS.ma60, lineWidth: 2, priceLineVisible: false, lastValueVisible: false })

  volumeSeries = chart.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: 'volume' })
  chart.priceScale('volume').applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } })

  volMa5Series = chart.addLineSeries({
    color: COLORS.ma5, lineWidth: 1, lineStyle: 2,
    priceLineVisible: false, lastValueVisible: false, priceScaleId: 'volume',
  })
  volMa20Series = chart.addLineSeries({
    color: COLORS.ma20, lineWidth: 1, lineStyle: 2,
    priceLineVisible: false, lastValueVisible: false, priceScaleId: 'volume',
  })

  chart.timeScale().fitContent()

  chart.subscribeCrosshairMove((param) => {
    if (containerRef.value) showTooltip(param, containerRef.value)
  })

  chart.timeScale().subscribeVisibleTimeRangeChange(() => { updatePriceRange() })
}

function updatePriceRange() {
  if (!candleSeries) return
  const data = candleSeries.data() as CandlestickData[]
  if (!data || data.length === 0) return
  const visibleRange = chart?.timeScale().getVisibleRange()
  if (!visibleRange) return
  let lo = Infinity, hi = -Infinity
  for (const d of data) {
    const t = d.time as string
    if (t >= (visibleRange.from as string) && t <= (visibleRange.to as string)) {
      if (d.low < lo) lo = d.low
      if (d.high > hi) hi = d.high
    }
  }
  if (lo < Infinity && hi > -Infinity) emit('priceRange', [lo * 0.95, hi * 1.05])
}

function setData(resp: OhlcvResponse) {
  if (!chart || !candleSeries || !volumeSeries) return

  dataMap = {}
  for (const d of resp.data) { dataMap[d.time] = d }

  const candles: CandlestickData[] = []
  const volumes: HistogramData[] = []
  const ma5: LineData[] = []; const ma10: LineData[] = []
  const ma20: LineData[] = []; const ma60: LineData[] = []
  const volMa5: LineData[] = []; const volMa20: LineData[] = []
  const markers: ChartMarker[] = []

  for (const d of resp.data) {
    const time = d.time as Time
    candles.push({ time, open: d.open, high: d.high, low: d.low, close: d.close })
    volumes.push({ time, value: d.volume,
      color: d.close >= d.open ? 'rgba(239,71,111,0.5)' : 'rgba(0,212,170,0.5)' })
    if (d.ma5 != null) ma5.push({ time, value: d.ma5 })
    if (d.ma10 != null) ma10.push({ time, value: d.ma10 })
    if (d.ma20 != null) ma20.push({ time, value: d.ma20 })
    if (d.ma60 != null) ma60.push({ time, value: d.ma60 })
    if (d.vol_ma5 != null) volMa5.push({ time, value: d.vol_ma5 })
    if (d.vol_ma20 != null) volMa20.push({ time, value: d.vol_ma20 })
    if (props.showSignals) {
      if (d.buy_signal) markers.push({ time, position: 'belowBar', color: COLORS.up, shape: 'arrowUp', text: '买入', size: 2 })
      if (d.sell_signal) markers.push({ time, position: 'aboveBar', color: COLORS.down, shape: 'arrowDown', text: '卖出', size: 2 })
    }
  }

  candleSeries.setData(candles)
  volumeSeries.setData(volumes)
  ma5Series?.setData(ma5); ma10Series?.setData(ma10)
  ma20Series?.setData(ma20); ma60Series?.setData(ma60)
  volMa5Series?.setData(volMa5); volMa20Series?.setData(volMa20)
  candleSeries.setMarkers(markers)
  chart.timeScale().fitContent()
  nextTick(updatePriceRange)
}

function destroyChart() {
  if (chart) { chart.remove(); chart = null }
  candleSeries = null; volumeSeries = null
  ma5Series = ma10Series = ma20Series = ma60Series = null
  volMa5Series = volMa20Series = null
}

onMounted(() => {
  nextTick(() => { initChart(); if (props.data) setData(props.data) })
})
onUnmounted(() => { destroyChart() })

watch(() => props.data, (newData) => {
  if (!chart) { nextTick(() => { initChart(); if (props.data) setData(props.data) }); return }
  if (newData) setData(newData)
})
watch(() => props.height, (h) => { chart?.applyOptions({ height: h }) })
watch(() => props.showSignals, () => { if (props.data) setData(props.data) })
</script>

<style scoped>
.chart-wrapper { position: relative; flex: 1; min-width: 0; }
.chart-container { width: 100%; }
.chart-loading, .chart-error {
  position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
  font-size: 14px;
}
.chart-loading { color: var(--text-muted); }
.chart-error { color: #ef476f; }

.kline-tooltip {
  position: absolute; z-index: 20;
  background: rgba(20, 24, 32, 0.95);
  border: 1px solid rgba(255, 209, 102, 0.35);
  border-radius: 6px;
  padding: 8px 12px;
  font-size: 12px;
  line-height: 1.7;
  min-width: 155px;
  pointer-events: none;
  box-shadow: 0 4px 16px rgba(0,0,0,0.5);
}
.tt-date {
  font-weight: 700; color: var(--accent-gold);
  margin-bottom: 4px; font-size: 13px;
}
.tt-row {
  display: flex; justify-content: space-between; gap: 12px;
}
.tt-row span:first-child { color: var(--text-muted); }
.tt-row span:last-child { color: var(--text-primary); }
.tt-high { color: var(--accent-up) !important; }
.tt-low { color: var(--accent-down) !important; }
.tt-up { color: var(--accent-up) !important; }
.tt-down { color: var(--accent-down) !important; }
.tt-divider {
  height: 1px; background: rgba(255,255,255,0.08);
  margin: 4px 0;
}
</style>
