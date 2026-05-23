<template>
  <div class="page">
    <h4 class="page-title">组合回测结果</h4>
    <p class="desc">{{ data?.description || 'LightGBM Top10 等权组合的模拟回测结果' }}</p>

    <div class="metric-cards" v-if="data">
      <div v-for="(val, key) in data.metrics" :key="key" class="metric-card" :class="cardClass(key)">
        <div class="metric-title">{{ key }}</div>
        <div class="metric-value">{{ val }}</div>
      </div>
    </div>

    <div v-if="data" class="chart-row">
      <div class="chart-left">
        <h6>净值曲线</h6>
        <div ref="equityRef" class="chart-box"></div>
        <h6>回撤 (%)</h6>
        <div ref="drawdownRef" class="chart-box" style="height:180px"></div>
      </div>
    </div>

    <hr v-if="data">

    <div v-if="data && data.holdings.length > 0">
      <h6>当前持仓</h6>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="col in holdingCols" :key="col">{{ col }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, i) in data.holdings" :key="i">
            <td v-for="col in holdingCols" :key="col" :class="cellClass(row[col])">
              {{ fmtStr(row[col]) }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-else class="empty-state">加载中...</div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, nextTick } from 'vue'
import {
  createChart, ColorType, type IChartApi, type LineData, type Time,
} from 'lightweight-charts'
import { getPortfolio } from '@/api/client'
import type { PortfolioResponse } from '@/types/api'

const data = ref<PortfolioResponse | null>(null)
const equityRef = ref<HTMLDivElement | null>(null)
const drawdownRef = ref<HTMLDivElement | null>(null)
let equityChart: IChartApi | null = null
let drawdownChart: IChartApi | null = null

const COLORS = {
  equity: '#ef476f',
  drawdown: '#00d4aa',
}

function createLc(container: HTMLDivElement, height: number): IChartApi {
  return createChart(container, {
    layout: { background: { type: ColorType.Solid, color: '#11161d' }, textColor: '#a3a8af' },
    grid: { vertLines: { color: 'rgba(42,46,57,0.4)' }, horzLines: { color: 'rgba(42,46,57,0.4)' } },
    rightPriceScale: { borderColor: 'rgba(197,203,206,0.3)', visible: true },
    timeScale: { borderColor: 'rgba(197,203,206,0.3)', timeVisible: false },
    crosshair: { vertLine: { color: 'rgba(255,209,102,0.5)', width: 1, style: 2 }, horzLine: { color: 'rgba(255,209,102,0.3)', width: 1, style: 2 } },
    handleScroll: { vertTouchDrag: false },
    height,
  })
}

function renderCharts() {
  if (!data.value) return

  if (equityChart) equityChart.remove()
  if (equityRef.value) {
    equityRef.value.style.height = '320px'
    equityChart = createLc(equityRef.value, 320)
    const es = equityChart.addAreaSeries({
      lineColor: COLORS.equity, topColor: 'rgba(239,71,111,0.2)', bottomColor: 'rgba(239,71,111,0.02)',
      lineWidth: 2,
    })
    const pts: LineData[] = data.value.dates.map((d, i) => ({
      time: d as Time, value: data.value!.equity[i],
    }))
    es.setData(pts)
    equityChart.timeScale().fitContent()
  }

  if (drawdownChart) drawdownChart.remove()
  if (drawdownRef.value) {
    drawdownChart = createLc(drawdownRef.value, 180)
    const ds = drawdownChart.addAreaSeries({
      lineColor: COLORS.drawdown, topColor: 'rgba(0,212,170,0.15)', bottomColor: 'rgba(0,212,170,0.02)',
      lineWidth: 1.5,
    })
    const pts: LineData[] = data.value.dates.map((d, i) => ({
      time: d as Time, value: data.value!.drawdown[i],
    }))
    ds.setData(pts)
    drawdownChart.timeScale().fitContent()
  }
}

onMounted(async () => {
  data.value = await getPortfolio()
  await nextTick()
  renderCharts()
})

onUnmounted(() => { equityChart?.remove(); drawdownChart?.remove() })

const holdingCols = computed(() => {
  if (!data.value || !data.value.holdings.length) return []
  return Object.keys(data.value.holdings[0])
})

function cardClass(key: string): string {
  if (key.includes('回撤')) return 'card-danger'
  if (key.includes('收益') || key.includes('率') && !key.includes('回撤')) return 'card-success'
  if (key.includes('夏普')) return 'card-primary'
  return 'card-warning'
}

function cellClass(val: any): string {
  const s = String(val ?? '')
  if (s.includes('+')) return 'up'
  if (s.includes('-')) return 'down'
  return ''
}

function fmtStr(v: any): string {
  if (v === null || v === undefined) return '-'
  if (typeof v === 'number') return v.toFixed(2)
  return String(v)
}
</script>

<style scoped>
.page { max-width: 1400px; margin: 0 auto; }
.page-title { margin-bottom: 4px; font-size: 18px; }
.desc { font-size: 12px; color: var(--text-muted); margin-bottom: 12px; }
.metric-cards { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }
.metric-card { flex: 1; min-width: 140px; padding: 14px 16px; border-radius: 8px; background: var(--bg-card); border: 1px solid var(--border-color); text-align: center; }
.metric-title { font-size: 11px; color: var(--text-muted); margin-bottom: 4px; }
.metric-value { font-size: 20px; font-weight: 700; }
.card-success .metric-value { color: var(--accent-up); }
.card-danger .metric-value { color: var(--accent-down); }
.card-primary .metric-value { color: var(--accent-blue); }
.card-warning .metric-value { color: var(--accent-gold); }
.chart-row { margin-bottom: 12px; }
.chart-left { }
.chart-box { min-height: 180px; }
h6 { font-size: 12px; color: var(--text-muted); margin: 8px 0 4px; }
.data-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.data-table th { background: #1a1a2e; color: var(--text-muted); padding: 6px 8px; text-align: left; border-bottom: 1px solid var(--border-color); }
.data-table td { padding: 5px 8px; border-bottom: 1px solid rgba(42,46,57,0.5); }
.data-table tr:hover { background: rgba(255,255,255,0.03); }
td.up { color: var(--accent-up); }
td.down { color: var(--accent-down); }
hr { border-color: var(--border-color); margin: 16px 0; }
.empty-state { padding: 60px 0; text-align: center; color: var(--text-muted); }
</style>
