<template>
  <div class="page">
    <h4 class="page-title">选股日历 · 历史选股与收益回顾</h4>
    <p class="hint">
      台账 <code>output/results_cn/selection_ledger.parquet</code>（构建：
      <code>uv run python scripts/build_selection_ledger.py</code>，生成于 {{ summary?.ledger_mtime || '—' }}）。
      收益 = 决策日后 1/5/10/20 个交易日的 qfq 收盘收益；超额 = 减去当日全市场等权平均。
    </p>

    <div v-if="summary && !summary.built" class="panel">
      <div class="empty">台账还没建：{{ summary.hint }}</div>
    </div>

    <template v-else-if="summary">
      <div class="toolbar">
        <button class="btn-ghost" @click="shiftMonth(-1)">◀ 上月</button>
        <b class="month-label">{{ month }}</b>
        <button class="btn-ghost" @click="shiftMonth(1)">下月 ▶</button>
        <select v-model="source" @change="onSourceChange">
          <option value="">全部来源</option>
          <option v-for="s in summary.sources" :key="s" :value="s">{{ s }}</option>
        </select>
        <span class="meta">记录 {{ totalRecords }} 条 / {{ summary.dates.length }} 个决策日 ·
          {{ sourceCountsText }}</span>
      </div>

      <div class="panel">
        <h6>月历（格子里的数字 = 当日选股数；底色 = 该日组合 T+5 平均超额）</h6>
        <div class="calendar">
          <div v-for="w in weekdayLabels" :key="w" class="weekday">{{ w }}</div>
          <div v-for="cell in calendarCells" :key="cell.key" class="cell"
               :class="{ blank: !cell.date, selected: cell.day?.date === selectedDate, has: cell.day,
                         idle: cell.date && !cell.day }"
               @click="cell.day && loadDay(cell.day.date)">
            <template v-if="cell.date">
              <div class="cell-top">
                <span class="dnum">{{ cell.date.slice(8) }}</span>
                <span v-if="cell.day" class="n">×{{ cell.day.n }}</span>
              </div>
              <template v-if="cell.day">
                <div class="bar" :style="{ background: color(cell.day.mean_excess_r5) }"></div>
                <div class="cell-mid">
                  <span :class="tone(cell.day.mean_excess_r5)">{{ pct(cell.day.mean_excess_r5) }}</span>
                  <span class="src">{{ sourceDots(cell.day.per_source) }}</span>
                </div>
              </template>
              <div v-else class="cell-idle">无选股</div>
            </template>
          </div>
        </div>
        <div class="legend">
          <span><i class="sw gain"></i>超额 &gt; 0</span>
          <span><i class="sw loss"></i>超额 &lt; 0</span>
          <span><i class="sw none"></i>无收益数据（未来交易日不足）</span>
          <span>来源标记：A=arm（研究手臂） N=notify（PK 后最终名单） P=preselected（归档候选池） R=survey_raw G=survey_gate</span>
        </div>
      </div>

      <div class="panel" v-if="day">
        <h6>{{ day.date }} 选股明细（{{ day.compare?.length ?? 0 }} 只票 / {{ day.count }} 行；来源 {{ sourceDots(day.per_source) }}）
          <span class="empty tiny">· 阶段/风险按 <b>该决策日之前</b>的行情判定；点代码或名称跳个股页（那里按最新交易日判定）</span>
        </h6>
        <div class="pill-row">
          <span class="pill" v-for="h in horizons" :key="h">
            T+{{ h }} 均值 <b :class="tone(day.mean['r' + h])">{{ pct(day.mean['r' + h]) }}</b>
            · 超额 <b :class="tone(day.mean_excess['r' + h])">{{ pct(day.mean_excess['r' + h]) }}</b>
          </span>
        </div>

        <div class="tabs">
          <button class="tab" :class="{ active: dayView === 'compare' }" @click="dayView = 'compare'">
            多策略对比（{{ day.compare?.length ?? 0 }} 只）
          </button>
          <button class="tab" :class="{ active: dayView === 'list' }" @click="dayView = 'list'">
            明细列表（{{ day.count }} 行）
          </button>
        </div>

        <!-- 多策略对比：同一只票只占一行，每个策略一列。
             平铺列表里 preselected(候选池) 与 notify(PK 后最终名单) 往往是同一批票，
             看起来就是"重复"（实测 2026-10-08：8 只票 × 2 个源 = 16 行）。 -->
        <table class="data-table" v-if="dayView === 'compare'">
          <thead>
            <tr>
              <th>代码</th><th>名称</th><th>行业</th>
              <th v-for="st in day.strategies || []" :key="st" :title="st">{{ sourceShort(st) }}</th>
              <th>阶段</th><th>T+1</th><th>T+5</th><th>T+20</th><th>超额T+5</th><th>入选原因</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="c in day.compare || []" :key="c.stock_code"
                :class="{ active: c.stock_code === selectedCode }"
                @mouseenter="selectedCode = c.stock_code">
              <td class="mono link" @click="gotoStock(c.stock_code)">{{ c.stock_code }}</td>
              <td class="left link" @click="gotoStock(c.stock_code)">{{ c.name || '—' }}</td>
              <td class="left tiny">{{ shortIndustry(c.industry) }}</td>
              <td v-for="st in day.strategies || []" :key="st" :class="{ 'cell-in': c.cells[st] }">
                <template v-if="c.cells[st]">
                  <span class="tag gold">#{{ c.cells[st].rank ?? '—' }}</span>
                  <span class="tiny dim" v-if="c.cells[st].weight">
                    {{ (c.cells[st].weight * 100).toFixed(1) }}%
                  </span>
                </template>
                <span v-else class="dim">·</span>
              </td>
              <td class="left"><span class="stage-chip" :class="'stage-' + (c.stage || '')"
                    :title="c.stage_evidence">{{ c.stage_label || '—' }}</span></td>
              <td :class="tone(c.r1)">{{ pct(c.r1) }}</td>
              <td :class="tone(c.r5)">{{ pct(c.r5) }}</td>
              <td :class="tone(c.r20)">{{ pct(c.r20) }}</td>
              <td :class="tone(c.excess_r5)">{{ pct(c.excess_r5) }}</td>
              <td class="left multiline" :title="c.reason">{{ c.reason || '—' }}</td>
            </tr>
          </tbody>
        </table>

        <!-- 明细列表整块包进 <template v-else>：
             关系说明的 <p> **不能**插在 v-if 与 v-else 之间 —— 那会打断 Vue 的
             v-if/v-else 链（第一版就是这么写的，页面上只能看到一个残缺的分支）。 -->
        <template v-else>
        <p v-if="relationNote" class="hint tiny relation-note">{{ relationNote }}</p>

        <table class="data-table">
          <thead><tr><th>来源</th><th>代码</th><th>名称</th><th>阶段</th><th>入选原因</th><th>风险描述</th>
            <th>rank</th><th>权重</th><th>手数</th>
            <th>T+1</th><th>T+5</th><th>T+10</th><th>T+20</th><th>超额T+5</th></tr></thead>
          <tbody>
            <tr v-for="(r, i) in day.rows" :key="i" :class="{ active: r.stock_code === selectedCode }"
                @mouseenter="selectedCode = r.stock_code">
              <td class="mono">{{ r.source }}
                <span v-if="alsoIn(r)" class="tag dim" :title="'同一只票也出现在：' + alsoIn(r)">同 {{ alsoIn(r) }}</span>
              </td>
              <td class="mono link" @click="gotoStock(r.stock_code)">{{ r.stock_code }}</td>
              <td class="left link" @click="gotoStock(r.stock_code)">{{ r.name || '—' }}</td>
              <td class="left"><span class="stage-chip" :class="'stage-' + r.stage"
                    :title="r.stage_evidence">{{ r.stage_label || '—' }}</span></td>
              <td class="left multiline" :title="r.reason">{{ r.reason || '—' }}</td>
              <td class="left multiline" :title="r.risk + ' ｜ ' + r.risk_notes">
                <div>{{ r.risk || '—' }}</div>
                <div class="subtle" v-if="r.risk_notes && r.risk_notes !== '—'">{{ r.risk_notes }}</div>
              </td>
              <td>{{ r.rank ?? '—' }}</td>
              <td>{{ r.weight != null ? (r.weight * 100).toFixed(1) + '%' : '—' }}</td>
              <td>{{ r.lots ?? '—' }}</td>
              <td :class="tone(r.r1)">{{ pct(r.r1) }}</td>
              <td :class="tone(r.r5)">{{ pct(r.r5) }}</td>
              <td :class="tone(r.r10)">{{ pct(r.r10) }}</td>
              <td :class="tone(r.r20)">{{ pct(r.r20) }}</td>
              <td :class="tone(r.excess_r5)">{{ pct(r.excess_r5) }}</td>
            </tr>
          </tbody>
        </table>
        </template>
      </div>

      <div class="panel">
        <h6>阶段 → 后续表现（判断"这个阶段到底值不值得买"）</h6>
        <table class="data-table">
          <thead><tr><th>阶段</th><th>样本</th><th>T+5 均值</th><th>T+5 超额</th><th>T+5 胜率</th><th>T+20 均值</th></tr></thead>
          <tbody>
            <tr v-for="s in perf?.by_stage || []" :key="s.stage_label">
              <td class="left"><span class="stage-chip" :class="'stage-' + stageKey(s.stage_label)">{{ s.stage_label }}</span></td>
              <td>{{ s.n }}</td>
              <td :class="tone(s.mean_r5)">{{ pct(s.mean_r5) }}</td>
              <td :class="tone(s.mean_excess_r5)">{{ pct(s.mean_excess_r5) }}</td>
              <td>{{ s.win_rate_r5 != null ? (s.win_rate_r5 * 100).toFixed(0) + '%' : '—' }}</td>
              <td :class="tone(s.mean_r20)">{{ pct(s.mean_r20) }}</td>
            </tr>
          </tbody>
        </table>
        <div class="empty tiny">
          阶段是"状态描述"（规则见 factor_engine/trend_stage.py：均线排列+斜率+52周分位+量能），
          不是买卖信号；本仓库 state_probability 研究实测过"状态不足以单独预测收益"。
        </div>
      </div>

      <div class="panel">
        <h6>各来源覆盖与表现（口径不同，别混着看）</h6>
        <table class="data-table">
          <thead><tr><th>来源</th><th>记录</th><th>决策日</th><th>区间</th><th>可评估</th>
            <th>T+1 均值</th><th>T+5 均值</th><th>T+5 超额</th><th>T+5 胜率</th></tr></thead>
          <tbody>
            <tr v-for="s in perf?.by_source || []" :key="s.source"
                :class="{ current: s.source === source }">
              <td class="mono">{{ s.source }}</td>
              <td>{{ s.n_picks }}</td><td>{{ s.n_dates }}</td>
              <td class="mono">{{ s.date_range?.join(' ~ ') }}</td>
              <td>{{ s.n_evaluated }}</td>
              <td :class="tone(s.mean_r1)">{{ pct(s.mean_r1) }}</td>
              <td :class="tone(s.mean_r5)">{{ pct(s.mean_r5) }}</td>
              <td :class="tone(s.mean_excess_r5)">{{ pct(s.mean_excess_r5) }}</td>
              <td>{{ s.win_rate_r5 != null ? (s.win_rate_r5 * 100).toFixed(0) + '%' : '—' }}</td>
            </tr>
          </tbody>
        </table>
        <div class="empty tiny">
          notify = 当日最终名单（覆盖天数最少，最接近账户实盘）；preselected = 归档候选池（跨度最长）；
          survey_raw / survey_gate = 研究回看名单（门禁前后）。
          近期日期 T+5/T+10/T+20 为"—"是未来交易日不足，不是缺失。
        </div>
      </div>

      <div class="panel">
        <h6>业绩回顾（{{ perf?.source === 'all' ? '全部来源' : perf?.source }}）：按决策日等权，
          期与期之间按顺序复利</h6>
        <div class="pill-row" v-if="perf?.overall">
          <span class="pill">选股 <b>{{ perf.overall.n_picks }}</b> 次 / {{ perf.overall.n_dates }} 个决策日</span>
          <span class="pill">可评估 <b>{{ perf.overall.n_evaluated }}</b></span>
          <span class="pill" v-for="h in horizons" :key="h">
            T+{{ h }} 胜率 <b :class="tone(perf.overall['mean_r' + h])">
              {{ perf.overall['win_rate_r' + h] != null ? (perf.overall['win_rate_r' + h] * 100).toFixed(0) + '%' : '—' }}</b>
            · 均值 <b :class="tone(perf.overall['mean_r' + h])">{{ pct(perf.overall['mean_r' + h]) }}</b>
            · 超额 <b :class="tone(perf.overall['mean_excess_r' + h])">{{ pct(perf.overall['mean_excess_r' + h]) }}</b>
          </span>
          <span class="pill">累计倍数 <b>{{ perf.overall.equity_multiple }}</b> ·
            最大回撤 <b class="bad">{{ pct(perf.overall.max_drawdown) }}</b></span>
        </div>
        <svg v-if="curvePoints" class="curve" viewBox="0 0 800 180" preserveAspectRatio="none">
          <polyline :points="curvePoints" fill="none" stroke="#ffd166" stroke-width="2" />
          <line x1="0" :y1="baselineY" x2="800" :y2="baselineY" stroke="#2a2e39" stroke-width="1" />
        </svg>
        <table class="data-table">
          <thead><tr><th>决策日</th><th>只数</th><th>可评估</th><th>T+1</th><th>T+5</th><th>T+10</th><th>T+20</th>
            <th>超额T+5</th><th>T+5 胜率</th><th>期收益</th><th>累计净值</th></tr></thead>
          <tbody>
            <tr v-for="r in perf?.by_date || []" :key="r.date">
              <td class="mono">{{ r.date }}</td>
              <td>{{ r.n }}</td><td>{{ r.n_with_return }}</td>
              <td :class="tone(r.mean_r1)">{{ pct(r.mean_r1) }}</td>
              <td :class="tone(r.mean_r5)">{{ pct(r.mean_r5) }}</td>
              <td :class="tone(r.mean_r10)">{{ pct(r.mean_r10) }}</td>
              <td :class="tone(r.mean_r20)">{{ pct(r.mean_r20) }}</td>
              <td :class="tone(r.mean_excess_r5)">{{ pct(r.mean_excess_r5) }}</td>
              <td>{{ r.win_rate_r5 != null ? (r.win_rate_r5 * 100).toFixed(0) + '%' : '—' }}</td>
              <td>{{ equityMap[r.date] ? pct(equityMap[r.date].period_return) : '—' }}</td>
              <td>{{ equityMap[r.date] ? equityMap[r.date].equity.toFixed(4) : '—' }}</td>
            </tr>
          </tbody>
        </table>
        <div class="empty tiny" v-if="perf?.overall?.note">{{ perf.overall.note }}</div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  getCalendarDay, getCalendarMonth, getCalendarPerformance, getCalendarSummary,
  type CalendarDayBrief, type CalendarDayDetail, type CalendarMonth, type CalendarPerformance,
  type CalendarSummary,
} from '@/api/client'

const router = useRouter()
const route = useRoute()
const STATE_KEY = 'selection_calendar_state_v1'
const summary = ref<CalendarSummary | null>(null)
const monthData = ref<CalendarMonth | null>(null)
const day = ref<CalendarDayDetail | null>(null)
const perf = ref<CalendarPerformance | null>(null)
const month = ref('')
const source = ref('')
const dayView = ref<'compare' | 'list'>('compare')
const selectedDate = ref('')
const selectedCode = ref('')
const horizons = [1, 5, 10, 20]
const weekdayLabels = ['一', '二', '三', '四', '五', '六', '日']

const totalRecords = computed(() => Object.values(summary.value?.source_counts || {}).reduce((a, b) => a + b, 0))
const sourceCountsText = computed(() =>
  Object.entries(summary.value?.source_counts || {}).map(([k, v]) => `${k}:${v}`).join(' '))

const dayMap = computed(() => {
  const map: Record<string, CalendarDayBrief> = {}
  for (const d of monthData.value?.days || []) map[d.date] = d
  return map
})
const calendarCells = computed(() => {
  if (!month.value) return []
  const [y, m] = month.value.split('-').map(Number)
  const first = new Date(y, m - 1, 1)
  const startPad = (first.getDay() + 6) % 7          // 周一为第一列
  const daysInMonth = new Date(y, m, 0).getDate()
  const cells: { key: string; date?: string; day?: CalendarDayBrief }[] = []
  for (let i = 0; i < startPad; i++) cells.push({ key: `pad${i}` })
  for (let d = 1; d <= daysInMonth; d++) {
    const date = `${month.value}-${String(d).padStart(2, '0')}`
    cells.push({ key: date, date, day: dayMap.value[date] })
  }
  return cells
})
const equityMap = computed(() => {
  const map: Record<string, { equity: number; period_return: number }> = {}
  for (const p of perf.value?.curve || []) map[p.date] = p
  return map
})
const curvePoints = computed(() => {
  const curve = perf.value?.curve || []
  if (curve.length < 2) return ''
  const values = curve.map((c) => c.equity)
  const min = Math.min(...values), max = Math.max(...values)
  const span = max - min || 1
  return curve.map((c, i) => {
    const x = (i / (curve.length - 1)) * 800
    const y = 170 - ((c.equity - min) / span) * 150
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
})
const baselineY = 170

function pct(v: number | null | undefined) {
  if (v == null || !Number.isFinite(Number(v))) return '—'
  return (Number(v) * 100).toFixed(2) + '%'
}
function tone(v: number | null | undefined) {
  if (v == null || !Number.isFinite(Number(v))) return 'muted'
  return Number(v) > 0 ? 'ok' : (Number(v) < 0 ? 'bad' : 'muted')
}
function color(v: number | null | undefined) {
  if (v == null || !Number.isFinite(Number(v))) return 'rgba(42,46,57,0.6)'
  const a = Math.min(0.85, Math.abs(Number(v)) * 12 + 0.12)
  return Number(v) > 0 ? `rgba(0,212,170,${a})` : `rgba(239,71,111,${a})`
}
function gotoStock(code: string) {
  if (code) router.push({ path: '/kline', query: { code } })
}
const STAGE_KEYS: Record<string, string> = {
  '上涨趋势': 'uptrend', '下跌趋势': 'downtrend', '顶部区域（涨透/滞涨）': 'top_risk',
  '底部反转尝试': 'bottom_reversal', '趋势中段/震荡整理': 'mid_trend',
}
function stageKey(label: string) { return STAGE_KEYS[label] || 'mid_trend' }
/** 策略列的短标签：列窄，长名字会把表格挤爆 */
const SOURCE_SHORT: Record<string, string> = {
  notify: '最终名单', preselected: '候选池', arm: '研究手臂',
  survey_raw: '调研(原)', survey_gate: '调研(门禁)',
}
function sourceShort(src: string) { return SOURCE_SHORT[src] || src }
/** 行业名很长（"C29橡胶和塑料制品业"），只取前 6 字，完整值放 title */
function shortIndustry(text: string | undefined) {
  const t = (text || '').trim()
  return t.length > 6 ? t.slice(0, 6) : t || '—'
}

/**
 * 同一天里"哪些源其实是同一批票"。
 *
 * 背景（用户提问）：明细列表里 notify 与 preselected 看起来是重复的 —— 分情况：
 * 全台账 preselected 覆盖 20 个决策日、notify 覆盖 6 个，**只有 3 天同时存在**；
 * 其中 2 天几乎不相交（只重叠 1 只），只有 2026-10-08 两边完全相同（PK 一只没淘汰）。
 * 完全重叠时平铺两遍确实没意义，所以这里明说，并给重复行打「同 X」标记。
 */
const RELATION_TEXT: Record<string, string> = {
  identical: '两边**完全相同**（同一批票）',
  subset: '前者完全包含于后者',
  superset: '前者完全包含后者',
  disjoint: '两边**完全不重叠**',
  partial: '两边部分重叠',
}

const relationNote = computed(() => {
  const rels = day.value?.source_relations || []
  if (!rels.length) return ''
  const parts = rels.map((r) => {
    const txt = (RELATION_TEXT[r.relation] || r.relation).replace(/\*\*/g, '')
    return `${r.a} ↔ ${r.b}：${txt}（共同 ${r.both}｜仅 ${r.a} ${r.only_a}｜仅 ${r.b} ${r.only_b}）`
  })
  const dup = rels.some((r) => r.relation === 'identical' && r.both > 0)
  const tail = dup
    ? '　→ 完全重叠的源在下面的明细里会各列一遍；看"多策略对比"视图更清楚（一只票只占一行）。'
    : ''
  return '源之间的关系：' + parts.join('；') + tail
})

/** 该行还出现在哪些源里（明细列表用来标记重复） */
const codeToSources = computed(() => {
  const map: Record<string, string[]> = {}
  for (const c of day.value?.compare || []) {
    map[c.stock_code] = Object.keys(c.cells || {})
  }
  return map
})
function alsoIn(row: { stock_code: string; source: string }) {
  const others = (codeToSources.value[row.stock_code] || []).filter((s) => s !== row.source)
  return others.join('、')
}

/** 来源 → 单字母：小写 'a' 会和 'A' 混淆，arm 统一用 A */
const SOURCE_LETTER: Record<string, string> = {
  arm: 'A', notify: 'N', preselected: 'P', survey_raw: 'R', survey_gate: 'G',
}

function sourceDots(per: Record<string, number> | undefined) {
  if (!per) return ''
  return Object.entries(per).map(([k, v]) => `${SOURCE_LETTER[k] || k[0]}${v}`).join(' ')
}

/** 把当前视图状态写进 URL query + localStorage —— 切页/刷新回来都能还原。 */
function persistState() {
  const query: Record<string, string> = {}
  if (month.value) query.month = month.value
  if (source.value) query.source = source.value
  if (selectedDate.value) query.date = selectedDate.value
  router.replace({ path: '/calendar', query })
  try {
    localStorage.setItem(STATE_KEY, JSON.stringify(query))
  } catch { /* 隐私模式下忽略 */ }
}

function readSavedState(): Record<string, string> {
  const fromQuery: Record<string, string> = {}
  for (const key of ['month', 'source', 'date']) {
    const value = route.query[key]
    if (typeof value === 'string' && value) fromQuery[key] = value
  }
  if (Object.keys(fromQuery).length) return fromQuery
  try {
    const raw = localStorage.getItem(STATE_KEY)
    if (raw) return JSON.parse(raw) as Record<string, string>
  } catch { /* ignore */ }
  return {}
}

async function loadMonth() {
  monthData.value = await getCalendarMonth(month.value, source.value)
}
async function loadDay(date: string) {
  selectedDate.value = date
  persistState()
  try {
    day.value = await getCalendarDay(date, source.value)
  } catch {
    day.value = null
  }
}
async function loadPerf() {
  perf.value = await getCalendarPerformance(source.value)
}
async function reload() {
  await Promise.all([loadMonth(), loadPerf()])
  const days = monthData.value?.days || []
  persistState()
  // 优先恢复之前选中的日期（仍在本月且有记录），否则退回本月第一天
  const want = selectedDate.value && days.some(d => d.date === selectedDate.value)
    ? selectedDate.value
    : days[0]?.date
  if (want) await loadDay(want)
  else { day.value = null; selectedDate.value = ''; persistState() }
}
function shiftMonth(delta: number) {
  const [y, m] = month.value.split('-').map(Number)
  const d = new Date(y, m - 1 + delta, 1)
  month.value = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
  selectedDate.value = ''          // 换月后旧日期不再适用，交给 reload 选当月首日
  reload()
}

/** 切换来源：保留当前选中日期（若该日期在新来源下没有记录则回退到当月首日）。 */
async function onSourceChange() {
  await reload()
}

onMounted(async () => {
  summary.value = await getCalendarSummary()
  if (!summary.value.built) return
  const months = summary.value.months
  const saved = readSavedState()
  month.value = (saved.month && months.includes(saved.month))
    ? saved.month
    : (months[months.length - 1] || new Date().toISOString().slice(0, 7))
  source.value = saved.source && summary.value.sources.includes(saved.source) ? saved.source : ''
  selectedDate.value = saved.date || ''
  await reload()
})
</script>

<style scoped>
.page { max-width: 1400px; margin: 0 auto; }
.page-title { margin-bottom: 8px; font-size: 18px; }
.hint { color: var(--text-muted); font-size: 12px; margin-bottom: 12px; }
.toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.btn-ghost { background: transparent; color: var(--text-muted); border: 1px solid var(--border-color);
  border-radius: 4px; padding: 5px 12px; cursor: pointer; }
.month-label { font-size: 15px; color: var(--accent-gold); min-width: 90px; text-align: center; }
.toolbar select { background: var(--bg-input); border: 1px solid var(--border-color); border-radius: 4px;
  color: var(--text-primary); padding: 5px 8px; }
.meta { font-size: 12px; color: var(--text-muted); }
.panel { margin-bottom: 16px; }
.panel h6 { font-size: 13px; color: var(--text-muted); margin: 0 0 8px; }
.calendar { display: grid; grid-template-columns: repeat(7, 1fr); gap: 6px; }
.weekday { text-align: center; font-size: 11px; color: var(--text-muted); padding: 2px 0; }
.cell { min-height: 74px; border: 1px solid var(--border-color); border-radius: 6px;
  background: var(--bg-card); padding: 5px 7px; }
.cell.blank { border: 0; background: transparent; }
.cell.idle { opacity: 0.55; }
.cell-idle { font-size: 10px; color: var(--text-muted); margin-top: 6px; }
.cell.has { cursor: pointer; }
.cell.selected { border-color: var(--accent-gold); box-shadow: 0 0 0 1px var(--accent-gold) inset; }
.cell-top { display: flex; justify-content: space-between; align-items: baseline; }
.dnum { font-size: 12px; color: var(--text-primary); }
.n { font-size: 11px; color: var(--text-muted); }
.bar { height: 4px; border-radius: 2px; margin: 4px 0; }
.cell-mid { display: flex; justify-content: space-between; font-size: 11px; color: var(--text-muted); }
.src { font-family: ui-monospace, Menlo, monospace; }
.ok { color: var(--accent-down); }
.bad { color: var(--accent-up); }
.muted { color: var(--text-muted); }
.legend { display: flex; gap: 14px; flex-wrap: wrap; margin-top: 8px; font-size: 11px; color: var(--text-muted); }
.sw { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; }
.sw.gain { background: rgba(0,212,170,0.7); }
.sw.loss { background: rgba(239,71,111,0.7); }
.sw.none { background: rgba(42,46,57,0.8); }
.pill-row { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; }
.pill { font-size: 11px; color: var(--text-muted); background: var(--bg-card);
  border: 1px solid var(--border-color); border-radius: 10px; padding: 3px 10px; }
.pill b { color: var(--text-primary); }
.data-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.data-table th { background: #1a1a2e; color: var(--text-muted); padding: 6px 8px; text-align: right;
  border-bottom: 1px solid var(--border-color); white-space: nowrap; }
.data-table td { padding: 5px 8px; text-align: right; border-bottom: 1px solid rgba(42,46,57,0.5); }
.data-table td.left, .data-table th.left { text-align: left; }
.mono { font-family: ui-monospace, Menlo, monospace; color: var(--text-muted); }
.note { max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ellipsis { max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* 原因/风险：多行换行显示（不再单行省略），并给一个舒服的可读宽度 */
.multiline { min-width: 260px; max-width: 380px; white-space: normal; overflow-wrap: anywhere;
  line-height: 1.45; vertical-align: top; }
.subtle { color: var(--text-muted); font-size: 11px; margin-top: 2px; }
.link { color: var(--accent-gold); cursor: pointer; }
.link:hover { text-decoration: underline; }
.stage-chip { padding: 1px 8px; border-radius: 9px; border: 1px solid var(--border-color);
  font-size: 11px; white-space: nowrap; }
.stage-uptrend { color: var(--accent-up); border-color: var(--accent-up); }
.stage-downtrend { color: var(--accent-down); border-color: var(--accent-down); }
.stage-top_risk { color: var(--accent-orange); border-color: var(--accent-orange); }
.stage-bottom_reversal { color: var(--accent-blue); border-color: var(--accent-blue); }
.stage-mid_trend { color: var(--text-muted); }
.curve { width: 100%; height: 180px; background: var(--bg-card); border: 1px solid var(--border-color);
  border-radius: 6px; margin-bottom: 10px; }
.empty { color: var(--text-muted); }
.tiny { font-size: 11px; }
</style>

<style scoped>
/* 多策略对比：命中的格子给一点底纹，扫一眼就能看出"哪只票被几个策略同时选中" */
.data-table td.cell-in { background: rgba(255, 209, 102, 0.07); }
.relation-note { margin: 0 0 var(--sp-2); line-height: 1.6; }
</style>
