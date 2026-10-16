<template>
  <p class="hint tiny" v-if="meta?.available">当日快照 {{ meta.asof }} · 有效 {{ meta.valid }}/{{ meta.expected }} 只
    <span v-if="meta.excluded"> · 停牌/无有效当日报价等排除 {{ meta.excluded }} 只</span> · 盘中统计，非收盘定稿
  </p>
  <p class="hint tiny" v-else-if="enabled">当日实时统计{{ meta?.reason ? `：${meta.reason}；当前保留归档数据，日期如下` : '准备中；当前先显示归档数据' }}</p>
</template>
<script setup lang="ts">
import type { RealtimeMeta } from '@/types/api'
defineProps<{ meta?: RealtimeMeta; enabled?: boolean }>()
</script>
