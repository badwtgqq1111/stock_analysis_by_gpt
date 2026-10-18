<!--
  个股多空（合并视图，2026-10-18）。

  用户反馈："AI 结论和研报证据分开意义不大" —— 这里把两个数据源**合成一张结论卡**：
    · AI 结论：每次入选时模型给的买/持/卖（决策口径）；
    · 研报证据：卖方研报/公开来源的多空要点与引用（原文口径）。
  两栏依据里用 `AI` / `研报` 小标签标明出处；两边结论冲突时显示"分歧"，不强行统一。
-->
<template>
  <div class="panel longshort-panel" v-if="code">
    <h6>
      个股多空
      <span class="tag" :class="stanceTone">{{ combinedStance }}</span>
      <span class="dim tiny" v-if="aiLatest && dossier">AI 决策 + 研报证据（已合并）</span>
      <span class="dim tiny" v-else-if="aiLatest">仅 AI 结论（研报暂无）</span>
      <span class="dim tiny" v-else-if="dossier">仅研报证据（AI 暂无）</span>
      <button class="btn-ghost btn-sm" style="margin-left:auto" :disabled="loading" @click="load()">刷新</button>
    </h6>

    <StateBlock v-if="loading && !ai && !research" state="loading" title="加载个股多空…" />
    <StateBlock v-else-if="error && !ai && !dossier" state="error" title="取不到多空数据" :detail="error" :retry="load" />

    <template v-else>
      <div class="verdict-row">
        <span class="tag info" v-if="aiLatest">
          AI：{{ aiLatest.action }}<template v-if="aiLatest.confidence != null">（{{ Math.round(aiLatest.confidence * 100) }}%）</template>
        </span>
        <span class="tag" v-if="dossier">研报：{{ dossier.stance || '证据不足' }}</span>
        <span class="dim tiny" v-if="aiLatest?.decision_date">决策日 {{ aiLatest.decision_date }}</span>
        <span class="dim tiny" v-if="dossier?.updated_at">研报核对于 {{ (dossier.updated_at || '').slice(0, 10) }}</span>
      </div>

      <p class="ls-summary" v-if="summaryText">{{ summaryText }}</p>

      <div class="case-grid">
        <div class="case bull">
          <b>多头依据</b>
          <p v-if="aiLatest?.bull_case"><span class="src">AI</span>{{ aiLatest.bull_case }}</p>
          <ul v-if="dossier?.bull?.length">
            <li v-for="(p, i) in dossier.bull" :key="'b' + i"><span class="src">研报</span>{{ p.point }}</li>
          </ul>
          <p v-if="!aiLatest?.bull_case && !dossier?.bull?.length" class="dim">—</p>
        </div>
        <div class="case bear">
          <b>空头依据</b>
          <p v-if="aiLatest?.bear_case"><span class="src">AI</span>{{ aiLatest.bear_case }}</p>
          <ul v-if="dossier?.bear?.length">
            <li v-for="(p, i) in dossier.bear" :key="'s' + i"><span class="src">研报</span>{{ p.point }}</li>
          </ul>
          <p v-if="!aiLatest?.bear_case && !dossier?.bear?.length" class="dim">—</p>
        </div>
      </div>

      <p class="tiny risk" v-if="riskText"><b>风险：</b>{{ riskText }}</p>

      <table class="data-table tiny" v-if="dossier?.ratings?.length">
        <thead><tr><th>机构</th><th>评级</th><th>目标价</th><th>日期</th></tr></thead>
        <tbody>
          <tr v-for="(r, i) in dossier.ratings" :key="i">
            <td>{{ r.broker }}</td><td>{{ r.rating }}</td>
            <td>{{ r.target_price || '—' }}</td><td class="mono">{{ r.date }}</td>
          </tr>
        </tbody>
      </table>

      <details class="cites" v-if="dossier?.citations?.length">
        <summary>引用明细（{{ dossier.citations.length }} 条 · {{ researchSource }}）</summary>
        <div class="cite-row" v-for="c in dossier.citations" :key="c.id">
          <span class="cite-id mono">[{{ c.id }}]</span>
          <div class="cite-body">
            <p class="cite-quote">{{ c.quote }}</p>
            <p class="cite-quote zh" v-if="c.quote_zh">{{ c.quote_zh }}</p>
            <p class="dim tiny">{{ citeSource(c) }}</p>
            <p class="dim tiny" v-if="c.url"><a :href="c.url" target="_blank" rel="noopener">原文链接 ↗</a></p>
          </div>
        </div>
      </details>

      <p class="hint tiny">
        AI 是决策口径（买/持/卖），研报是原文证据口径；两边冲突时标"分歧"，不强行统一，也不构成投资建议。
        <template v-if="dossier?.caveats"> {{ dossier.caveats }}</template>
      </p>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import StateBlock from '@/components/ui/StateBlock.vue'
import { getStockAi, getStockResearch } from '@/api/client'
import type {
  StockAiResponse, StockResearchCitation, StockResearchResponse,
} from '@/types/api'

const props = defineProps<{ code: string | null }>()

const ai = ref<StockAiResponse | null>(null)
const research = ref<StockResearchResponse | null>(null)
const loading = ref(false)
const error = ref('')
let sequence = 0

const aiLatest = computed(() => ai.value?.latest || null)
const dossier = computed(() => research.value?.dossier || null)
const researchSource = computed(() => research.value?.source === 'public_web' ? '公开来源' : 'Bynewlt 研报库')

const aiStance = computed(() => {
  const action = aiLatest.value?.action || ''
  if (!action) return null
  if (action.includes('买入')) return '偏多'
  if (action.includes('卖出')) return '偏空'
  return '中性'
})
const combinedStance = computed(() => {
  const a = aiStance.value, r = dossier.value?.stance || null
  if (a && r) {
    if (a === r) return a
    if (a === '中性') return r
    if (r === '中性' || r === '证据不足') return a
    return '分歧'
  }
  return a || r || '暂无结论'
})
const stanceTone = computed(() => {
  const s = combinedStance.value
  if (s === '偏多') return 'ok'
  if (s === '偏空') return 'bad'
  if (s === '分歧') return 'gold'
  return 'dim'
})
const summaryText = computed(() =>
  [aiLatest.value?.summary, dossier.value?.summary].filter(Boolean).join(' '))
const riskText = computed(() => {
  const flags = aiLatest.value?.risk_flags || []
  const risks = (dossier.value?.risks || []).map((r) => r.point)
  return [...flags, ...risks].filter(Boolean).join('；')
})

function citeSource(c: StockResearchCitation) {
  const parts = [c.broker || c.source || '', c.reportTitle || c.title || '', c.reportDate || c.date || '']
  const line = parts.filter(Boolean).join(' · ')
  return `${line}${c.page ? ` · p${c.page}` : ''}`
}

async function load() {
  const code = props.code
  if (!code) { ai.value = null; research.value = null; return }
  const request = ++sequence
  loading.value = true
  error.value = ''
  const [a, r] = await Promise.allSettled([getStockAi(code), getStockResearch(code)])
  if (request !== sequence) return
  ai.value = a.status === 'fulfilled' ? a.value : null
  research.value = r.status === 'fulfilled' ? r.value : null
  if (a.status === 'rejected' && r.status === 'rejected') {
    error.value = String((a.reason as Error)?.message || a.reason || '取数失败')
  }
  loading.value = false
}

watch(() => props.code, load, { immediate: true })
</script>

<style scoped>
.verdict-row { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; margin-bottom: var(--sp-2); }
.ls-summary { font-size: var(--fs-md); line-height: 1.65; margin: var(--sp-2) 0; }
.case-grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-3); margin: var(--sp-2) 0; }
.case { border: 1px solid var(--border-color); border-radius: var(--r-2); padding: var(--sp-2);
  background: rgba(255, 255, 255, 0.02); }
.case.bull > b { color: var(--accent-up); }
.case.bear > b { color: var(--accent-down); }
.case p { font-size: var(--fs-sm); line-height: 1.6; margin: 4px 0; }
.case ul { margin: 4px 0 0; padding-left: 16px; }
.case li { font-size: var(--fs-sm); line-height: 1.6; margin: 3px 0; }
.src { display: inline-block; font-size: 10px; padding: 0 4px; margin-right: 5px; border-radius: 3px;
  color: var(--text-muted); border: 1px solid var(--border-color); }
.risk { color: var(--accent-gold); }
.cites { margin-top: var(--sp-2); }
.cite-row { display: flex; gap: var(--sp-2); padding: 6px 0; border-bottom: 1px dashed var(--border-color); }
.cite-id { color: var(--accent-gold); }
.cite-body { flex: 1; min-width: 0; }
.cite-quote { font-size: var(--fs-sm); line-height: 1.55; margin: 0 0 2px; }
.cite-quote.zh { color: var(--text-muted); }
@media (max-width: 760px) { .case-grid { grid-template-columns: 1fr; } }
</style>
