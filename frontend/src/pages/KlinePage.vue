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
      <span v-if="data.profile" class="profile">
        · {{ data.profile.board }}<template v-if="data.profile.industry"> / {{ data.profile.industry }}</template>
        <template v-if="data.profile.list_date"> · 上市 {{ data.profile.list_date }}</template>
        <span v-for="f in data.profile.flags" :key="f" class="flag" :class="flagClass(f)">· {{ f }}</span>
      </span>
    </div>
    <div class="stage-bar" v-if="data?.stage && data.stage.stage !== 'insufficient'">
      <span class="stage-chip" :class="'stage-' + data.stage.stage">{{ data.stage.label }}</span>
      <span class="stage-why">{{ data.stage.evidence }}</span>
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
import { onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import ControlPanel from '@/components/ControlPanel.vue'
import KlineChart from '@/components/KlineChart.vue'
import ChipDistribution from '@/components/ChipDistribution.vue'
import SignalStats from '@/components/SignalStats.vue'
import { useKlineData } from '@/composables/useKlineData'

const route = useRoute()
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

function flagClass(flag: string) {
  if (flag.startsWith('已退市') || flag.startsWith('ST')) return 'flag-bad'
  if (flag.startsWith('次新')) return 'flag-warn'
  return 'flag-info'
}

// 支持从别处跳转：/#/kline?code=301230.SZ
onMounted(() => {
  const code = String(route.query.code || '')
  if (code) selectedCode.value = code
})
watch(() => route.query.code, v => { if (v) selectedCode.value = String(v) })
</script>

