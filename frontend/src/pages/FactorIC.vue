<template>
  <div class="page">
    <PageHeader
      title="因子 IC · 因子池体检"
      description="这一页的职责是<b>把因子池分级并挑出能进池的因子</b>，不是画两张曲线就完事：IC 时序看稳定性、汇总表按等级筛、挑中的因子一键登记成策略池候选。等级阈值直接采用仓库自己的审计口径 <code>scripts/ic_rubric_audit.py</code>（出处 <code>output/research/ic_rubric_audit/REPORT.md</code>）。"
    >
      <template #actions>
        <select v-model="factorSet" class="picker">
          <option value="qlib_alpha158">Qlib Alpha158</option>
          <option value="qlib_alpha360">Qlib Alpha360</option>
        </select>
        <select v-model="horizon" class="picker">
          <option :value="5">5 天</option><option :value="10">10 天</option>
          <option :value="20">20 天</option><option :value="40">40 天</option>
          <option :value="60">60 天</option>
        </select>
        <select v-model.number="topN" class="picker">
          <option :value="5">曲线 Top 5</option><option :value="10">曲线 Top 10</option>
          <option :value="15">曲线 Top 15</option><option :value="20">曲线 Top 20</option>
        </select>
        <button class="btn-ghost" :disabled="loading" @click="fetch">重新计算</button>
        <a class="btn-ghost link-btn" href="#/strategies">策略池 →</a>
      </template>
    </PageHeader>

    <StateBlock v-if="error" state="error" title="取不到因子 IC" :detail="error" :retry="fetch" />
    <StateBlock v-else-if="!data" state="loading"
                title="加载因子 IC…"
                detail="首次计算约 30 秒（之后走 output/factor_ic_cache_*.pkl 缓存）" />

    <template v-else>
      <!-- 概览：一眼看出这版因子集里"有多少能进池" -->
      <div class="panel">
        <div class="tile-row">
          <div class="tile"><label>因子数</label><b>{{ stats.total }}</b>
            <span class="tiny dim">{{ data.factor_set }} · h{{ data.horizon }}</span></div>
          <div class="tile"><label>有效（|IC|≥0.02）</label><b class="ok">{{ stats.valid }}</b>
            <span class="tiny dim">{{ stats.validPct }}</span></div>
          <div class="tile"><label>合格及以上</label><b class="ok">{{ stats.good }}</b>
            <span class="tiny dim">可进多因子池</span></div>
          <div class="tile"><label>优质 / 很强</label><b class="gold">{{ stats.quality }}</b>
            <span class="tiny dim">很强的要查过拟合</span></div>
          <div class="tile"><label>中位 |IC|</label><b class="mono">{{ fmt(stats.medianIc, 4) }}</b></div>
          <div class="tile"><label>中位 |ICIR|</label><b class="mono">{{ fmt(stats.medianIcir, 3) }}</b></div>
          <div class="tile wide"><label>缓存区间</label>
            <b class="mono tiny">{{ cacheWindow }}</b>
            <span class="tiny dim">{{ data.dates.length }} 期（日频 IC）</span></div>
        </div>

        <div class="rubric">
          <div class="rubric-col">
            <h6>等级标尺（|IC| 阈值，来自本仓库审计口径）</h6>
            <ul>
              <li><span class="grade g-invalid">无效</span> |IC| &lt; 0.02</li>
              <li><span class="grade g-pass">合格(入池)</span> 0.02 ~ 0.04</li>
              <li><span class="grade g-good">优质</span> 0.04 ~ 0.07</li>
              <li><span class="grade g-hot">很强/查过拟合</span> ≥ 0.07</li>
            </ul>
          </div>
          <div class="rubric-col">
            <h6>ICIR 分档</h6>
            <ul>
              <li><span class="grade g-invalid">不稳定</span> |ICIR| &lt; 0.3</li>
              <li><span class="grade g-pass">合格</span> 0.3 ~ 0.5</li>
              <li><span class="grade g-good">很好</span> 0.5 ~ 0.8</li>
              <li><span class="grade g-hot">很强</span> ≥ 0.8</li>
            </ul>
          </div>
          <div class="rubric-col wide">
            <h6>口径提醒（不一样别混着比）</h6>
            <p class="tiny">
              <code>ic_rubric_audit</code> 的阈值是为 <b>月频秩 IC</b>（20 日前瞻、按月聚合）定的；
              本页缓存是 <b>日频</b> IC，噪声更大。所以这里的等级只能当<b>粗筛</b>：
              挑出来的因子入池后，还要由 <code>scripts/llm_factor_mining.py</code> 的月频秩 IC /
              Newey-West t / BH-FDR 复核。两套口径目前不共享缓存，切换 horizon 会重算。
            </p>
          </div>
        </div>
      </div>

      <div class="charts-grid">
        <div class="chart-panel">
          <h6>IC 时间序列（曲线 Top {{ topN }}，按 |IC 均值|）</h6>
          <div ref="icChartRef" class="chart-box"></div>
        </div>
        <div class="chart-panel">
          <h6>Rank IC 时间序列</h6>
          <div ref="rankChartRef" class="chart-box"></div>
        </div>
      </div>

      <div class="panel">
        <h6>
          因子池分级（{{ filtered.length }} / {{ sortedSummary.length }} 个）
          <button class="btn-ghost btn-sm" :disabled="pooling || !batchTarget.factors.length"
                  @click="poolBatch">
            {{ gradeFilter === '全部' ? '把当前筛选的' : `把「${gradeFilter}」以上的` }}
            {{ batchTarget.factors.length }} 个因子批量入池
          </button>
          <span class="tiny dim" v-if="poolMsg">{{ poolMsg }}</span>
        </h6>
        <div class="toolbar-row">
          <input v-model="keyword" class="search" placeholder="搜索因子名（如 BETA / CORR / VSTD）" />
          <select v-model="gradeFilter" class="picker">
            <option value="全部">全部等级</option>
            <option value="合格(入池)">合格及以上</option>
            <option value="优质">优质及以上</option>
            <option value="很强/查过拟合">只看很强</option>
          </select>
          <span class="tiny dim">点表头排序；「入池」= 登记成策略池候选（不会进生产）。</span>
        </div>
        <div class="table-scroll">
          <table class="data-table">
            <thead>
              <tr>
                <th class="left" @click="sortBy('grade')">等级 {{ arrow('grade') }}</th>
                <th class="left" @click="sortBy('factor')">因子 {{ arrow('factor') }}</th>
                <th @click="sortBy('mean_ic')">IC 均值 {{ arrow('mean_ic') }}</th>
                <th>IC 标准差</th>
                <th @click="sortBy('ic_ir')">ICIR {{ arrow('ic_ir') }}</th>
                <th @click="sortBy('mean_rank_ic')">RankIC 均值</th>
                <th @click="sortBy('ic_positive_rate')">IC&gt;0 占比</th>
                <th>ICIR 分档</th>
                <th class="left">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="f in filtered" :key="f.factor" :class="{ hot: f._grade === '很强/查过拟合' }">
                <td class="left"><span class="grade" :class="gradeClass(f._grade)">{{ f._grade }}</span></td>
                <td class="left mono">{{ f.factor }}</td>
                <td :class="tone(f.mean_ic)"><b>{{ fmt(f.mean_ic, 4) }}</b></td>
                <td class="dim mono">{{ fmt(f.std_ic, 4) }}</td>
                <td :class="tone(f.ic_ir)"><b>{{ fmt(f.ic_ir, 3) }}</b></td>
                <td :class="tone(f.mean_rank_ic)">{{ fmt(f.mean_rank_ic, 4) }}</td>
                <td>{{ f.ic_positive_rate != null ? (f.ic_positive_rate * 100).toFixed(1) + '%' : '—' }}</td>
                <td class="tiny dim">{{ icirBand(f.ic_ir) }}</td>
                <td class="left">
                  <button class="btn-ghost btn-xs" :disabled="pooling" @click="poolOne(f)">入池</button>
                </td>
              </tr>
              <tr v-if="!filtered.length">
                <td colspan="9" class="dim">没有匹配的因子</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { createChart, ColorType, type IChartApi, type ISeriesApi, type LineData, type LineWidth, type Time } from 'lightweight-charts'
import { getFactorIC, registerStrategyCandidate } from '@/api/client'
import type { FactorICResponse, FactorICSummary } from '@/types/api'
import PageHeader from '@/components/ui/PageHeader.vue'
import StateBlock from '@/components/ui/StateBlock.vue'

// lightweight-charts v4 的 LineWidth 类型只允许 1|2|3|4，但运行时会按 1.5px 渲染；
// 这里显式放宽类型，保住原本想要的细线观感（原先直接写 1.5 会导致 vue-tsc 构建失败）。
const LINE_WIDTH = 1.5 as unknown as LineWidth

type Grade = '无效' | '合格(入池)' | '优质' | '很强/查过拟合'
type Row = FactorICSummary & { _grade: Grade; _abs_ic: number }

const data = ref<FactorICResponse | null>(null)
const factorSet = ref('qlib_alpha158')
const horizon = ref(20)
const topN = ref(10)
const error = ref('')
const loading = ref(false)
const keyword = ref('')
const gradeFilter = ref<'全部' | Grade>('全部')
const sortKey = ref<'grade' | 'factor' | 'mean_ic' | 'ic_ir' | 'mean_rank_ic' | 'ic_positive_rate'>('mean_ic')
const sortAsc = ref(false)
const pooling = ref(false)
const poolMsg = ref('')

const icChartRef = ref<HTMLDivElement | null>(null)
const rankChartRef = ref<HTMLDivElement | null>(null)
let icChart: IChartApi | null = null
let rankChart: IChartApi | null = null

const LINE_COLORS = [
  '#ef476f', '#ffd166', '#ff9f1c', '#118ab2', '#06d6a0',
  '#ef767a', '#a29bfe', '#55efc4', '#74b9ff', '#fdcb6e',
  '#e17055', '#00cec9', '#6c5ce7', '#f9ca24', '#7bed9f',
  '#e056a0', '#a29bfe', '#ff7979', '#badc58', '#c7ecee',
]

// ── 等级：阈值与 scripts/ic_rubric_audit.py 的 grade() 保持一致 ──────────────
const GRADE_ORDER: Record<Grade, number> = { '无效': 0, '合格(入池)': 1, '优质': 2, '很强/查过拟合': 3 }

function gradeOf(meanIc: number | null | undefined): Grade {
  const a = Math.abs(Number(meanIc) || 0)
  if (a < 0.02) return '无效'
  if (a < 0.04) return '合格(入池)'
  if (a < 0.07) return '优质'
  return '很强/查过拟合'
}

function gradeClass(grade: Grade) {
  if (grade === '无效') return 'g-invalid'
  if (grade === '合格(入池)') return 'g-pass'
  if (grade === '优质') return 'g-good'
  return 'g-hot'
}

function icirBand(v: number | null | undefined) {
  const a = Math.abs(Number(v) || 0)
  if (!Number.isFinite(a) || a === 0) return '—'
  if (a < 0.3) return '不稳定'
  if (a < 0.5) return '合格'
  if (a < 0.8) return '很好'
  return '很强'
}

const rows = computed<Row[]>(() => (data.value?.summary || []).map((item) => ({
  ...item,
  _grade: gradeOf(item.mean_ic),
  _abs_ic: Math.abs(Number(item.mean_ic) || 0),
})))

const sortedSummary = computed(() => {
  const list = [...rows.value]
  const key = sortKey.value
  list.sort((a, b) => {
    const sign = sortAsc.value ? 1 : -1
    if (key === 'grade') return sign * (GRADE_ORDER[a._grade] - GRADE_ORDER[b._grade])
    if (key === 'factor') return -sign * String(a.factor).localeCompare(String(b.factor))
    return sign * ((Number((a as any)[key]) || 0) - (Number((b as any)[key]) || 0))
  })
  return list
})

const filtered = computed(() => {
  const floor = gradeFilter.value === '全部' ? 0 : GRADE_ORDER[gradeFilter.value]
  const kw = keyword.value.trim().toLowerCase()
  return sortedSummary.value.filter((row) =>
    GRADE_ORDER[row._grade] >= floor
    && (!kw || row.factor.toLowerCase().includes(kw)),
  )
})

const stats = computed(() => {
  const list = rows.value
  const total = list.length || 1
  const valid = list.filter((r) => r._grade !== '无效').length
  const good = list.filter((r) => GRADE_ORDER[r._grade] >= 1).length
  const quality = list.filter((r) => GRADE_ORDER[r._grade] >= 2).length
  const med = (values: number[]) => {
    const sorted = values.filter((v) => Number.isFinite(v)).sort((a, b) => a - b)
    return sorted.length ? sorted[Math.floor(sorted.length / 2)] : null
  }
  return {
    total: list.length,
    valid, validPct: `${((valid / total) * 100).toFixed(0)}% 有效`,
    good, quality,
    medianIc: med(list.map((r) => Math.abs(Number(r.mean_ic) || 0))),
    medianIcir: med(list.map((r) => Math.abs(Number(r.ic_ir) || 0))),
  }
})

const cacheWindow = computed(() => {
  const dates = data.value?.dates || []
  return dates.length ? `${dates[0]} → ${dates[dates.length - 1]}` : '—'
})

/** 批量入池的目标：当前筛选（等级 + 关键字）命中的因子 */
const batchTarget = computed(() => ({ factors: filtered.value.map((r) => r.factor) }))

function sortBy(key: typeof sortKey.value) {
  if (sortKey.value === key) sortAsc.value = !sortAsc.value
  else { sortKey.value = key; sortAsc.value = key === 'factor' }
}

function arrow(key: string) {
  return sortKey.value === key ? (sortAsc.value ? '▲' : '▼') : ''
}

function fmt(v: any, digits: number) {
  if (v === null || v === undefined) return '—'
  const n = Number(v)
  return Number.isFinite(n) ? n.toFixed(digits) : '—'
}

function tone(v: number | null | undefined) {
  if (v === null || v === undefined) return ''
  const n = Number(v)
  if (!Number.isFinite(n)) return ''
  return n > 0 ? 'up' : n < 0 ? 'down' : ''
}

// ── 图表 ────────────────────────────────────────────────────────────────────
function createLcChart(container: HTMLDivElement): IChartApi {
  return createChart(container, {
    layout: { background: { type: ColorType.Solid, color: '#11161d' }, textColor: '#a3a8af' },
    grid: { vertLines: { color: 'rgba(42,46,57,0.4)' }, horzLines: { color: 'rgba(42,46,57,0.4)' } },
    rightPriceScale: { borderColor: 'rgba(197,203,206,0.3)' },
    timeScale: { borderColor: 'rgba(197,203,206,0.3)', timeVisible: false },
    crosshair: { vertLine: { color: 'rgba(255,209,102,0.5)', width: 1, style: 2 },
                 horzLine: { color: 'rgba(255,209,102,0.3)', width: 1, style: 2 } },
    handleScroll: { vertTouchDrag: false },
  })
}

function renderSeries(
  target: 'ic' | 'rank',
  container: HTMLDivElement | null,
  chartRef: { value: IChartApi | null },
  source: 'ic_series' | 'rank_ic_series',
  seriesMap: Map<string, ISeriesApi<'Line'>>,
) {
  if (!data.value || !container) return
  if (chartRef.value) { chartRef.value.remove(); seriesMap.clear() }
  const chart = createLcChart(container)
  chartRef.value = chart
  container.style.height = '320px'
  data.value.factors.forEach((f, i) => {
    const series = chart.addLineSeries({ color: LINE_COLORS[i % LINE_COLORS.length], lineWidth: LINE_WIDTH })
    const points: LineData[] = []
    const values = (data.value![source] as Record<string, (number | null)[]>)[f] ?? []
    data.value!.dates.forEach((d, j) => {
      const v = values[j]
      if (v !== null && v !== undefined) points.push({ time: d as Time, value: v })
    })
    series.setData(points)
    seriesMap.set(f, series)
  })
  chart.timeScale().fitContent()
  void target
}

const icSeriesMap = new Map<string, ISeriesApi<'Line'>>()
const rankSeriesMap = new Map<string, ISeriesApi<'Line'>>()
const icChartHolder = { value: null as IChartApi | null }
const rankChartHolder = { value: null as IChartApi | null }

async function fetch() {
  loading.value = true
  try {
    data.value = await getFactorIC(factorSet.value, horizon.value, topN.value)
    error.value = ''
    await nextTick()
    renderSeries('ic', icChartRef.value, icChartHolder, 'ic_series', icSeriesMap)
    renderSeries('rank', rankChartRef.value, rankChartHolder, 'rank_ic_series', rankSeriesMap)
    icChart = icChartHolder.value
    rankChart = rankChartHolder.value
  } catch (e) {
    error.value = String(e)
  } finally {
    loading.value = false
  }
}

// ── 入池：因子 → 策略池候选（kind=factor_candidate，不进生产）────────────────
async function poolOne(row: Row) {
  pooling.value = true
  poolMsg.value = ''
  try {
    const res = await registerStrategyCandidate({
      id: `factor-${factorSet.value.replace('qlib_', '')}-h${horizon.value}-${row.factor}`,
      name: `因子候选：${row.factor}`,
      kind: 'factor_candidate',
      origin: 'factor_ic_audit',
      owner: 'factor_ic',
      stage: 'candidate',
      summary: `${data.value?.factor_set} h${data.value?.horizon} ｜ 等级 ${row._grade} ｜ ICIR 分档 ${icirBand(row.ic_ir)}`,
      entry: `uv run python scripts/ic_rubric_audit.py（月频秩 IC 复核）`,
      metrics: {
        mean_ic: Number(row.mean_ic), std_ic: Number(row.std_ic), ic_ir: Number(row.ic_ir),
        mean_rank_ic: Number(row.mean_rank_ic), ic_positive_rate: Number(row.ic_positive_rate),
        grade: row._grade, horizon: horizon.value,
      },
      artifacts: [`output/factor_ic_cache_${data.value?.factor_set}_h${horizon.value}.pkl`],
      evidence: ['output/research/ic_rubric_audit/REPORT.md'],
    })
    poolMsg.value = `已入池 ${res.id}`
  } catch (e: any) {
    poolMsg.value = `入池失败：${e?.message || e}`
  } finally {
    pooling.value = false
  }
}

async function poolBatch() {
  const names = batchTarget.value.factors
  if (!names.length) return
  pooling.value = true
  poolMsg.value = ''
  try {
    const res = await registerStrategyCandidate({
      id: `factor-cohort-${factorSet.value.replace('qlib_', '')}-h${horizon.value}-${gradeFilter.value}`,
      name: `因子池批次：${factorSet.value} h${horizon.value} · ${gradeFilter.value}及以上`,
      kind: 'factor_cohort',
      origin: 'factor_ic_audit',
      owner: 'factor_ic',
      stage: 'candidate',
      summary: `${names.length} 个因子：${names.slice(0, 24).join('、')}${names.length > 24 ? ' …' : ''}`,
      entry: 'uv run python scripts/ic_rubric_audit.py（月频秩 IC 复核后再决定是否进多因子池）',
      metrics: {
        n_factors: names.length, horizon: horizon.value,
        median_abs_ic: stats.value.medianIc, median_abs_icir: stats.value.medianIcir,
        grade_floor: gradeFilter.value,
      },
      artifacts: [`output/factor_ic_cache_${data.value?.factor_set}_h${horizon.value}.pkl`],
      evidence: ['output/research/ic_rubric_audit/REPORT.md'],
    })
    poolMsg.value = `已入池批次 ${res.id}（${names.length} 个因子）`
  } catch (e: any) {
    poolMsg.value = `入池失败：${e?.message || e}`
  } finally {
    pooling.value = false
  }
}

onMounted(fetch)
onUnmounted(() => { icChart?.remove(); rankChart?.remove() })
watch([factorSet, horizon, topN], () => { void fetch() })
</script>

<style scoped>
.picker { padding: 5px 10px; background: var(--bg-input); color: var(--text-primary);
  border: 1px solid var(--border-color); border-radius: var(--r-2); }
.link-btn { text-decoration: none; display: inline-flex; align-items: center; }
.panel h6 { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.tile-row { display: flex; gap: var(--sp-4); flex-wrap: wrap; margin-bottom: var(--sp-3); }
.tile { display: flex; flex-direction: column; gap: 1px; min-width: 108px; }
.tile.wide { min-width: 220px; }
.tile label { font-size: 10px; color: var(--text-dim); letter-spacing: 0.04em; }
.tile b { font-size: var(--fs-lg); }
/* 计数用语义色（状态），不要用涨跌色：绿=状态正常，红/绿在 A 股里另有含义 */
.tile b.ok { color: var(--state-ok); }
.tile b.gold { color: var(--accent-gold); }
.rubric { display: flex; gap: var(--sp-5); flex-wrap: wrap; border-top: 1px solid var(--border-color);
  padding-top: var(--sp-2); }
.rubric-col { min-width: 200px; }
.rubric-col.wide { flex: 1; min-width: 320px; }
.rubric-col h6 { font-size: var(--fs-xs); color: var(--text-muted); margin-bottom: 4px; }
.rubric-col ul { list-style: none; margin: 0; padding: 0; font-size: var(--fs-xs); color: var(--text-muted); }
.rubric-col li { display: flex; gap: 6px; align-items: center; padding: 1px 0; }
.grade { padding: 0 6px; border-radius: var(--r-pill); font-size: 10px; border: 1px solid var(--border-color);
  white-space: nowrap; }
.g-invalid { color: var(--text-dim); }
.g-pass { color: var(--accent-blue); border-color: rgba(17, 138, 178, 0.5); }
.g-good { color: var(--accent-gold); border-color: rgba(255, 209, 102, 0.5); }
.g-hot { color: var(--accent-orange); border-color: rgba(255, 159, 28, 0.5); }
.charts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-3); margin-bottom: var(--sp-3); }
.chart-panel { background: var(--bg-card); border: 1px solid var(--border-color);
  border-radius: var(--r-3); padding: var(--sp-2) var(--sp-3); }
.chart-panel h6 { font-size: var(--fs-xs); color: var(--text-muted); margin-bottom: var(--sp-1); }
.toolbar-row { display: flex; gap: var(--sp-2); align-items: center; flex-wrap: wrap; margin-bottom: var(--sp-2); }
.search { flex: 1; min-width: 180px; max-width: 320px; padding: 5px 8px; background: var(--bg-input);
  border: 1px solid var(--border-color); border-radius: var(--r-2); color: var(--text-primary); }
.data-table th { cursor: default; }
.data-table thead th:nth-child(-n+2) { cursor: pointer; }
.data-table td.left, .data-table th.left { text-align: left; }
.data-table tr.hot { background: rgba(255, 159, 28, 0.05); }
.btn-xs { padding: 1px 8px; font-size: 10px; }
.up { color: var(--accent-up); }
.down { color: var(--accent-down); }
.mono { font-family: var(--font-mono); }
@media (max-width: 1100px) { .charts-grid { grid-template-columns: 1fr; } }
</style>
