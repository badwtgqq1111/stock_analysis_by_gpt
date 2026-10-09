<template>
  <div class="page">
    <PageHeader
      title="回测绩效"
      description="逐笔级回放引擎的组合净值 + 中证全指基准 → 与聚宽「收益概述」同口径的一套指标。数据来自 <code>output/results_cn/performance/</code>，前端只做展示。"
    >
      <template #actions>
        <select v-model="tag" class="stock-select">
          <option v-for="a in summary?.arms || []" :key="a.tag" :value="a.tag">{{ a.tag }}</option>
        </select>
        <button class="btn-ghost" :disabled="loading" @click="reload">刷新</button>
      </template>
    </PageHeader>

    <StateBlock v-if="error" state="error" title="取不到绩效数据" :detail="error" :retry="reload" />
    <StateBlock v-else-if="!detail" state="loading" title="加载中…" />

    <template v-else>
      <!-- 「这是什么」—— 这页此前只有一堆指标，第一反应会是"这是买的指数基金吗"。
           把口径写在最上面，先说清"策略是模拟账户、基准是指数本身、都不是基金"。 -->
      <div class="panel explain" v-if="detail.explain">
        <h6>{{ detail.explain.title }}</h6>
        <ul>
          <li v-html="md(detail.explain.strategy)"></li>
          <li v-html="md(detail.explain.benchmark)"></li>
          <li v-html="md(detail.explain.execution)"></li>
          <li v-html="md(detail.explain.how_to_read)"></li>
        </ul>
        <p class="warn-text tiny">⚠️ {{ detail.explain.not_what }}</p>
      </div>

      <div class="panel">
        <div class="pill-row">
          <span class="pill">区间 <b>{{ detail.window[0] }} ~ {{ detail.window[1] }}</b></span>
          <span class="pill"><b>{{ detail.days }}</b> 个交易日</span>
          <span class="pill">基准 <b>{{ detail.benchmark.name }}（{{ detail.benchmark.code }}）</b></span>
          <span class="pill" v-if="detail.account">
            <b>{{ money(detail.account.initial_capital) }}</b> →
            <b class="up">{{ money(detail.account.final_capital) }}</b>
          </span>
          <span class="pill">来源 <b class="mono">{{ detail.source }}</b></span>
          <span class="pill">交易明细 <b>{{ detail.trades?.length ?? 0 }}</b> 笔</span>
        </div>

        <div class="metrics">
          <div v-for="m in detail.metrics" :key="m.key" class="metric" :class="toneOf(m)">
            <div class="m-label" :title="m.note || m.label">{{ m.label }}</div>
            <div class="m-value">{{ fmt(m) }}</div>
          </div>
        </div>
      </div>

      <div class="panel">
        <div class="tabs">
          <button v-for="t in tabs" :key="t.key" class="tab"
                  :class="{ active: view === t.key }" @click="view = t.key">{{ t.label }}</button>
        </div>

        <!-- 收益概述 -->
        <template v-if="view === 'overview'">
          <h6>净值曲线（归一化）</h6>
          <SvgCurve :series="navSeries" :height="260" />
          <h6 class="mt">超额曲线（策略 / 基准）</h6>
          <SvgCurve :series="excessSeries" :height="180" />
          <h6 class="mt">回撤 (%)</h6>
          <SvgCurve :series="ddSeries" :height="150" />
        </template>

        <!-- 交易明细 -->
        <template v-else-if="view === 'trades'">
          <h6>交易明细（{{ shownTrades.length }} / {{ detail.trades?.length ?? 0 }} 笔）</h6>
          <div class="table-scroll">
            <table class="data-table">
              <thead><tr><th>日期</th><th>代码</th><th>方向</th><th>价格</th><th>股数</th>
                <th>金额</th><th class="text-left">原因</th></tr></thead>
              <tbody>
                <tr v-for="(t, i) in shownTrades" :key="i">
                  <td class="mono">{{ t.date }}</td>
                  <td class="mono link" @click="gotoStock(t.code)">{{ t.code }}</td>
                  <td :class="t.side === 'buy' ? 'up' : 'down'">{{ t.side === 'buy' ? '买入' : '卖出' }}</td>
                  <td>{{ t.price }}</td><td>{{ t.shares }}</td><td>{{ t.amount }}</td>
                  <td class="text-left dim tiny">{{ t.reason || '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>

        <!-- 性能分析（全部由已有曲线派生，不需要新数据）-->
        <template v-else-if="view === 'analysis'">
          <h6>月度收益（%）</h6>
          <table class="data-table monthly">
            <thead><tr><th>年</th><th v-for="m in 12" :key="m">{{ m }}月</th><th>全年</th></tr></thead>
            <tbody>
              <tr v-for="row in monthly" :key="row.year">
                <td class="mono">{{ row.year }}</td>
                <td v-for="(v, i) in row.months" :key="i" :class="v == null ? 'dim' : v >= 0 ? 'up' : 'down'">
                  {{ v == null ? '—' : (v * 100).toFixed(1) }}
                </td>
                <td :class="row.total >= 0 ? 'up' : 'down'"><b>{{ (row.total * 100).toFixed(1) }}</b></td>
              </tr>
            </tbody>
          </table>

          <h6 class="mt">滚动 60 日年化夏普</h6>
          <SvgCurve :series="rollingSharpe" :height="160" />
          <h6 class="mt">滚动 60 日年化波动率（%）</h6>
          <SvgCurve :series="rollingVol" :height="140" />

          <h6 class="mt">日收益分布（%）</h6>
          <div class="hist">
            <div v-for="b in histogram" :key="b.label" class="hist-col" :title="`${b.label}%：${b.n} 天`">
              <div class="hist-bar" :style="{ height: (b.n / maxHist * 100) + '%' }"
                   :class="b.mid >= 0 ? 'up-bg' : 'down-bg'" />
              <span class="dim tiny">{{ b.label }}</span>
            </div>
          </div>
          <p class="hint tiny">共 {{ dailyReturns.length }} 个交易日；最大单日 {{ pctMax }}，最小 {{ pctMin }}。</p>
        </template>

        <!-- 每日持仓 & 收益 -->
        <template v-else>
          <h6>每日持仓 &amp; 收益（持仓只数由交易明细重建）</h6>
          <div class="table-scroll">
            <table class="data-table">
              <thead><tr><th>日期</th><th>净值</th><th>当日收益</th><th>持仓只数</th><th>当日成交</th><th>回撤</th></tr></thead>
              <tbody>
                <tr v-for="r in dailyRows" :key="r.date">
                  <td class="mono">{{ r.date }}</td>
                  <td>{{ r.nav.toFixed(4) }}</td>
                  <td :class="r.ret >= 0 ? 'up' : 'down'">{{ (r.ret * 100).toFixed(2) }}%</td>
                  <td>{{ r.positions }}</td>
                  <td>{{ r.trades || '—' }}</td>
                  <td class="down">{{ (r.drawdown * 100).toFixed(2) }}%</td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { getPerformanceDetail, getPerformanceSummary } from '@/api/client'
import type { PerformanceDetail, PerformanceMetric, PerformanceSummary } from '@/types/api'
import PageHeader from '@/components/ui/PageHeader.vue'
import StateBlock from '@/components/ui/StateBlock.vue'
import SvgCurve from '@/components/ui/SvgCurve.vue'

const router = useRouter()
const summary = ref<PerformanceSummary | null>(null)
const detail = ref<PerformanceDetail | null>(null)
const tag = ref('base_v1')
const view = ref<'overview' | 'trades' | 'daily' | 'analysis'>('overview')
const loading = ref(false)
const error = ref('')

const tabs = [
  { key: 'overview', label: '收益概述' },
  { key: 'trades', label: '交易明细' },
  { key: 'daily', label: '每日持仓 & 收益' },
  { key: 'analysis', label: '性能分析' },
] as const

async function reload() {
  loading.value = true
  try {
    if (!summary.value) summary.value = await getPerformanceSummary()
    detail.value = await getPerformanceDetail(tag.value)
    error.value = ''
  } catch (e) {
    error.value = String(e)
  } finally {
    loading.value = false
  }
}
onMounted(reload)
watch(tag, () => { view.value = 'overview'; void reload() })

function gotoStock(code: string) { if (code) router.push({ path: '/kline', query: { code } }) }

/** 极简 markdown：只处理 **加粗**（产物里的说明文本用了它） */
function md(text: string): string {
  return String(text || '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
    .replace(/\*(.+?)\*/g, '<i>$1</i>')
}
function money(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—'
  return `${Math.round(Number(v)).toLocaleString('zh-CN')} 元`
}

function fmt(m: PerformanceMetric): string {
  const v = m.value
  if (v === null || v === undefined || (typeof v === 'number' && Number.isNaN(v))) return '—'
  if (m.fmt === 'pct' || m.fmt === 'pct_raw') return `${(Number(v) * 100).toFixed(2)}%`
  if (m.fmt === 'int') return String(v)
  if (m.fmt === 'money') return money(Number(v))
  if (m.fmt === 'text') return String(v)
  return Number(v).toFixed(3)
}
function toneOf(m: PerformanceMetric): string {
  if (m.fmt === 'text' || m.fmt === 'int') return ''
  const v = Number(m.value)
  if (Number.isNaN(v)) return ''
  if (['max_drawdown', 'excess_max_drawdown', 'vol_strategy', 'vol_benchmark'].includes(m.key)) return 'bad'
  if (['total_return', 'ann_return', 'excess_return', 'bench_return', 'alpha'].includes(m.key)) return v >= 0 ? 'up' : 'down'
  return ''
}

type Curve = { date: string; nav: number; bench: number; excess: number; drawdown: number }[]
const curve = computed<Curve>(() => (detail.value?.curve ?? []) as Curve)

const navSeries = computed(() => [
  { name: '策略', color: '#118ab2', points: curve.value.map((c) => c.nav) },
  { name: '基准 ' + (detail.value?.benchmark?.name ?? ''), color: '#ef476f', points: curve.value.map((c) => c.bench) },
])
const excessSeries = computed(() => [
  { name: '超额（策略/基准）', color: '#ffd166', points: curve.value.map((c) => c.excess) },
])
const ddSeries = computed(() => [
  { name: '回撤', color: '#ef476f', points: curve.value.map((c) => c.drawdown * 100) },
])

const shownTrades = computed(() => [...(detail.value?.trades ?? [])].reverse().slice(0, 500))

const dailyReturns = computed(() => {
  const c = curve.value
  const out: number[] = []
  for (let i = 1; i < c.length; i++) if (c[i - 1].nav) out.push(c[i].nav / c[i - 1].nav - 1)
  return out
})
const pctMax = computed(() => (Math.max(...dailyReturns.value, 0) * 100).toFixed(2) + '%')
const pctMin = computed(() => (Math.min(...dailyReturns.value, 0) * 100).toFixed(2) + '%')

/** 月度收益：按自然月把日收益连乘 */
const monthly = computed(() => {
  const c = curve.value
  const byMonth: Record<string, number[]> = {}
  for (let i = 1; i < c.length; i++) {
    const key = c[i].date.slice(0, 7)
    ;(byMonth[key] ||= []).push(c[i - 1].nav ? c[i].nav / c[i - 1].nav - 1 : 0)
  }
  const years: Record<string, (number | null)[]> = {}
  for (const [key, rets] of Object.entries(byMonth)) {
    const [y, m] = key.split('-')
    const total = rets.reduce((acc, r) => acc * (1 + r), 1) - 1
    ;(years[y] ||= new Array(12).fill(null))[Number(m) - 1] = total
  }
  return Object.entries(years).sort().map(([year, months]) => ({
    year, months,
    // 显式标 acc 类型：months 是 (number|null)[]，reduce 会把 acc 也推断成可空
    total: months.reduce<number>((acc, v) => acc * (1 + (v ?? 0)), 1) - 1,
  }))
})

function rolling(nums: number[], win: number, fn: (w: number[]) => number) {
  const out: number[] = []
  for (let i = 0; i < nums.length; i++) {
    if (i + 1 < win) { out.push(NaN); continue }
    out.push(fn(nums.slice(i + 1 - win, i + 1)))
  }
  return out
}
const rollingSharpe = computed(() => [{
  name: '滚动 60 日夏普', color: '#118ab2',
  points: rolling(dailyReturns.value, 60, (w) => {
    const m = w.reduce((a, b) => a + b, 0) / w.length
    const sd = Math.sqrt(w.reduce((a, b) => a + (b - m) ** 2, 0) / Math.max(w.length - 1, 1))
    return sd ? (m / sd) * Math.sqrt(252) : NaN
  }),
}])
const rollingVol = computed(() => [{
  name: '滚动 60 日年化波动率', color: '#ff9f1c',
  points: rolling(dailyReturns.value, 60, (w) => {
    const m = w.reduce((a, b) => a + b, 0) / w.length
    const sd = Math.sqrt(w.reduce((a, b) => a + (b - m) ** 2, 0) / Math.max(w.length - 1, 1))
    return sd * Math.sqrt(252) * 100
  }),
}])

/** 日收益直方图：±5% 之间分 20 档，超出并入两端 */
const histogram = computed(() => {
  const bins = new Array(20).fill(0)
  for (const r of dailyReturns.value) {
    const pct = r * 100
    const idx = Math.min(19, Math.max(0, Math.floor((pct + 5) / 0.5)))
    bins[idx] += 1
  }
  return bins.map((n, i) => ({
    n, mid: -5 + i * 0.5 + 0.25,
    label: (-5 + i * 0.5).toFixed(1),
  }))
})
const maxHist = computed(() => Math.max(...histogram.value.map((b) => b.n), 1))

/** 由交易明细重建每日持仓只数（买 +1 / 卖 -1），再与日频净值对齐 */
const dailyRows = computed(() => {
  const c = curve.value
  const trades = detail.value?.trades ?? []
  const byDate: Record<string, number> = {}
  for (const t of trades) byDate[t.date] = (byDate[t.date] || 0) + 1
  const pos: Record<string, number> = {}
  const delta: Record<string, number> = {}
  for (const t of trades) delta[t.date] = (delta[t.date] || 0) + (t.side === 'buy' ? 1 : -1)
  let acc = 0
  return c.map((row, i) => {
    acc += delta[row.date] || 0
    pos[row.date] = acc
    const prev = i > 0 ? c[i - 1].nav : row.nav
    return { date: row.date, nav: row.nav, ret: prev ? row.nav / prev - 1 : 0,
             positions: acc, trades: byDate[row.date] || 0, drawdown: row.drawdown }
  })
})
</script>

<style scoped>
.stock-select { padding: 5px 10px; background: var(--bg-input); color: var(--text-primary);
  border: 1px solid var(--border-color); border-radius: var(--r-2); }
.metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(132px, 1fr)); gap: var(--sp-2); }
.metric { background: var(--bg-input); border: 1px solid var(--border-color);
  border-radius: var(--r-3); padding: var(--sp-2) var(--sp-3); }
.metric .m-label { font-size: var(--fs-xs); color: var(--text-muted); margin-bottom: 2px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.metric .m-value { font-size: var(--fs-xl); font-weight: 700; }
.metric.up .m-value { color: var(--accent-up); }
.metric.down .m-value { color: var(--accent-down); }
.metric.bad .m-value { color: var(--accent-up); }
.mt { margin-top: var(--sp-3); }
.monthly td { font-size: var(--fs-xs); padding: 4px 6px; }
.hist { display: flex; align-items: flex-end; gap: 3px; height: 120px; }
.hist-col { flex: 1; display: flex; flex-direction: column; justify-content: flex-end;
  align-items: center; height: 100%; }
.hist-bar { width: 100%; border-radius: 2px 2px 0 0; min-height: 1px; }
.up-bg { background: var(--accent-up); opacity: 0.75; }
.down-bg { background: var(--accent-down); opacity: 0.75; }
.explain ul { margin: 0; padding-left: 18px; }
.explain li { font-size: var(--fs-sm); color: var(--text-primary); line-height: 1.75; margin-bottom: 4px; }
.explain b { color: var(--accent-gold); }
</style>
