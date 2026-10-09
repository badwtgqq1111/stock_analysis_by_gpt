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
  createChart, ColorType, type IChartApi, type LineData, type LineWidth, type Time,
} from 'lightweight-charts'
import { getPortfolio } from '@/api/client'
import type { PortfolioResponse } from '@/types/api'

// 同 FactorIC.vue：LineWidth 类型只允许 1|2|3|4，运行时按 1.5px 渲染，显式放宽类型。
const LINE_WIDTH = 1.5 as unknown as LineWidth

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
      lineWidth: LINE_WIDTH,
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
/* 页面专属：净值/回撤图容器（指标卡已上移到设计系统） */
.chart-left { flex: 1; min-width: 0; }
.chart-box { min-height: 180px; }
</style>
