<template>
  <div v-if="chips && chips.prices.length > 0" class="chip-panel" :style="{ height: height + 'px' }">
    <div class="chip-title">筹码分布</div>
    <div class="chip-chart" ref="chartRef">
      <div
        v-for="(price, i) in chips.prices"
        :key="i"
        class="chip-bar"
        :class="price <= chips.current_price ? 'profit' : 'loss'"
        :style="barStyle(i)"
      ></div>
      <div class="chip-price-line" :style="priceLineStyle">
        <span class="price-label">现价 {{ chips.current_price.toFixed(3) }}</span>
      </div>
    </div>
    <div class="chip-footer">筹码%</div>
  </div>
  <div v-else class="chip-panel chip-empty" :style="{ height: height + 'px' }"></div>
</template>

<script setup lang="ts">
import type { ChipData } from '@/types/api'
import { computed } from 'vue'

const props = defineProps<{
  chips: ChipData | null
  height: number
  priceRange: [number, number]
}>()

const barHeight = computed(() => {
  if (!props.chips || props.chips.prices.length === 0) return 0
  return (props.height - 44) / props.chips.prices.length
})

function barStyle(i: number) {
  if (!props.chips) return {}
  const vol = props.chips.volumes[i]
  const maxVol = Math.max(...props.chips.volumes, 0.1)
  const widthPct = (vol / maxVol) * 100
  const top = (props.height - 44) - (i + 1) * barHeight.value
  return {
    width: widthPct + '%',
    height: Math.max(barHeight.value, 1) + 'px',
    top: top + 'px',
  }
}

const priceLineStyle = computed(() => {
  if (!props.chips || !props.chips.prices.length) return {}
  const [lo, hi] = props.priceRange
  const pct = ((props.chips.current_price - lo) / (hi - lo)) * 100
  const clamped = Math.max(0, Math.min(100, pct))
  const topPx = (props.height - 44) * (1 - clamped / 100)
  return { top: topPx + 'px' }
})
</script>

<style scoped>
.chip-panel {
  width: 140px;
  border-left: 1px solid var(--border-color);
  position: relative;
  flex-shrink: 0;
}
.chip-title {
  text-align: center;
  font-size: 11px;
  color: var(--text-muted);
  padding: 4px 0;
}
.chip-chart {
  position: relative;
  flex: 1;
  height: calc(100% - 40px);
  margin: 0 4px;
}
.chip-bar {
  position: absolute;
  right: 0;
  min-width: 1px;
}
.chip-bar.profit {
  background: rgba(0, 212, 170, 0.5);
}
.chip-bar.loss {
  background: rgba(239, 71, 111, 0.5);
}
.chip-price-line {
  position: absolute;
  left: 0;
  right: 0;
  height: 0;
  border-top: 1px dashed var(--accent-gold);
}
.price-label {
  position: absolute;
  left: 2px;
  top: -8px;
  font-size: 9px;
  color: var(--accent-gold);
  white-space: nowrap;
}
.chip-footer {
  text-align: center;
  font-size: 10px;
  color: var(--text-muted);
  padding: 2px 0;
}
.chip-empty {
  opacity: 0.2;
}
</style>
