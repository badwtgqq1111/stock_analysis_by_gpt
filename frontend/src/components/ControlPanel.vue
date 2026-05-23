<template>
  <div class="control-panel">
    <div class="ctrl-item" style="min-width:220px">
      <label>股票代码（★为当前持仓）</label>
      <StockSelector
        :model-value="selectedCode"
        @update:model-value="onCode"
      />
    </div>
    <div class="ctrl-item">
      <label>时间范围</label>
      <TimeRangeSelector
        :model-value="days"
        @update:model-value="onDays"
      />
    </div>
    <div class="ctrl-item" style="min-width:180px">
      <label>手动输入代码</label>
      <ManualStockInput @submit="onManualCode" />
    </div>
    <div class="ctrl-item">
      <label>叠加显示</label>
      <OverlayToggles
        :model-value="overlays"
        @update:model-value="onOverlays"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import StockSelector from './StockSelector.vue'
import TimeRangeSelector from './TimeRangeSelector.vue'
import ManualStockInput from './ManualStockInput.vue'
import OverlayToggles from './OverlayToggles.vue'

const props = defineProps<{
  selectedCode: string | null
  days: number
  overlays: string[]
}>()

const emit = defineEmits<{
  'update:selectedCode': [value: string]
  'update:days': [value: number]
  'update:overlays': [value: string[]]
}>()

function onCode(val: string) { emit('update:selectedCode', val) }
function onDays(val: number) { emit('update:days', val) }
function onOverlays(val: string[]) { emit('update:overlays', val) }
function onManualCode(code: string) { emit('update:selectedCode', code) }
</script>

<style scoped>
.control-panel {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  align-items: flex-end;
  padding: 12px 16px;
  background: var(--bg-card);
  border: 1px solid var(--border-color);
  border-radius: 6px;
  margin-bottom: 12px;
}
.ctrl-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.ctrl-item label {
  font-size: 11px;
  color: var(--text-muted);
  text-transform: uppercase;
}
</style>
