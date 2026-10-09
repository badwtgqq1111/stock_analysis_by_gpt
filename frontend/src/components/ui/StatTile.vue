<template>
  <div class="stat-tile" :title="hint || undefined">
    <div class="stat-label">{{ label }}</div>
    <div class="stat-value" :class="toneClass">{{ value }}</div>
    <div v-if="sub" class="stat-sub">{{ sub }}</div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{ label: string; value: string | number; sub?: string; hint?: string; tone?: string }>(),
  { tone: '' },
)
const toneClass = computed(() =>
  ({ up: 'up', down: 'down', ok: 'ok', bad: 'bad', warn: 'warn-text', gold: 'link' })[props.tone] || '',
)
</script>
