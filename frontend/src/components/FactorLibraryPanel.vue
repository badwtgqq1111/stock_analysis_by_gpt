<template>
  <section class="panel library">
    <h6>
      因子库
      <span class="pill dim tiny">{{ totals.n ?? '—' }} 条</span>
      <span class="dim tiny" v-if="payload?.generated_at">同步于 {{ payload.generated_at }}</span>
      <span class="dim tiny" v-if="payload?.window_months?.length">
        窗口 {{ payload.window_months[0] }}~{{ payload.window_months[payload.window_months.length - 1] }}
      </span>
    </h6>

    <!-- 计数条：一眼看清"多少条过了门槛、多少条被同簇更强因子替代" -->
    <div class="counts">
      <button
        v-for="row in statusRows"
        :key="row.status"
        class="count"
        :class="[row.status, { active: status === row.status }]"
        :title="row.hint"
        @click="toggleStatus(row.status)"
      >
        <b>{{ row.n }}</b><span>{{ row.label }}</span>
      </button>
      <div class="count-sep" />
      <button
        v-for="item in categories"
        :key="item.key"
        class="count cat"
        :class="{ active: category === item.key }"
        :title="item.core"
        @click="toggleCategory(item.key)"
      >
        <b>{{ totals.by_category?.[item.label] ?? 0 }}</b><span>{{ item.label }}</span>
      </button>
      <div class="count-sep" />
      <button class="count" :class="[ 'kind', { active: kind === 'condition' } ]" @click="toggleKind('condition')">
        <b>{{ totals.by_kind?.condition ?? 0 }}</b><span>族 A 条件</span>
      </button>
      <button class="count" :class="[ 'kind', { active: kind === 'factor' } ]" @click="toggleKind('factor')">
        <b>{{ totals.by_kind?.factor ?? 0 }}</b><span>族 B 因子</span>
      </button>
    </div>

    <div class="filters">
      <input v-model="search" class="input" placeholder="按因子名 / 表达式 / 族搜索（如 moneyflow、zscore）" spellcheck="false" />
      <select v-model="kind" class="input">
        <option value="">全部 kind</option>
        <option value="factor">factor（族 B 因子）</option>
        <option value="condition">condition（族 A 条件）</option>
      </select>
      <select v-model="status" class="input">
        <option value="">全部状态</option>
        <option v-for="row in statusRows" :key="row.status" :value="row.status">{{ row.label }}</option>
      </select>
      <span class="tiny dim">命中 {{ filtered }} 条</span>
      <span class="tiny dim" v-if="payload?.criteria?.fdr_q">
        准入口径 |IC|≥{{ payload.criteria.min_abs_ic }}、BH-FDR q={{ payload.criteria.fdr_q }}、t≥{{ payload.criteria.hlz_t }}
      </span>
    </div>

    <p v-if="error" class="hint tiny bad">{{ error }}</p>
    <div class="table-scroll">
      <table class="data-table">
        <thead>
          <tr>
            <th class="text-left">因子 / 条件</th>
            <th>分类</th>
            <th>kind</th>
            <th>状态</th>
            <th>档</th>
            <th class="text-left">族 / 表达式</th>
            <th>IC</th>
            <th>ICIR</th>
            <th>NW t</th>
            <th>簇</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in items" :key="item.kind + ':' + item.id">
            <td class="text-left">
              <code>{{ item.id }}</code>
              <div class="tiny desc" :title="item.description || ''">{{ shorten(item.description, 60) }}</div>
              <div class="tiny dim" :title="item.performance_summary || ''">
                {{ shorten(item.performance_summary, 60) }}
              </div>
            </td>
            <td>
              <span class="tag dim" :title="item.category_core || ''">{{ item.category_label || '—' }}</span>
            </td>
            <td><span class="tag" :class="item.kind === 'condition' ? 'gold' : 'info'">{{ item.kind }}</span></td>
            <td><span class="tag" :class="statusTone(item.status)">{{ statusLabel(item.status) }}</span></td>
            <td class="mono">{{ item.grade || '—' }}</td>
            <td class="text-left tiny dim" :title="item.expression || item.family">
              <span v-if="item.family" class="pill dim tiny">{{ item.family }}</span>
              {{ shorten(item.expression, 60) }}
            </td>
            <td class="mono">{{ num(item.ic_mean, 4) }}</td>
            <td class="mono">{{ num(item.icir, 2) }}</td>
            <td class="mono">{{ num(item.nw_t, 2) }}</td>
            <td class="tiny dim">
              <span v-if="item.cluster_size">{{ item.representative ? '代表' : '冗余' }}/{{ item.cluster_size }}</span>
              <span v-else>—</span>
            </td>
          </tr>
          <tr v-if="!items.length && !loading">
            <td colspan="10" class="dim tiny">没有命中的因子（换个分类/状态/关键词试试）</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="more">
      <button class="btn-ghost" :disabled="loading || items.length >= filtered" @click="loadMore">
        {{ items.length >= filtered ? '已到底' : `加载更多（${items.length}/${filtered}）` }}
      </button>
      <span class="tiny dim" v-if="payload?.delta?.newly_admitted">
        本次新准入 {{ payload.delta.newly_admitted.length }} 个（{{ payload.delta.admitted_previous }} → {{ payload.delta.admitted_current }}）
      </span>
    </div>
    <p class="hint tiny">
      “冗余”= 与同簇更强代表的相关性超过阈值（{{ payload?.criteria?.cluster_threshold }}）被剔除 ⇒ 不该重复挖同一族；
      “excluded”= 质量标记列，不是因子。族 A 的条件与「策略池」同一份登记（<code>config/strategies.json</code>）。
    </p>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { getAgentFactors } from '@/api/client'
import type { AgentFactorItem, AgentFactorResponse } from '@/types/api'

const payload = ref<AgentFactorResponse | null>(null)
const items = ref<AgentFactorItem[]>([])
const status = ref('')
const kind = ref('')
const category = ref('')
const search = ref('')
const error = ref('')
const loading = ref(false)

const totals = computed<AgentFactorResponse['totals']>(() => payload.value?.totals || { n: 0 })
// 分类清单由后端给（来源 = config/factor_glossary.json，前端不写死一份）
const categories = computed(() => payload.value?.categories || [])
const filtered = computed(() => payload.value?.filtered ?? 0)
const statusRows = computed(() => {
  const by = totals.value.by_status || {}
  const labels: Record<string, string> = {
    admitted: '准入', in_use: '在用（族A条件）', proposed: '提议（未验）',
    candidate: '候选（族A）', excluded: '质量列', rejected: '已剔除', retired: '已退役',
  }
  const hints: Record<string, string> = {
    admitted: '过了 |IC|/ICIR/BH-FDR/HLZ t 四档，且不是同簇冗余',
    proposed: 'LLM 提议但还没过准入门槛（也是产物，别当结论用）',
    rejected: '被剔除：强度不足 / 多重检验不显著 / 同簇冗余（见 note）',
    excluded: '质量标记列（例如某字段缺失标记），不参与准入',
  }
  return Object.entries(labels)
    .filter(([key]) => by[key])
    .map(([key, label]) => ({ status: key, label, n: by[key], hint: hints[key] || '' }))
})

function statusTone(value: string) {
  return ({ admitted: 'ok', in_use: 'ok', proposed: 'warn', candidate: 'gold', rejected: 'bad', excluded: 'dim', retired: 'dim' } as Record<string, string>)[value] || ''
}
function statusLabel(value: string) {
  return ({ admitted: '准入', in_use: '在用', proposed: '提议', candidate: '候选', rejected: '剔除', excluded: '质量列', retired: '退役' } as Record<string, string>)[value] || value
}
function num(value: number | null | undefined, digits: number) {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(digits) : '—'
}
function shorten(text: string | null | undefined, limit: number) {
  const value = String(text || '')
  return value.length > limit ? `${value.slice(0, limit)}…` : value
}
function toggleStatus(value: string) {
  status.value = status.value === value ? '' : value
}
function toggleKind(value: string) {
  kind.value = kind.value === value ? '' : value
}
function toggleCategory(value: string) {
  category.value = category.value === value ? '' : value
}

async function fetchFactors(append = false) {
  loading.value = true
  try {
    const offset = append ? items.value.length : 0
    const data = await getAgentFactors({
      kind: kind.value, status: status.value, category: category.value, q: search.value, limit: 60, offset,
    })
    payload.value = data
    items.value = append ? [...items.value, ...data.items] : data.items
    error.value = ''
  } catch (err) {
    error.value = `因子库取数失败：${err}`
  } finally {
    loading.value = false
  }
}

function loadMore() {
  void fetchFactors(true)
}

let debounce: ReturnType<typeof setTimeout> | null = null
watch([status, kind, category, search], () => {
  if (debounce) clearTimeout(debounce)
  debounce = setTimeout(() => void fetchFactors(false), 250)
})

onMounted(() => fetchFactors(false))
</script>

<style scoped>
.counts { display: flex; flex-wrap: wrap; gap: var(--sp-2); margin-bottom: var(--sp-2); align-items: center; }
.count {
  display: flex; align-items: baseline; gap: 5px; cursor: pointer;
  background: var(--bg-elevated); border: 1px solid var(--border-color);
  border-radius: var(--r-2); padding: 3px 9px; color: var(--text-muted); font-size: var(--fs-xs);
}
.count b { font-size: var(--fs-lg); color: var(--text-primary); }
.count:hover { border-color: var(--accent-gold); }
.count.active { border-color: var(--accent-gold); background: rgba(255, 209, 102, 0.1); }
.count.admitted b { color: var(--state-ok); }
.count.cat b { color: var(--accent-blue); }
.desc { color: var(--text-muted); }
.count.rejected b { color: var(--state-bad); }
.count.proposed b { color: var(--state-warn); }
.count-sep { width: 1px; height: 18px; background: var(--border-color); }
.filters { display: flex; flex-wrap: wrap; gap: var(--sp-2); align-items: center; margin-bottom: var(--sp-2); }
.filters .input { width: auto; min-width: 190px; }
.more { display: flex; align-items: center; gap: var(--sp-3); margin-top: var(--sp-2); }
</style>
