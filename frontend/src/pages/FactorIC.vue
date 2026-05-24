<template>
  <div class="page">
    <h4 class="page-title">因子 IC 分析</h4>

    <div class="controls">
      <select v-model="factorSet">
        <option value="qlib_alpha158">Qlib Alpha158</option>
        <option value="qlib_alpha360">Qlib Alpha360</option>
      </select>
      <select v-model="horizon">
        <option :value="5">5天</option><option :value="10">10天</option>
        <option :value="20">20天</option><option :value="40">40天</option>
        <option :value="60">60天</option>
      </select>
      <select v-model="topN">
        <option :value="5">Top 5</option><option :value="10">Top 10</option>
        <option :value="15">Top 15</option><option :value="20">Top 20</option>
      </select>
    </div>

    <div v-if="data" class="charts-grid">
      <div class="chart-panel">
        <h6>IC 时间序列</h6>
        <div ref="icChartRef" class="chart-box"></div>
      </div>
      <div class="chart-panel">
        <h6>Rank IC 时间序列</h6>
        <div ref="rankChartRef" class="chart-box"></div>
      </div>
    </div>

    <hr>

    <div class="two-col" v-if="data">
      <div class="col-left">
        <h6>Top 10 因子 (按 IC 均值)</h6>
        <div class="bar-chart">
          <div v-for="f in data.top10" :key="f.factor" class="bar-row">
            <span class="bar-label top-label">{{ f.factor }}</span>
            <div class="bar-track">
              <div class="bar-fill bar-ic" :style="{ width: barPct(f.mean_ic) + '%' }"></div>
            </div>
            <span class="bar-val">{{ f.mean_ic?.toFixed(4) }}</span>
          </div>
        </div>
      </div>
      <div class="col-right">
        <h6>因子 IC 统计汇总</h6>
        <table class="data-table">
          <thead>
            <tr>
              <th @click="sortIc('factor')">因子 {{ icSortKey === 'factor' ? (icSortAsc ? '▲' : '▼') : '' }}</th>
              <th @click="sortIc('mean_ic')">IC均值</th>
              <th @click="sortIc('std_ic')">IC标准差</th>
              <th @click="sortIc('ic_ir')">ICIR</th>
              <th @click="sortIc('mean_rank_ic')">RankIC均值</th>
              <th @click="sortIc('ic_positive_rate')">IC>0占比</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="f in sortedSummary" :key="f.factor">
              <td>{{ f.factor }}</td>
              <td>{{ ff(f.mean_ic) }}</td><td>{{ ff(f.std_ic) }}</td>
              <td>{{ ff(f.ic_ir) }}</td><td>{{ ff(f.mean_rank_ic) }}</td>
              <td>{{ ff(f.ic_positive_rate) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div v-else class="empty-state">加载中...</div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { createChart, ColorType, type IChartApi, type ISeriesApi, type LineData, type Time } from 'lightweight-charts'
import { getFactorIC } from '@/api/client'
import type { FactorICResponse, FactorICSummary } from '@/types/api'

const data = ref<FactorICResponse | null>(null)
const factorSet = ref('qlib_alpha158')
const horizon = ref(20)
const topN = ref(10)

const icChartRef = ref<HTMLDivElement | null>(null)
const rankChartRef = ref<HTMLDivElement | null>(null)
let icChart: IChartApi | null = null
let rankChart: IChartApi | null = null
const icSeriesMap = new Map<string, ISeriesApi<'Line'>>()
const rankSeriesMap = new Map<string, ISeriesApi<'Line'>>()

const LINE_COLORS = [
  '#ef476f', '#ffd166', '#ff9f1c', '#118ab2', '#06d6a0',
  '#ef767a', '#a29bfe', '#55efc4', '#74b9ff', '#fdcb6e',
  '#e17055', '#00cec9', '#6c5ce7', '#f9ca24', '#7bed9f',
  '#e056a0', '#a29bfe', '#ff7979', '#badc58', '#c7ecee',
]

const icSortKey = ref('mean_ic')
const icSortAsc = ref(false)

const sortedSummary = computed(() => {
  if (!data.value) return []
  const rows = [...data.value.summary]
  rows.sort((a, b) => {
    const va = (a as any)[icSortKey.value] ?? 0
    const vb = (b as any)[icSortKey.value] ?? 0
    return icSortAsc.value ? (va > vb ? 1 : -1) : (va < vb ? 1 : -1)
  })
  return rows
})

function createLcChart(container: HTMLDivElement): IChartApi {
  return createChart(container, {
    layout: { background: { type: ColorType.Solid, color: '#11161d' }, textColor: '#a3a8af' },
    grid: { vertLines: { color: 'rgba(42,46,57,0.4)' }, horzLines: { color: 'rgba(42,46,57,0.4)' } },
    rightPriceScale: { borderColor: 'rgba(197,203,206,0.3)' },
    timeScale: { borderColor: 'rgba(197,203,206,0.3)', timeVisible: false },
    crosshair: { vertLine: { color: 'rgba(255,209,102,0.5)', width: 1, style: 2 }, horzLine: { color: 'rgba(255,209,102,0.3)', width: 1, style: 2 } },
    handleScroll: { vertTouchDrag: false },
  })
}

function renderIcChart() {
  if (!data.value || !icChartRef.value) return
  if (icChart) { icChart.remove(); icSeriesMap.clear() }
  icChart = createLcChart(icChartRef.value)
  icChartRef.value.style.height = '350px'

  data.value.factors.forEach((f, i) => {
    const series = icChart!.addLineSeries({ color: LINE_COLORS[i % LINE_COLORS.length], lineWidth: 1.5 })
    const points: LineData[] = []
    const vals = data.value!.ic_series[f] ?? []
    data.value!.dates.forEach((d, j) => {
      const v = vals[j]
      if (v != null) points.push({ time: d as Time, value: v })
    })
    series.setData(points)
    icSeriesMap.set(f, series)
  })
  icChart.timeScale().fitContent()
}

function renderRankChart() {
  if (!data.value || !rankChartRef.value) return
  if (rankChart) { rankChart.remove(); rankSeriesMap.clear() }
  rankChart = createLcChart(rankChartRef.value)
  rankChartRef.value.style.height = '350px'

  data.value.factors.forEach((f, i) => {
    const series = rankChart!.addLineSeries({ color: LINE_COLORS[i % LINE_COLORS.length], lineWidth: 1.5 })
    const points: LineData[] = []
    const vals = data.value!.rank_ic_series[f] ?? []
    data.value!.dates.forEach((d, j) => {
      const v = vals[j]
      if (v != null) points.push({ time: d as Time, value: v })
    })
    series.setData(points)
    rankSeriesMap.set(f, series)
  })
  rankChart.timeScale().fitContent()
}

async function fetch() {
  data.value = await getFactorIC(factorSet.value, horizon.value, topN.value)
  await nextTick()
  renderIcChart()
  renderRankChart()
}

onMounted(fetch)
onUnmounted(() => { icChart?.remove(); rankChart?.remove() })

watch([factorSet, horizon, topN], () => { fetch() })

function barPct(val: number | null): number {
  if (!val || !data.value) return 0
  const max = Math.max(...data.value.top10.map(f => Math.abs(f.mean_ic ?? 0)), 0.01)
  return Math.abs(val) / max * 100
}

function sortIc(key: string) {
  if (icSortKey.value === key) icSortAsc.value = !icSortAsc.value
  else { icSortKey.value = key; icSortAsc.value = false }
}

function ff(v: any): string {
  if (v === null || v === undefined) return '-'
  return Number(v).toFixed(4)
}
</script>

<style scoped>
.page { max-width: 1400px; margin: 0 auto; }
.page-title { margin-bottom: 10px; font-size: 18px; }
.controls { display: flex; gap: 10px; margin-bottom: 12px; }
.controls select { padding: 6px 10px; background: var(--bg-input); color: var(--text-primary); border: 1px solid var(--border-color); border-radius: 4px; }
.charts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.chart-panel h6 { font-size: 12px; color: var(--text-muted); margin-bottom: 4px; }
.chart-box { min-height: 350px; }
.two-col { display: flex; gap: 20px; }
.col-left { width: 380px; flex-shrink: 0; }
.col-right { flex: 1; min-width: 0; }
h6 { font-size: 13px; color: var(--text-muted); margin-bottom: 6px; }
.bar-chart { padding: 4px 0; }
.bar-row { display: flex; align-items: center; gap: 4px; height: 22px; font-size: 11px; }
.bar-label { color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.top-label { width: 100px; text-align: right; }
.bar-track { flex: 1; height: 10px; background: rgba(42,46,57,0.5); border-radius: 2px; }
.bar-fill { height: 100%; border-radius: 2px; min-width: 2px; }
.bar-ic { background: var(--accent-green); }
.bar-val { width: 65px; text-align: left; color: var(--text-muted); }
.data-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.data-table th { background: #1a1a2e; color: var(--text-muted); padding: 6px 8px; text-align: left; cursor: pointer; border-bottom: 1px solid var(--border-color); }
.data-table td { padding: 5px 8px; border-bottom: 1px solid rgba(42,46,57,0.5); }
.data-table tr:hover { background: rgba(255,255,255,0.03); }
hr { border-color: var(--border-color); margin: 16px 0; }
.empty-state { padding: 60px 0; text-align: center; color: var(--text-muted); }
</style>
