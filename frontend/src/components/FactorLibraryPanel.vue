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

    <div class="fitness-bar">
      <span class="tiny dim">因子适应度（可配置 Fitness Hook；只用于排序，不替代准入与闸门）</span>
      <select class="input tiny-input" v-model="fitnessProfile" @change="loadFitness">
        <option value="default">默认（IC/ICIR/t + 冗余·换手·衰减惩罚）</option>
        <option value="low_turnover">低换手（换手惩罚加倍）</option>
        <option value="long_horizon">长持有（偏 h20、衰减慢）</option>
      </select>
      <span class="tiny dim" v-if="fitness?.counts">
        候选 {{ fitness.counts.factors }}｜已审 {{ fitness.counts.with_inspector }}｜
        <b class="up">推荐 {{ fitness.counts.recommended }}</b>｜待审 {{ fitness.counts.pending_review }}
      </span>
      <span class="picker-actions">
        <label class="tiny dim inline-check">
          <input type="checkbox" v-model="includeModel" @change="compare && runCompare()" />
          叠加模型分（台账·决策日）
        </label>
        <button class="btn-ghost btn-sm" :disabled="compareIds.length < 2" @click="runCompare">
          对比选中（{{ compareIds.length }}/5）
        </button>
        <button class="btn-ghost btn-sm" v-if="compareIds.length" @click="compareIds = []">清空</button>
      </span>
    </div>
    <p v-if="error" class="hint tiny bad">{{ error }}</p>
    <div class="table-scroll">
      <table class="data-table">
        <thead>
          <tr>
            <th class="pick-col"><input type="checkbox" :checked="pickedAll" @change="toggleAll" /></th>
            <th class="text-left">因子 / 条件</th>
            <th>分类</th>
            <th>kind</th>
            <th>状态</th>
            <th>档</th>
            <th class="text-left">族 / 表达式</th>
            <th>IC</th>
            <th>ICIR</th>
            <th>NW t</th>
            <th :title="`适应度（profile=${fitnessProfile}；只用于排序，不替代准入与闸门）`">fitness</th>
            <th>簇</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in items" :key="item.kind + ':' + item.id"
              class="f-row" :class="{ picked: item.id === selectedId }"
              @click="openInspector(item.id)">
            <td class="pick-col" @click.stop>
              <input type="checkbox" :value="item.id" v-model="compareIds" :disabled="!canPick(item.id)" />
            </td>
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
            <td class="mono" :title="fitnessTitle(item.id)">{{ fitnessOf(item.id) }}</td>
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
    <!-- 多因子对比（2–5 条）：滚动 IC 对齐叠加 + 指标表；缺档案的列出来给命令 -->
    <section class="panel inspector" v-if="compare">
      <h6>
        多因子对比 · <span class="tiny dim">{{ compare.table?.map((r: any) => r.id).join(' / ') }}</span>
        <span class="dim tiny" v-if="compare.common_dates">共同日期 {{ compare.common_dates }} 天（{{ (compare.window || []).join(' ~ ') }}）</span>
        <button class="btn-ghost btn-sm" @click="compare = null">收起</button>
      </h6>
      <p class="tiny dim" v-if="compare.missing?.length">
        缺档案：<span v-for="m in compare.missing" :key="m.id"><code>{{ m.id }}</code>（{{ m.command }}） </span>
      </p>
      <SvgCurve v-if="compareSeries.length" :series="compareSeries" :height="180" />
      <div class="table-scroll">
        <table class="data-table">
          <thead><tr><th class="text-left">因子</th><th>期数</th><th>IC(h20)</th><th>ICIR</th><th>NW t</th>
            <th>多空</th><th>胜率</th><th>Q5 换手</th><th>max|ρ|</th><th>判定</th></tr></thead>
          <tbody>
            <tr v-for="row in compare.table" :key="row.id">
              <td class="text-left mono">{{ row.id }}</td>
              <td>{{ row.periods ?? '—' }}</td>
              <td class="mono" :class="tone(row.ic_h20)">{{ num(row.ic_h20, 4) }}</td>
              <td class="mono">{{ num(row.icir_h20, 2) }}</td>
              <td class="mono">{{ num(row.nw_t_h20, 2) }}</td>
              <td class="mono" :class="tone(row.long_short_mean)">{{ pct2(row.long_short_mean) }}</td>
              <td>{{ pct2(row.long_short_win_rate) }}</td>
              <td>{{ pct2(row.top_turnover_mean) }}</td>
              <td class="mono">{{ num(row.max_abs_rho, 3) }}</td>
              <td :class="row.redundant ? 'down' : 'up'">{{ row.redundant ? '冗余' : '不冗余' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="tiny dim">{{ compare.note }}</p>
    </section>

    <!-- 因子审查（Inspector）：多维体检 + 血缘；没有档案就让人一键跑（不生成占位曲线） -->
    <section class="panel inspector" v-if="selectedId">
      <h6>
        因子审查 · <code>{{ selectedId }}</code>
        <span class="dim tiny" v-if="inspector?.available">
          近 {{ inspector.window_months }} 个月 · 生成于 {{ inspector.generated_at }}
        </span>
        <button class="btn-ghost btn-sm" @click="selectedId = ''">收起</button>
        <button class="btn-ghost btn-sm" :disabled="running" @click="runInspector">
          {{ running ? '审查中…' : (inspector?.available ? '重跑审查' : '跑审查') }}
        </button>
      </h6>
      <p class="tiny dim" v-if="inspectorError">{{ inspectorError }}</p>
      <p class="tiny dim" v-if="inspector && !inspector.available">
        {{ inspector.reason }}；命令：<code>{{ inspector.command }}</code>
        <span v-if="inspector.reviewed?.length">（已审 {{ inspector.reviewed.length }} 条，例如 {{ inspector.reviewed.slice(0, 3).join(', ') }}）</span>
      </p>
      <template v-if="inspector?.available">
        <div class="pill-row">
          <span v-for="h in [1, 5, 20]" :key="h" class="pill" :class="tone(inspector.ic_by_horizon?.[h]?.mean)">
            h{{ h }} IC <b>{{ num(inspector.ic_by_horizon?.[h]?.mean, 4) }}</b>
            · ICIR <b>{{ num(inspector.ic_by_horizon?.[h]?.icir, 2) }}</b>
            · NWt <b>{{ num(inspector.ic_by_horizon?.[h]?.nw_t, 2) }}</b>
            <span class="dim tiny">（{{ inspector.ic_by_horizon?.[h]?.periods ?? 0 }} 期）</span>
          </span>
          <span class="pill" v-if="inspector.decay">衰减 h1/h20 <b>{{ num(inspector.decay.ratio_h1_over_h20, 2) }}</b></span>
          <span class="pill" :class="inspector.redundancy?.redundant ? 'down' : 'up'"
                v-if="inspector.redundancy?.status === 'ok'">
            与准入最大 |ρ| <b>{{ num(inspector.redundancy.max_abs_rho, 3) }}</b>
            （{{ inspector.redundancy.max_abs_rho_peer }}）{{ inspector.redundancy.redundant ? ' → 冗余' : ' → 不冗余' }}
          </span>
          <span class="pill" v-if="inspector.quantiles?.top_turnover_mean != null">
            Q5 换手 <b>{{ pct2(inspector.quantiles.top_turnover_mean) }}</b>
          </span>
        </div>

        <div class="insp-grid">
          <div class="insp-block">
            <h6 class="sub">滚动 IC（h20，窗口 {{ inspector.rolling_window }}）</h6>
            <SvgCurve :series="rollingSeries" :height="160" />
          </div>
          <div class="insp-block">
            <h6 class="sub">5 档分位组合（{{ inspector.quantiles?.periods }} 期 · {{ inspector.quantiles?.unit }}）</h6>
            <div class="q-bars">
              <div v-for="(value, key) in (inspector.quantiles?.mean_group_return || {})" :key="key" class="q-row">
                <span class="q-label">{{ String(key).toUpperCase() }}</span>
                <span class="q-bar" :class="Number(value) >= 0 ? 'pos' : 'neg'"
                      :style="{ width: barWidth(Number(value)) }" />
                <span class="q-val mono" :class="tone(value)">{{ pct2(value) }}</span>
              </div>
            </div>
            <p class="tiny" v-if="inspector.quantiles?.long_short_mean != null">
              多空 Q5−Q1 <b :class="tone(inspector.quantiles.long_short_mean)">{{ pct2(inspector.quantiles.long_short_mean) }}</b>
              · 胜率 {{ pct2(inspector.quantiles.long_short_win_rate) }}
            </p>
          </div>
        </div>

        <p class="tiny dim">
          血缘：面板 {{ inspector.lineage?.panel?.start }} ~ {{ inspector.lineage?.panel?.end }} ·
          {{ inspector.lineage?.rows }} 行 / {{ inspector.lineage?.dates }} 日 / {{ inspector.lineage?.stocks }} 股 ·
          覆盖率 {{ pct2(inspector.lineage?.coverage) }} · PIT {{ pct2(inspector.lineage?.pit_valid_share) }} ·
          标签 {{ Object.values(inspector.lineage?.label || {}).join(' / ') }}
        </p>
        <p class="tiny dim" v-for="note in (inspector.notes || [])" :key="note">· {{ note }}</p>
      </template>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import {
  getAgentFactors, getFactorFitness, getFactorInspector, getFactorInspectorCompare, runAgentAction,
} from '@/api/client'
import type { AgentFactorItem, AgentFactorResponse } from '@/types/api'
import SvgCurve from '@/components/ui/SvgCurve.vue'

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

// ── 适应度 + 多因子对比 ──
const fitness = ref<any>(null)
const fitnessProfile = ref('default')
const compareIds = ref<string[]>([])
const compare = ref<any>(null)
const includeModel = ref(false)

async function loadFitness() {
  try {
    fitness.value = await getFactorFitness(fitnessProfile.value)
  } catch {
    fitness.value = null
  }
}
const fitnessMap = computed<Record<string, number>>(() => {
  const map: Record<string, number> = {}
  for (const row of fitness.value?.factors || []) map[row.id] = row.score
  return map
})
function fitnessOf(id: string) {
  const value = fitnessMap.value[id]
  return value === undefined ? '—' : value.toFixed(3)
}
function fitnessTitle(id: string) {
  const row = (fitness.value?.factors || []).find((r: any) => r.id === id)
  if (!row) return '不在 fitness 候选里（状态不是 admitted/in_use/candidate）'
  const pend = Object.entries(row.penalties || {}).filter(([, v]: any) => v?.penalty == null).map(([k]) => k)
  return `score ${row.score.toFixed(3)}｜rank ${row.rank}｜`
    + (pend.length ? `未审惩罚项：${pend.join(',')}（缺 inspector 档案）` : '惩罚项齐')
    + (row.recommended ? '｜✅ 推荐入池候选' : (row.pending_review ? '｜待审查' : ''))
}
const pickedAll = computed(() => items.value.length > 0 && items.value.every((i) => compareIds.value.includes(i.id)))
function canPick(id: string) {
  return compareIds.value.includes(id) || compareIds.value.length < 5
}
function toggleAll(event: Event) {
  const checked = (event.target as HTMLInputElement).checked
  compareIds.value = checked ? items.value.slice(0, 5).map((i) => i.id) : []
}
const compareColors = ['#118ab2', '#e07a5f', '#2a9d8f', '#e9c46a', '#8d99ae', '#f4a261']
const compareSeries = computed(() => (compare.value?.series || []).map((item: any, i: number) => ({
  name: item.id,
  color: compareColors[i % compareColors.length],
  points: (item.points || []).map((p: any) => Number(p.ic)),
})))
async function runCompare() {
  if (compareIds.value.length < 2) return
  try {
    compare.value = await getFactorInspectorCompare(compareIds.value, includeModel.value)
  } catch (err) {
    error.value = String(err)
  }
}

// ── 因子审查（Inspector）──
const selectedId = ref('')
const inspector = ref<any>(null)
const inspectorError = ref('')
const running = ref(false)

async function openInspector(id: string) {
  selectedId.value = id
  inspectorError.value = ''
  inspector.value = null
  try {
    inspector.value = await getFactorInspector(id)
  } catch (err) {
    inspectorError.value = String(err)
  }
}

async function runInspector() {
  if (!selectedId.value) return
  running.value = true
  inspectorError.value = ''
  try {
    const payload = await runAgentAction({ action: 'factor_inspect', factor_id: selectedId.value })
    if (payload.result) inspector.value = { ...payload.result, available: true }
    if (payload.exit !== 0) inspectorError.value = payload.stderr || payload.stdout || `exit ${payload.exit}`
  } catch (err) {
    inspectorError.value = String(err)
  } finally {
    running.value = false
  }
}

const rollingSeries = computed(() => {
  const points = (inspector.value?.rolling_ic?.['20'] || []).map((row: any) => Number(row.ic))
  return [{ name: '滚动 IC（h20）', color: '#118ab2', points }]
})
const maxAbsQuantile = computed(() => Math.max(
  0.0001, ...Object.values(inspector.value?.quantiles?.mean_group_return || {}).map((v: any) => Math.abs(Number(v)))))
function barWidth(value: number) {
  return `${Math.min(100, (Math.abs(value) / maxAbsQuantile.value) * 100)}%`
}
function tone(value: any) {
  return Number(value ?? 0) >= 0 ? 'up' : 'down'
}
function pct2(value: any) {
  const n = Number(value)
  return Number.isFinite(n) ? `${(n * 100).toFixed(2)}%` : '—'
}

onMounted(() => { void fetchFactors(false); void loadFitness() })
</script>

<style scoped>
.pick-col { width: 26px; text-align: center; }
.fitness-bar { display: flex; gap: var(--sp-2); align-items: center; flex-wrap: wrap; margin-bottom: var(--sp-2); }
.fitness-bar .tiny-input { width: auto; font-size: var(--fs-xs); }
.picker-actions { margin-left: auto; display: flex; gap: 6px; align-items: center; }
.inline-check { display: inline-flex; gap: 4px; align-items: center; }
.f-row { cursor: pointer; }
.f-row:hover { background: rgba(255, 255, 255, 0.03); }
.f-row.picked { background: rgba(233, 196, 106, 0.08); }
.inspector { margin-top: var(--sp-3); }
.insp-grid { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr); gap: var(--sp-3); margin: var(--sp-2) 0; }
@media (max-width: 1100px) { .insp-grid { grid-template-columns: 1fr; } }
.insp-block h6.sub { font-size: var(--fs-xs); color: var(--text-muted); margin-bottom: 4px; }
.q-bars { display: flex; flex-direction: column; gap: 4px; }
.q-row { display: grid; grid-template-columns: 28px 1fr 64px; align-items: center; gap: 6px; }
.q-label { font-size: 10px; color: var(--text-dim); }
.q-bar { height: 12px; border-radius: 3px; display: block; }
.q-bar.pos { background: var(--accent-up); }
.q-bar.neg { background: var(--accent-down); }
.q-val { font-size: 10px; text-align: right; }
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
