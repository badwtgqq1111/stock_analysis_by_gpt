<template>
  <div class="page">
    <h4 class="page-title">K线图表</h4>
    <ControlPanel
      :selected-code="selectedCode"
      :days="days"
      :overlays="overlays"
      @update:selected-code="onCode"
      @update:days="onDays"
      @update:overlays="onOverlays"
    />
    <div class="title-bar" v-if="data?.latest">
      <b>{{ data.code }} {{ data.name }}</b>
      &nbsp;收盘 {{ data.latest.close.toFixed(3) }}
      <span :class="data.latest.change_pct >= 0 ? 'up' : 'down'">
        &nbsp;{{ data.latest.change_pct >= 0 ? '+' : '' }}{{ data.latest.change_pct.toFixed(2) }}%
      </span>
      <span class="bar-count">({{ data.latest.total_bars }}根K线)</span>
    </div>
    <div class="chart-area" v-if="data && data.data.length > 0">
      <div class="chart-row">
        <KlineChart
          :data="data" :is-loading="isLoading" :error="error"
          :show-signals="overlays.includes('signals')" :height="600"
          @price-range="onPriceRange"
        />
        <ChipDistribution
          v-if="overlays.includes('chips') && data.chips"
          :chips="data.chips" :height="600" :price-range="priceRange"
        />
      </div>
    </div>
    <div v-else-if="!selectedCode" class="empty-state">请选择股票代码</div>
    <div v-else-if="isLoading" class="empty-state">加载中...</div>
    <div v-else class="empty-state">暂无行情数据</div>
    <SignalStats :stats="data?.signal_stats ?? null" />
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import ControlPanel from '@/components/ControlPanel.vue'
import KlineChart from '@/components/KlineChart.vue'
import ChipDistribution from '@/components/ChipDistribution.vue'
import SignalStats from '@/components/SignalStats.vue'
import { useKlineData } from '@/composables/useKlineData'

const { data, isLoading, error, stockCode, days, showSignals, showChips } = useKlineData()
const selectedCode = ref<string | null>(null)
const daysModel = ref(365)
const overlays = ref<string[]>(['signals', 'chips'])
const priceRange = ref<[number, number]>([0, 100])

watch(selectedCode, v => { stockCode.value = v })
watch(daysModel, v => { days.value = v })
watch(overlays, v => { showSignals.value = v.includes('signals'); showChips.value = v.includes('chips') })

function onCode(v: string) { selectedCode.value = v }
function onDays(v: number) { daysModel.value = v }
function onOverlays(v: string[]) { overlays.value = v }
function onPriceRange(r: [number, number]) { priceRange.value = r }
</script>

<style scoped>
.page { max-width: 1400px; margin: 0 auto; }
.page-title { margin-bottom: 10px; font-size: 18px; }
.title-bar { font-size: 13px; margin-bottom: 6px; color: var(--text-muted); }
.title-bar b { color: var(--text-primary); }
.title-bar .up { color: var(--accent-up); }
.title-bar .down { color: var(--accent-down); }
.bar-count { color: #666; font-size: 11px; }
.chart-area { margin-bottom: 10px; }
.chart-row { display: flex; position: relative; }
.empty-state { padding: 80px 0; text-align: center; color: var(--text-muted); font-size: 16px; }
</style>
