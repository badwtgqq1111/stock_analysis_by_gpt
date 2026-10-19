<template>
  <svg class="sparkline" :viewBox="`0 0 ${width} ${height}`" preserveAspectRatio="none"
       :style="{ height: height + 'px' }" aria-hidden="true">
    <polyline v-if="points" :points="points" fill="none" :stroke="color"
              stroke-width="1.5" vector-effect="non-scaling-stroke" />
  </svg>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(defineProps<{
  values: (number | null | undefined)[]
  width?: number
  height?: number
  color?: string
}>(), { width: 100, height: 28, color: 'var(--accent-gold)' })

const points = computed(() => {
  const vals = props.values.filter((v): v is number => v != null && Number.isFinite(v))
  if (vals.length < 2) return ''
  const min = Math.min(...vals)
  const max = Math.max(...vals)
  const span = max - min || 1
  const n = vals.length
  return vals.map((v, i) => {
    const x = (i / (n - 1)) * props.width
    const y = props.height - ((v - min) / span) * (props.height - 2) - 1
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
})
</script>

<style scoped>
.sparkline { width: 100%; display: block; overflow: visible; }
</style>
