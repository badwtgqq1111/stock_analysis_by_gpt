<!--
  K 线页「AI 多空分析」面板。

  用户要求（2026-10-10）："被选中的每只个股再用 AI 做多空综合分析，评价买入，持有，还是卖出，
  要求在前端 K 线页面显示。"

  数据源 `/api/stocks/<code>/ai`：这只票**历史上每一次**被某个策略选中时，AI 给出的
  多空分析与 买入/持有/卖出。策略覆盖生产（模型选股/手臂）与备选（多因子候选）。
-->
<template>
  <div class="panel ai-panel" v-if="code">
    <h6>
      AI 多空分析
      <span class="dim tiny">覆盖全部策略（生产 + 备选）</span>
      <span class="tag info" v-if="summary?.latest_date">AI 已到 {{ summary.latest_date }}</span>
      <button class="btn-ghost btn-sm" style="margin-left:auto" @click="reload()">刷新</button>
    </h6>

    <StateBlock v-if="error" state="error" title="取不到 AI 结论" :detail="error" :retry="reload" />
    <StateBlock v-else-if="loading && !data" state="loading" title="加载 AI 多空分析…" />

    <template v-else-if="data?.n && data.latest">
      <div class="verdict-row">
        <span class="verdict" :class="verdictClass(data.latest.action)">{{ data.latest.action }}</span>
        <span class="tag info" v-if="data.latest.confidence != null">
          置信度 {{ Math.round((data.latest.confidence || 0) * 100) }}%
        </span>
        <span class="tag gold" v-for="label in data.latest.strategy_labels" :key="label">{{ label }}</span>
        <span class="dim tiny">
          决策日 {{ data.latest.decision_date }}
          <template v-if="data.latest.horizon"> · 视角 {{ data.latest.horizon }}</template>
          · 历史入选 {{ data.n }} 次
        </span>
      </div>

      <p class="ai-summary" v-if="data.latest.summary">{{ data.latest.summary }}</p>

      <div class="case-grid">
        <div class="case bull">
          <b class="up">多头依据</b>
          <p>{{ data.latest.bull_case || '—' }}</p>
        </div>
        <div class="case bear">
          <b class="down">空头依据</b>
          <p>{{ data.latest.bear_case || '—' }}</p>
        </div>
      </div>

      <p class="tiny risk" v-if="data.latest.risk_flags.length">
        风险：
        <span class="tag bad" v-for="flag in data.latest.risk_flags" :key="flag">{{ flag }}</span>
      </p>

      <table class="data-table tiny" v-if="data.rows.length > 1">
        <thead>
          <tr><th>决策日</th><th>评价</th><th>置信度</th><th>入选策略</th><th>摘要</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in data.rows" :key="row.decision_date">
            <td class="mono">{{ row.decision_date }}</td>
            <td><span class="verdict sm" :class="verdictClass(row.action)">{{ row.action }}</span></td>
            <td class="mono">
              {{ row.confidence == null ? '—' : Math.round(row.confidence * 100) + '%' }}
            </td>
            <td class="dim">{{ row.strategy_labels.join('、') || '—' }}</td>
            <td>{{ row.summary || '—' }}</td>
          </tr>
        </tbody>
      </table>
      <p class="dim tiny" v-if="data.rows.length > 1">按决策日倒序；同一只票在多个策略名单里各算一次。</p>
    </template>

    <StateBlock v-else state="empty" :title="data?.hint || '该股票暂无 AI 结论'" />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import StateBlock from '@/components/ui/StateBlock.vue'
import { getAiPicksSummary, getStockAi } from '@/api/client'
import { useAsync } from '@/composables/useAsync'
import type { StockAiResponse } from '@/types/api'

const props = defineProps<{ code: string | null }>()

const EMPTY: StockAiResponse = {
  code: '', available: false, n: 0, latest: null, rows: [], actions: {},
}

// 依赖 code：换票自动重取；没有代码时不发请求（直接给空态）。
const { data, error, loading, reload } = useAsync<StockAiResponse>(
  () => (props.code ? getStockAi(props.code) : Promise.resolve(EMPTY)),
  () => props.code,
)

// 顶部「AI 已到 <交易日>」：来自按日汇总，答"AI 覆盖到哪天"。
const summaryState = useAsync(() => getAiPicksSummary())
const summary = computed(() => summaryState.data.value)

/** 买入=红、卖出=绿（A 股口径）、持有/待分析=中性灰。 */
function verdictClass(action: string) {
  if (action === '买入') return 'buy'
  if (action === '卖出') return 'sell'
  if (action === '待分析') return 'pending'
  return 'hold'
}
</script>

<style scoped>
.verdict-row { display: flex; align-items: center; flex-wrap: wrap; gap: var(--sp-2); margin-bottom: var(--sp-2); }
.verdict {
  font-weight: 700; font-size: var(--fs-md); padding: 2px 12px; border-radius: var(--r-pill);
  border: 1px solid var(--border-color);
}
.verdict.sm { font-size: var(--fs-xs); padding: 0 8px; }
.verdict.buy { color: var(--accent-up); border-color: var(--accent-up); }
.verdict.sell { color: var(--accent-down); border-color: var(--accent-down); }
.verdict.hold { color: var(--accent-gold); border-color: var(--accent-gold); }
.verdict.pending { color: var(--text-muted); border-color: var(--border-color); }
.ai-summary { margin: 0 0 var(--sp-2); line-height: 1.6; }
.case-grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-2); margin-bottom: var(--sp-2); }
@media (max-width: 900px) { .case-grid { grid-template-columns: 1fr; } }
.case {
  border: 1px solid var(--border-color); border-radius: var(--r-2);
  padding: var(--sp-2); background: var(--bg-input);
}
.case p { margin: 4px 0 0; line-height: 1.55; }
.risk { margin: 0 0 var(--sp-2); }
.risk .tag { margin-right: 4px; }
</style>
