<!--
  K 线页「研报多空调研」面板（P2.72）。

  用户要求（2026-10-17）："K线图表里的策略选出来的票，每只我都想做一下针对个股的研报
  调研，用于对个股的多空分析，方便买卖股票时做为参考。并在前端展示。"

  数据源 `/api/stocks/<code>/research`：Bynewlt 研报库的原文证据（机构/报告日期/PDF 页码/
  原文引文/来源哈希）+ agent 按技能规则**逐条核对**后整理的多空要点（每条挂引用编号）。
  未收录/未整理的票如实显示状态，不编造内容；本面板只读盘，不发计费请求（"刷新"=重新读盘）。
-->
<template>
  <div class="panel stock-research-panel" v-if="code">
    <h6>
      研报多空调研
      <span class="dim tiny">{{ sourceSubtitle }}</span>
      <span class="tag info" v-if="isPublic">公开来源</span>
      <span class="tag info" v-if="dossier?.updated_at">核对于 {{ (dossier.updated_at || '').slice(0, 10) }}</span>
      <button class="btn-ghost btn-sm" style="margin-left:auto" @click="reload()">刷新</button>
    </h6>

    <StateBlock v-if="error" state="error" title="取不到研报调研" :detail="error" :retry="reload" />
    <StateBlock v-else-if="loading && !data" state="loading" title="加载研报调研…" />

    <template v-else-if="data">
      <!-- 已合成：多空稿 + 引用明细 -->
      <template v-if="dossier">
        <div class="verdict-row">
          <span class="stance" :class="stanceClass(dossier.stance)">{{ dossier.stance || '证据不足' }}</span>
          <span class="tag ok" v-if="dossier.verification?.level === 'agent_checked'">逐条核对</span>
          <span class="tag info" v-else-if="dossier.verification?.level === 'auto_checked'">自动引文核对</span>
          <span class="tag info" v-if="data.evidence?.reports">{{ isPublic ? '公开研报' : '库内报告' }} {{ data.evidence.reports }} 份</span>
          <span class="tag dim" v-if="brokerText">{{ brokerText }}</span>
          <span class="dim tiny">
            {{ windowText }}
            <template v-if="data.fetched_at"> · 证据取于 {{ data.fetched_at.slice(0, 10) }}</template>
          </span>
        </div>

        <p class="rs-summary" v-if="dossier.summary">{{ dossier.summary }}</p>

        <div class="case-grid">
          <div class="case bull">
            <b class="up">多头依据</b>
            <ul class="pt-list" v-if="dossier.bull?.length">
              <li v-for="(p, i) in dossier.bull" :key="i">
                <span>{{ p.point }}</span>
                <span class="cite-chips">
                  <button v-for="rid in p.refs" :key="rid" class="cite-chip mono"
                          :class="{ open: isOpen('bull', i, rid) }"
                          :title="citeTitle(rid)" @click="toggle('bull', i, rid)">{{ rid }}</button>
                </span>
                <div class="cite-pop" v-if="activePop('bull', i)">
                  <p class="cite-quote">{{ activePop('bull', i)?.quote }}</p>
                  <p class="cite-quote zh" v-if="activePop('bull', i)?.quote_zh">{{ activePop('bull', i)?.quote_zh }}</p>
                  <p class="dim tiny">{{ citeSource(activePop('bull', i)) }}</p>
                  <p class="dim tiny" v-if="activePop('bull', i)?.url">
                    <a :href="activePop('bull', i)?.url" target="_blank" rel="noopener">查看原文 ↗</a>
                  </p>
                </div>
              </li>
            </ul>
            <p class="dim tiny" v-else>—</p>
          </div>
          <div class="case bear">
            <b class="down">空头依据</b>
            <ul class="pt-list" v-if="dossier.bear?.length">
              <li v-for="(p, i) in dossier.bear" :key="i">
                <span>{{ p.point }}</span>
                <span class="cite-chips">
                  <button v-for="rid in p.refs" :key="rid" class="cite-chip mono"
                          :class="{ open: isOpen('bear', i, rid) }"
                          :title="citeTitle(rid)" @click="toggle('bear', i, rid)">{{ rid }}</button>
                </span>
                <div class="cite-pop" v-if="activePop('bear', i)">
                  <p class="cite-quote">{{ activePop('bear', i)?.quote }}</p>
                  <p class="cite-quote zh" v-if="activePop('bear', i)?.quote_zh">{{ activePop('bear', i)?.quote_zh }}</p>
                  <p class="dim tiny">{{ citeSource(activePop('bear', i)) }}</p>
                  <p class="dim tiny" v-if="activePop('bear', i)?.url">
                    <a :href="activePop('bear', i)?.url" target="_blank" rel="noopener">查看原文 ↗</a>
                  </p>
                </div>
              </li>
            </ul>
            <p class="dim tiny" v-else>—</p>
          </div>
        </div>

        <div class="risk-block" v-if="dossier.risks?.length">
          <b class="dim">风险与提示</b>
          <ul class="pt-list">
            <li v-for="(p, i) in dossier.risks" :key="i">
              <span>{{ p.point }}</span>
              <span class="cite-chips">
                <button v-for="rid in p.refs" :key="rid" class="cite-chip mono"
                        :class="{ open: isOpen('risk', i, rid) }"
                        :title="citeTitle(rid)" @click="toggle('risk', i, rid)">{{ rid }}</button>
              </span>
              <div class="cite-pop" v-if="activePop('risk', i)">
                <p class="cite-quote">{{ activePop('risk', i)?.quote }}</p>
                <p class="cite-quote zh" v-if="activePop('risk', i)?.quote_zh">{{ activePop('risk', i)?.quote_zh }}</p>
                <p class="dim tiny">{{ citeSource(activePop('risk', i)) }}</p>
                <p class="dim tiny" v-if="activePop('risk', i)?.url">
                  <a :href="activePop('risk', i)?.url" target="_blank" rel="noopener">查看原文 ↗</a>
                </p>
              </div>
            </li>
          </ul>
        </div>

        <table class="data-table tiny" v-if="dossier.ratings?.length">
          <thead>
            <tr><th>机构</th><th>评级</th><th>目标价</th><th>报告期</th><th>调整</th></tr>
          </thead>
          <tbody>
            <tr v-for="(r, i) in dossier.ratings" :key="i">
              <td>{{ r.broker }}</td>
              <td>{{ r.rating || '—' }}</td>
              <td class="mono">{{ r.target_price || '—' }}</td>
              <td class="mono">{{ r.date || '—' }}</td>
              <td class="dim">{{ r.change || '—' }}</td>
            </tr>
          </tbody>
        </table>

        <details class="cites" v-if="dossier.citations?.length">
          <summary>引用明细（{{ dossier.citations.length }} 条 · 点要点旁编号可展开原文）</summary>
          <div class="cite-row" v-for="c in dossier.citations" :key="c.id" :id="'rs-cite-' + c.id">
            <span class="cite-id mono">[{{ c.id }}]</span>
            <div class="cite-body">
              <p class="cite-quote">{{ c.quote }}</p>
              <p class="cite-quote zh" v-if="c.quote_zh">{{ c.quote_zh }}</p>
              <p class="dim tiny">{{ citeSource(c) }}</p>
              <p class="dim tiny" v-if="c.url">
                <a :href="c.url" target="_blank" rel="noopener">原文链接 ↗</a>
              </p>
            </div>
          </div>
        </details>

        <p class="hint tiny rs-foot" v-if="isPublic">
          本稿基于公开来源证据（东财券商研报/巨潮公告/新闻/结构化数据）逐条核对引用整理
          （<span class="mono">cite</span> 引文绑定检查）；引用以原文为准，不构成投资建议。
          <template v-if="dossier.caveats"> {{ dossier.caveats }}</template>
        </p>
        <p class="hint tiny rs-foot" v-else>
          本稿按 Bynewlt 研报原文逐条核对整理（<span class="mono">cite</span> 引文绑定检查
          <template v-if="readCount"> + 关键数字 <span class="mono">read</span> 原文页核对 {{ readCount }} 处</template>）；
          引用以原文为准，研报观点不构成投资建议。
          <template v-if="dossier.caveats"> {{ dossier.caveats }}</template>
          <template v-if="callsText"> · 该票累计研报库调用 {{ callsText }} 次。</template>
        </p>
      </template>

      <!-- 未合成：按状态给话，不编造内容 -->
      <template v-else>
        <div class="verdict-row" v-if="data.strategy_labels?.length">
          <span class="tag dim" v-for="label in data.strategy_labels" :key="label">{{ label }}</span>
          <span class="dim tiny" v-if="data.fetched_at">证据取于 {{ data.fetched_at.slice(0, 10) }}</span>
        </div>
        <template v-if="data.state === 'fetched'">
          <p class="rs-digest">
            证据已取回：<b>{{ data.evidence?.items ?? '—' }}</b> {{ isPublic ? '条证据' : '条原文引文' }}
            <template v-if="data.evidence?.context">（另有 {{ data.evidence.context }} 条相邻上下文）</template>，
            覆盖 <b>{{ data.evidence?.reports ?? '—' }}</b> 份报告
            <template v-if="brokerText">，机构：{{ brokerText }}</template>
            <template v-if="data.evidence?.report_dates?.length">，报告日期 {{ data.evidence.report_dates[0] }} ~ {{ data.evidence.report_dates[1] }}</template>。
          </p>
          <p class="rs-digest dim tiny" v-if="isPublic">
            细分：研报全文 {{ data.evidence?.report_pdfs ?? 0 }} 份 · 公告 {{ data.evidence?.announcements ?? '—' }} 条（全文 {{ data.evidence?.announcement_pdfs ?? 0 }}）·
            新闻 {{ data.evidence?.news ?? '—' }} 条（正文 {{ data.evidence?.news_text ?? 0 }}）<template v-if="tushareText"> · {{ tushareText }}</template>
          </p>
          <p class="hint tiny">{{ data.hint }}</p>
        </template>
        <StateBlock v-else state="empty" :title="stateTitle" :detail="data.hint || ''" />
      </template>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import StateBlock from '@/components/ui/StateBlock.vue'
import { getStockResearch } from '@/api/client'
import { useAsync } from '@/composables/useAsync'
import type { StockResearchCitation, StockResearchResponse } from '@/types/api'

const props = defineProps<{ code: string | null }>()

const EMPTY: StockResearchResponse = { code: '', in_scope: false, state: 'not_in_scope', dossier: null }

const { data, error, loading, reload } = useAsync<StockResearchResponse>(
  () => (props.code ? getStockResearch(props.code) : Promise.resolve(EMPTY)),
  () => props.code,
)

const dossier = computed(() => data.value?.dossier || null)
const citeMap = computed(() => {
  const map = new Map<string, StockResearchCitation>()
  for (const c of dossier.value?.citations || []) map.set(c.id, c)
  return map
})

const brokerText = computed(() => {
  const brokers = data.value?.evidence?.brokers || []
  if (!brokers.length) return ''
  const shown = brokers.slice(0, 3).join('、')
  return brokers.length > 3 ? `${shown} 等 ${brokers.length} 家` : shown
})

/** 公开来源稿（Bynewlt 未收录 → 公开证据兜底）：徽标/措辞/细分展示都按此切换。 */
const isPublic = computed(() => (
  data.value?.source === 'public_web'
  || data.value?.dossier?.source === 'public_web'
  || data.value?.evidence?.source === 'public_web'
))
const sourceSubtitle = computed(() => (
  isPublic.value ? '公开来源证据 · 逐条核对引用' : 'Bynewlt 研报库 · 逐条核对原文'
))
const windowText = computed(() => {
  if (isPublic.value) return dossier.value?.window || '公开来源证据'
  const since = data.value?.evidence?.window_since
  return `报告窗口 ${since || dossier.value?.window || '—'} 起`
})
const tushareText = computed(() => {
  const t = data.value?.evidence?.tushare || {}
  const total = Object.values(t).reduce((sum, n) => sum + (n || 0), 0)
  return total > 0 ? `结构化数据 ${total} 条（券商预测/调研/回购/解禁/筹码等）` : ''
})

const readCount = computed(() => (dossier.value?.citations || []).filter(c => c.verified === 'read').length)
const callsText = computed(() => {
  const n = data.value?.calls_used
  return typeof n === 'number' && n > 0 ? String(n) : ''
})

const opened = ref<string | null>(null)
const STATE_TITLES: Record<string, string> = {
  not_in_scope: '不在本轮调研名单',
  queued: '已入调研队列',
  fetching: '正在拉取证据',
  not_found: '研报库暂未收录',
  empty: '库中暂无匹配证据',
  no_data: '公开来源证据不足',
  error: '拉取出错',
  unknown: '结果待确认',
  rejected: '调用被拒',
}
const stateTitle = computed(() => STATE_TITLES[data.value?.state || ''] || '研报调研')

/** 倾向标签：偏多=红、偏空=绿（A 股口径）、中性=金、证据不足=灰。 */
function stanceClass(stance?: string) {
  if (!stance) return 'na'
  if (stance.includes('偏多')) return 'long'
  if (stance.includes('偏空')) return 'short'
  if (stance.includes('中性')) return 'neutral'
  return 'na'
}

function toggle(scope: string, index: number, refId: string) {
  const key = `${scope}-${index}::${refId}`
  opened.value = opened.value === key ? null : key
}
function isOpen(scope: string, index: number, refId: string) {
  return opened.value === `${scope}-${index}::${refId}`
}
function activePop(scope: string, index: number): StockResearchCitation | null {
  const key = opened.value
  if (!key || !key.startsWith(`${scope}-${index}::`)) return null
  return citeMap.value.get(key.split('::')[1]) || null
}
function citeTitle(rid: string) {
  const cite = citeMap.value.get(rid)
  if (!cite) return rid
  return `${cite.broker || cite.source || ''} ${cite.reportDate || cite.date || ''}`.trim() || rid
}
function citeSource(c: StockResearchCitation | null | undefined) {
  if (!c) return ''
  const parts = [c.broker || c.source || '', c.reportTitle || c.title || '', c.reportDate || c.date || '']
  const line = parts.filter(Boolean).join(' · ')
  const page = c.page ? ` · p${c.page}` : ''
  const verified = c.verified === 'read' ? '（原文页核对）' : c.verified === 'cite' ? '（引文绑定核对）' : ''
  return `${line}${page}${verified}`
}
</script>

<style scoped>
.verdict-row { display: flex; align-items: center; flex-wrap: wrap; gap: var(--sp-2); margin-bottom: var(--sp-2); }
.stance {
  font-weight: 700; font-size: var(--fs-md); padding: 2px 12px; border-radius: var(--r-pill);
  border: 1px solid var(--border-color);
}
.stance.long { color: var(--accent-up); border-color: var(--accent-up); }
.stance.short { color: var(--accent-down); border-color: var(--accent-down); }
.stance.neutral { color: var(--accent-gold); border-color: var(--accent-gold); }
.stance.na { color: var(--text-muted); border-color: var(--border-color); }
.rs-summary { margin: 0 0 var(--sp-2); line-height: 1.6; }
.case-grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-2); margin-bottom: var(--sp-2); }
@media (max-width: 900px) { .case-grid { grid-template-columns: 1fr; } }
.case {
  border: 1px solid var(--border-color); border-radius: var(--r-2);
  padding: var(--sp-2); background: var(--bg-input);
}
.pt-list { list-style: none; margin: 4px 0 0; padding: 0; }
.pt-list li { margin: 0 0 var(--sp-1); line-height: 1.55; }
.cite-chips { display: inline-flex; gap: 3px; margin-left: 4px; vertical-align: middle; }
.cite-chip {
  font-size: var(--fs-xs); padding: 0 5px; border-radius: var(--r-1); cursor: pointer;
  border: 1px solid var(--border-color); background: transparent; color: var(--accent-gold);
}
.cite-chip.open, .cite-chip:hover { border-color: var(--accent-gold); }
.cite-pop {
  margin: 2px 0 var(--sp-1); padding: var(--sp-1) var(--sp-2);
  border-left: 2px solid var(--accent-gold); background: var(--bg-input);
}
.cite-quote { margin: 0 0 2px; font-size: var(--fs-xs); line-height: 1.5; white-space: pre-wrap; }
.cite-quote.zh { color: var(--text-muted); }
.risk-block { margin: 0 0 var(--sp-2); }
.risk-block .pt-list li { font-size: var(--fs-sm); }
.cites { margin: var(--sp-2) 0; }
.cites summary { cursor: pointer; font-size: var(--fs-sm); color: var(--text-muted); }
.cite-row { display: flex; gap: var(--sp-1); margin-top: var(--sp-2); }
.cite-id { color: var(--accent-gold); white-space: nowrap; }
.cite-body { min-width: 0; }
.rs-digest { margin: 0 0 var(--sp-1); line-height: 1.6; }
.rs-foot { margin: var(--sp-2) 0 0; }
</style>
