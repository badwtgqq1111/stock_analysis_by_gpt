<!--
  筹码分布（价格轴与左侧 K 线共用同一套 priceRange/plotHeight，2026-10-18）。

  旧实现按 bin 序号等距摆放，价格轴与左侧 K 线对不上；现在每个 bin 用
  `(priceMax - price) / (priceMax - priceMin) * plotHeight` 定位，
  与 lightweight-charts 的线性价格轴一致。下方补充：
    获利盘/套牢盘、平均成本、90% 成本区间、集中度、筹码峰。
-->
<template>
  <div v-if="chips && chips.prices.length > 0" class="chip-panel">
    <div class="chip-title" title="120 日成交量按 exp(-0.02×age) 衰减；柱形只显示当前价格轴可见区间，统计值用完整分布">
      筹码分布<template v-if="asof"> · {{ asof }}</template>
      <span class="chip-legend">红=获利 / 绿=套牢</span>
    </div>
    <div class="chip-chart" :style="{ height: plotHeight + 'px' }">
      <div v-if="bandStyle" class="chip-band" :style="bandStyle" />
      <div
        v-for="(price, i) in chips.prices"
        :key="i"
        class="chip-bar"
        :class="price <= chips.current_price ? 'profit' : 'loss'"
        :style="barStyle(i)"
      />
      <div class="chip-price-line" :style="priceLineStyle">
        <span class="price-label">现价 {{ chips.current_price.toFixed(3) }}</span>
      </div>
    </div>
    <div class="chip-stats">
      <div class="cs-row">
        <span>获利盘</span><b class="up">{{ profitPct.toFixed(2) }}%</b>
        <span>套牢</span><b class="down">{{ (100 - profitPct).toFixed(2) }}%</b>
      </div>
      <div class="cs-row"><span>平均成本</span><b>{{ avgCost }}</b></div>
      <div class="cs-row"><span>90%成本</span><b>{{ p5 }} ~ {{ p95 }}</b></div>
      <div class="cs-row">
        <span>集中度</span><b>{{ concentration }}%</b>
        <span>峰</span><b>{{ peak }}</b>
      </div>
    </div>
  </div>
  <div v-else class="chip-panel chip-empty" :style="{ height: height + 'px' }"></div>
</template>

<script setup lang="ts">
import type { ChipData } from '@/types/api'
import { computed } from 'vue'

const props = defineProps<{
  chips: ChipData | null
  height: number
  /** K 线可见区间的价格上下界（与左侧价格轴同一份） */
  priceRange: [number, number]
  /** 价格绘图区高度（不含时间轴），由 KlineChart 实测后传入 */
  plotHeight: number
  /** 悬停日期（窗口内筹码按当天重算；不传=最新一根） */
  asof?: string
}>()

const priceMin = computed(() => props.priceRange[0])
const priceMax = computed(() => props.priceRange[1])
const span = computed(() => Math.max(priceMax.value - priceMin.value, 1e-9))

function priceToY(price: number) {
  return ((priceMax.value - price) / span.value) * props.plotHeight
}

const binHeight = computed(() => {
  const n = props.chips?.prices.length || 0
  if (n < 2) return 1
  const width = props.chips!.prices[1] - props.chips!.prices[0]
  return Math.max(1, (width / span.value) * props.plotHeight)
})

const maxVol = computed(() => Math.max(...(props.chips?.volumes || [0]), 0.1))

function barStyle(i: number) {
  if (!props.chips) return { display: 'none' }
  const price = props.chips.prices[i]
  const vol = props.chips.volumes[i]
  const top = priceToY(price) - binHeight.value / 2
  if (top > props.plotHeight || top + binHeight.value < 0) return { display: 'none' }
  return {
    width: (vol / maxVol.value) * 100 + '%',
    height: binHeight.value + 'px',
    top: top + 'px',
  }
}

const priceLineStyle = computed(() => {
  if (!props.chips) return {}
  return { top: priceToY(props.chips.current_price) + 'px' }
})

const stats = computed(() => {
  const c = props.chips
  if (!c || !c.prices.length) {
    return { profit: 0, avg: 0, p5: 0, p95: 0, concentration: 0, peak: 0 }
  }
  const total = c.volumes.reduce((a, b) => a + b, 0) || 1
  let profit = 0
  let weighted = 0
  let peakIdx = 0
  for (let i = 0; i < c.prices.length; i++) {
    if (c.prices[i] <= c.current_price) profit += c.volumes[i]
    weighted += c.prices[i] * c.volumes[i]
    if (c.volumes[i] > c.volumes[peakIdx]) peakIdx = i
  }
  const avg = weighted / total
  // 按价格从低到高累计，取覆盖 5% / 95% 的成本分位
  const order = c.prices.map((_, i) => i).sort((a, b) => c.prices[a] - c.prices[b])
  let cum = 0
  let p5 = c.prices[order[0]]
  let p95 = c.prices[order[order.length - 1]]
  for (const i of order) {
    cum += c.volumes[i]
    if (cum <= total * 0.05) p5 = c.prices[i]
    if (cum <= total * 0.95) p95 = c.prices[i]
  }
  return {
    profit, avg, p5, p95,
    concentration: avg > 0 ? ((p95 - p5) / avg) * 100 : 0,
    peak: c.prices[peakIdx],
  }
})

const profitPct = computed(() => (stats.value.profit / (props.chips?.volumes.reduce((a, b) => a + b, 0) || 1)) * 100)
const avgCost = computed(() => stats.value.avg.toFixed(3))
const p5 = computed(() => stats.value.p5.toFixed(3))
const p95 = computed(() => stats.value.p95.toFixed(3))
const concentration = computed(() => stats.value.concentration.toFixed(2))
const peak = computed(() => stats.value.peak.toFixed(3))
const bandStyle = computed(() => {
  const top = priceToY(stats.value.p95)
  const bottom = priceToY(stats.value.p5)
  const height = Math.max(1, bottom - top)
  if (top > props.plotHeight || top + height < 0) return null
  return { top: Math.max(0, top) + 'px', height: height + 'px' }
})
</script>

<style scoped>
.chip-panel {
  width: 190px;
  border-left: 1px solid var(--border-color);
  position: relative;
  flex-shrink: 0;
  padding-bottom: 6px;
}
.chip-title {
  position: absolute; top: 2px; left: 0; right: 0; z-index: 3;
  text-align: center; font-size: 11px; color: var(--text-muted);
  pointer-events: none; text-shadow: 0 1px 2px rgba(0, 0, 0, 0.8);
}
.chip-legend { display: block; font-size: 9px; color: var(--text-dim, #777); }
.chip-chart { position: relative; margin: 0 4px; overflow: hidden; }
.chip-band {
  position: absolute; left: 0; right: 0; z-index: 1;
  background: rgba(255, 209, 102, 0.08);
  border-top: 1px dashed rgba(255, 209, 102, 0.45);
  border-bottom: 1px dashed rgba(255, 209, 102, 0.45);
}
.chip-bar { position: absolute; right: 0; z-index: 2; }
.chip-bar.profit { background: var(--accent-up); opacity: 0.75; }
.chip-bar.loss { background: var(--accent-down); opacity: 0.65; }
.chip-price-line {
  position: absolute; left: 0; right: 0; z-index: 4;
  border-top: 1px dashed var(--accent-gold); pointer-events: none;
}
.price-label {
  position: absolute; right: 2px; top: -14px; font-size: 10px; color: var(--accent-gold);
  background: rgba(20, 24, 32, 0.85); padding: 0 3px; border-radius: 2px;
}
.chip-stats { margin: 4px 6px 0; font-size: 10px; line-height: 1.55; }
.cs-row { display: flex; align-items: baseline; gap: 4px; color: var(--text-muted); }
.cs-row b { color: var(--text-primary); font-weight: 600; }
.cs-row .up { color: var(--accent-up); }
.cs-row .down { color: var(--accent-down); }
.chip-empty { min-height: 80px; }
</style>
