<script lang="ts">
/**
 * 横向条形图 —— 评分榜 / 因子重要性 / SHAP 三处原本各写一份，这里收敛成一处。
 *
 * 两种数据形态自动切换：
 *   单极（全是正数）→ 宽度按最大值归一，从 0 起画。
 *   双极（有正有负，例如 SHAP）→ 以 50% 为轴，正向右、负向左，`offset` 由组件算。
 * 调用方若已自己算好 offset（SHAP 场景），传 `offset` 覆盖即可。
 */
export interface Bar {
  key?: string | number
  label: string
  value: number
  text: string
  /** pos=红（A 股口径的"好"）/ neg=绿 / neutral=金 / info=蓝 */
  tone?: 'pos' | 'neg' | 'neutral' | 'info'
  offset?: number
}
</script>

<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{ bars: Bar[]; labelWidth?: number; maxHeight?: number }>(),
  { labelWidth: 60, maxHeight: 0 },
)

const bipolar = computed(
  () => props.bars.some((b) => b.value < 0) && props.bars.some((b) => b.value > 0),
)
const maxAbs = computed(() => Math.max(...props.bars.map((b) => Math.abs(b.value)), 1e-9))

const width = (b: Bar) => {
  const ratio = Math.abs(b.value) / maxAbs.value
  return (bipolar.value ? ratio * 80 : ratio * 100).toFixed(2)
}
const left = (b: Bar) => {
  if (b.offset != null) return b.offset
  if (!bipolar.value) return 0
  return b.value < 0 ? 95 - Number(width(b)) : 5
}
const toneClass = (b: Bar) => (b.tone === 'pos' ? 'pos' : b.tone === 'neg' ? 'neg' : b.tone || 'neutral')
const valueClass = (b: Bar) => (b.tone === 'pos' ? 'up' : b.tone === 'neg' ? 'down' : '')
</script>

<template>
  <div class="bar-chart" :style="maxHeight ? { maxHeight: maxHeight + 'px', overflowY: 'auto' } : undefined">
    <div v-for="(b, i) in bars" :key="b.key ?? i" class="bar-row">
      <span class="bar-label" :style="{ width: labelWidth + 'px' }" :title="b.label">{{ b.label }}</span>
      <div class="bar-track">
        <div class="bar-fill" :class="toneClass(b)" :style="{ width: width(b) + '%', marginLeft: left(b) + '%' }" />
      </div>
      <span class="bar-value" :class="valueClass(b)">{{ b.text }}</span>
    </div>
  </div>
</template>
