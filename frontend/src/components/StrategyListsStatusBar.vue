<!--
  每日名单刷新状态条 —— 「策略池 / 选股结果 / 选股日历 / K 线自选」消费**同一份**契约产物。

  背景（用户 2026-10-13）："为什么每日自动化后，不是所有策略的选股都更新呢？……数据太分散，
  还是该消费对象没有消费数据？" 以前每条策略各写各的 CSV/台账，缺了也没人报错。
  现在 `scripts/refresh_strategy_lists.py` 按 `config/strategies.json` 的 `daily_refresh` 声明
  逐条刷新 + 校验 + 归档，写一份 `output/results_cn/strategy_lists/status.json`；
  这个组件把它渲染成状态条，页面只读、不重算。

  用法：`<StrategyListsStatusBar />`（自己拉取）或 `<StrategyListsStatusBar :status="pool.lists_status" />`（复用已取到的）。
-->
<template>
  <div v-if="view?.available" class="lists-bar">
    <span class="pill" :class="view.ok ? '' : 'bad-pill'">
      名单刷新 <b>{{ view.ok ? '全部达标' : '需处理 ' + (view.attention?.length || 0) + ' 项' }}</b>
    </span>
    <span class="pill dim tiny">目标交易日 {{ view.target_date || '—' }}</span>
    <span class="pill dim tiny" v-if="view.generated_at">刷新于 {{ view.generated_at }}</span>
    <span v-for="row in shown" :key="row.id" class="ls-chip" :class="tone(row.status)" :title="chipTitle(row)">
      <i>{{ icon(row.status) }}</i>{{ row.name || row.id }}
      <span class="dim tiny">{{ row.date_after || '—' }}</span>
    </span>
    <span v-if="hiddenCount" class="dim tiny">… 另 {{ hiddenCount }} 条</span>
  </div>
  <div v-else-if="view" class="lists-bar">
    <span class="pill dim tiny">名单刷新契约：{{ view.hint }}</span>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { getStrategyListsStatus } from '@/api/client'
import type { StrategyListRefresh, StrategyListsStatus } from '@/types/api'

const props = withDefaults(defineProps<{ status?: StrategyListsStatus | null; limit?: number }>(), {
  status: null,
  limit: 8,
})

const fetched = ref<StrategyListsStatus | null>(null)
const view = computed<StrategyListsStatus | null>(() => props.status ?? fetched.value)

onMounted(async () => {
  if (props.status) return
  try {
    fetched.value = await getStrategyListsStatus()
  } catch {
    /* 状态条取不到不影响页面本身，静默即可 */
  }
})

const ORDER: Record<string, number> = {
  stale: 0, failed: 1, timeout: 2, no_signal: 3, updated: 4, already_fresh: 5, skipped: 9,
}
const ICONS: Record<string, string> = {
  updated: '✅', already_fresh: '✅', no_signal: '➖', stale: '⚠️', timeout: '⏱️',
  failed: '❌', skipped: '⏭️',
}
const TONES: Record<string, string> = {
  updated: 'ok', already_fresh: 'ok', no_signal: 'dim', stale: 'warn', timeout: 'warn',
  failed: 'bad', skipped: 'dim',
}
const sorted = computed<StrategyListRefresh[]>(() => {
  const rows = [...(view.value?.rows || [])]
  return rows.sort((a, b) => (ORDER[a.status] ?? 7) - (ORDER[b.status] ?? 7) || a.id.localeCompare(b.id))
})
const shown = computed(() => sorted.value.slice(0, props.limit))
const hiddenCount = computed(() => Math.max(0, sorted.value.length - shown.value.length))

const icon = (status: string) => ICONS[status] ?? '•'
const tone = (status: string) => TONES[status] ?? 'dim'
const chipTitle = (row: StrategyListRefresh) =>
  `${row.name || row.id}｜${row.status}｜名单 ${row.date_after || '—'}（历史 ${row.history_after || '—'}，${row.rows ?? '—'} 只，${row.elapsed_seconds ?? 0}s）\n${row.reason || ''}`
</script>

<style scoped>
.lists-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-2) 0;
}
.ls-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 1px 8px;
  border: 1px solid var(--border-color);
  border-radius: var(--r-pill);
  font-size: var(--fs-xs);
  background: var(--bg-card);
}
.ls-chip.ok { border-color: var(--state-ok); color: var(--state-ok); }
.ls-chip.warn { border-color: var(--state-warn); color: var(--state-warn); }
.ls-chip.bad { border-color: var(--state-bad); color: var(--state-bad); }
.ls-chip.dim { color: var(--text-muted); }
.ls-chip i { font-style: normal; }
</style>
