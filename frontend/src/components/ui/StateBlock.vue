<!-- loading / empty / error 三态合一：消灭每个页面各写一遍的 v-if 分支 -->
<template>
  <div class="state-block" :class="state">
    <div v-if="state === 'loading'" class="spinner" />
    <div v-if="title" class="state-title">{{ title }}</div>
    <div v-if="detail" class="state-detail">{{ detail }}</div>
    <div v-if="$slots.default || retry" class="state-actions">
      <slot />
      <button v-if="retry" class="btn btn-sm" @click="retry()">重试</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{ state?: 'loading' | 'empty' | 'error'; title?: string; detail?: string; retry?: () => void }>(),
  { state: 'empty' },
)
const state = computed(() => props.state)
</script>
