<template>
  <p class="hint tiny" v-if="meta?.available">当日快照 {{ meta.asof }} · 有效 {{ meta.valid }}/{{ meta.expected }} 只
    <span v-if="meta.excluded"> · 停牌/无有效当日报价等排除 {{ meta.excluded }} 只</span> · 盘中统计，非收盘定稿
    <span v-if="meta.source"> · 源 {{ sourceLabel(meta.source) }}</span>
    <span v-if="showFlow && meta.flow_available === false" class="warn-text"> · 资金流字段不可用，资金榜仍为归档</span>
  </p>
  <p class="hint tiny" v-else-if="enabled">当日实时统计{{ meta?.reason ? `：${meta.reason}；当前保留归档数据，日期如下` : '准备中；当前先显示归档数据' }}</p>
</template>
<script setup lang="ts">
import type { RealtimeMeta } from '@/types/api'
defineProps<{ meta?: RealtimeMeta; enabled?: boolean; showFlow?: boolean }>()
const LABELS: Record<string, string> = {
  eastmoney_snapshot: '东财快照',
  tencent_snapshot: '腾讯快照',
  realtime_snapshot: '实时快照',
}
const sourceLabel = (source: string) => LABELS[source] || source
</script>
