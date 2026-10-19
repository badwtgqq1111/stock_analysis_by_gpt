<template>
  <div class="page">
    <PageHeader
      title="实盘镜像"
      description="真实券商持仓（人工录入 <code>config/live_account_holdings.json</code>）：<strong>实时分时 / 日K</strong>、<strong>日/周/月 对中证500 的收益对比</strong>、<strong>收益日历</strong>。它不下单、不参与生产权重。"
    />

    <StateBlock v-if="error" state="error" title="取不到实盘镜像数据" :detail="error" :retry="reload" />
    <StateBlock v-else-if="!live" state="loading" title="加载中…" />
    <StateBlock v-else-if="!live.available" state="empty" title="还没有实盘镜像产物"
                detail="先跑：uv run python scripts/build_live_mirror.py --print" />

    <template v-else>
      <!-- ① 账户概览 -->
      <div class="panel">
        <div class="pill-row">
          <span class="pill">{{ live.broker }} <b>{{ live.account_masked }}</b></span>
          <span class="pill">快照 <b>{{ live.captured_at }}</b>
            <span v-if="(live.snapshot_age_days ?? 0) > 5" class="warn-text">（{{ live.snapshot_age_days }} 天未更新）</span>
          </span>
          <span class="pill" :title="rt?.quote_error || ''">价源
            <b :class="usingQuotePrice ? 'up' : 'muted'">{{ priceBasisLabel }}</b>{{ quotedAt }}</span>
          <span class="pill" :class="usingQuotePrice ? 'up' : ''">
            行情更新 <b>{{ realtimeUpdated || '—' }}</b>
            <span class="tiny muted" v-if="rtLive">（{{ polling ? '15 秒轮询中' : '已停' }}）</span>
            <span class="tiny muted" v-else>（{{ rtPhase }} · {{ priceBasisLabel }}）</span>
          </span>
          <span class="pill">盘后产物 <b>{{ live.generated_at }}</b> · 盯市
            <b :class="(live.realtime_priced ?? 0) > 0 ? 'up' : 'muted'">{{ live.realtime_priced ?? 0 }}</b>/{{ live.positions.length }} 条</span>
        </div>
        <div class="stat-grid">
          <StatTile label="总资产" :value="money(live.total_assets)"
                    :sub="`市值 ${money(live.market_value)} + 现金 ${money(live.cash)}`" />
          <StatTile label="推算初始本金" :value="money(live.initial_capital_implied)" sub="成本 + 现金 + |已实现|" />
          <StatTile label="账户累计" :value="pct(live.cumulative_return)" :tone="tone(live.cumulative_return ?? 0)"
                    sub="含已实现与未实现" />
          <StatTile label="未实现浮盈" :value="money(live.unrealized_pnl)" :tone="tone(live.unrealized_pnl ?? 0)"
                    sub="相对成本价" />
          <StatTile label="当日参考盈亏" :value="money(live.day_pnl ?? null)" :tone="tone(live.day_pnl ?? 0)"
                    :sub="usingQuotePrice ? `现价 vs 昨收（券商同口径）· ${priceBasisLabel}` : '盘后口径'" />
          <StatTile :label="`已实现（清仓 ${live.cleared_positions?.length ?? 0} 笔）`"
                    :value="money(live.realized_pnl)" :tone="tone(live.realized_pnl ?? 0)" />
          <StatTile label="第一重仓" :value="pct(live.top1_weight)" sub="单票上限 12%" />
        </div>
        <div class="pill-row mt" v-if="live.relative_20d?.return_20d !== undefined">
          <span class="pill">同窗 20 日（{{ live.relative_20d.window_start }} ~ {{ live.relative_20d.window_end }}）：中证500
            <b :class="tone(live.relative_20d.return_20d ?? 0)">{{ pct(live.relative_20d.return_20d) }}</b></span>
          <span class="pill">持仓组合 <b :class="tone(live.relative_20d.portfolio_20d ?? 0)">{{ pct(live.relative_20d.portfolio_20d) }}</b></span>
          <span class="pill" :class="tone(live.relative_20d.excess_20d ?? 0)">超额 <b>{{ pct(live.relative_20d.excess_20d) }}</b></span>
        </div>
      </div>

      <!-- ② 持仓 + 实时分时/日K -->
      <div class="panel">
        <h6>持仓与个股行情（点一行 → 下方看它的实时分时 / 日K）</h6>
        <div class="table-scroll">
          <table class="data-table">
            <thead><tr><th>代码</th><th>名称</th><th>股数</th><th>成本价</th><th>现价</th><th>行情时间</th><th>市值</th><th>权重</th>
              <th>浮盈</th><th>20日</th><th>建议</th><th>行情</th></tr></thead>
            <tbody>
              <tr v-for="p in live.positions" :key="p.stock_code" :class="{ picked: p.stock_code === pickedCode }"
                  @click="pick(p.stock_code)">
                <td class="mono">{{ p.stock_code }}</td>
                <td>{{ p.name }}</td>
                <td>{{ p.shares }}</td>
                <td>{{ p.cost_price }}</td>
                <td>{{ p.last_price }}</td>
                <td class="tiny" :class="String(p.price_source || '').startsWith('live') ? 'up' : 'muted'">
                  {{ quoteTime(p) }}
                </td>
                <td>{{ money(p.market_value) }}</td>
                <td :class="(p.weight ?? 0) > (live.max_weight_single_allowed ?? 0.12) ? 'down' : ''">{{ pct(p.weight) }}</td>
                <td :class="tone(p.pnl_amount)">{{ Number(p.pnl_amount).toFixed(2) }}
                  <span class="tiny">({{ pct(p.pnl_pct) }})</span></td>
                <td :class="tone(live.relative_20d?.per_position?.[p.stock_code]?.momentum_20d ?? 0)">
                  {{ pct(live.relative_20d?.per_position?.[p.stock_code]?.momentum_20d) }}</td>
                <td><b :class="p.action === '卖出' ? 'down' : (p.action === '买入' ? 'up' : '')">{{ p.action || '—' }}</b>
                  <span class="tiny muted">{{ p.rule }}</span></td>
                <td class="tiny">
                  <a class="link" @click.stop="pick(p.stock_code, 'intraday')">分时</a> ·
                  <a class="link" @click.stop="pick(p.stock_code, 'daily')">日K</a>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="pill-row mt">
          <span class="pill">当前 <b>{{ pickedCode || '未选' }}</b>
            <span v-if="pickedName">{{ pickedName }}</span></span>
          <button class="tab" :class="{ on: chartTab === 'intraday' }" @click="pick(pickedCode, 'intraday')">实时分时（今日）</button>
          <button class="tab" :class="{ on: chartTab === 'daily' }" @click="pick(pickedCode, 'daily')">日K（近 1 年）</button>
          <span class="pill" v-if="chartTab === 'intraday' && intraday">
            源 <b>{{ intraday.source_label }}</b> · {{ intraday.date }}
            <b>{{ intraday.latest.points }}</b> 点
            <b :class="tone(intraday.latest.change_pct ?? 0)">{{ pct(intraday.latest.change_pct) }}</b>
            <span v-if="intraday.session?.live" class="tiny muted">（{{ polling ? '轮询中' : '已停' }}）</span>
          </span>
          <span class="pill" v-if="chartTab === 'daily' && ohlcv">{{ ohlcv.code }} {{ ohlcv.name }}
            <b>{{ ohlcv.data.length }}</b> 根</span>
        </div>
        <p class="tiny muted" v-if="chartTab === 'intraday'">
          分时是**当日**（实时源，30 秒轮询、非交易时段自动停、页面隐藏时停）；看历史某天的分时与逐股 AI 多空分析，
          点右上角进「K 线图表」页。这里只画价格与均价两条线。
        </p>
        <div v-if="chartTab === 'intraday'">
          <p v-if="intradayLoading" class="tiny muted">加载分时…</p>
          <p v-else-if="!intraday" class="tiny muted">暂无分时（{{ intradayError || '非交易时段或源不可用' }}）</p>
          <IntradayChart v-else :code="intraday.code" :date="intraday.date" :points="intraday.points" :prev-close="intraday.prev_close" :height="300" />
        </div>
        <div v-else>
          <p v-if="ohlcvLoading" class="tiny muted">加载日K…</p>
          <KlineChart v-else-if="ohlcv" :data="ohlcv" :height="460"
                      :is-loading="ohlcvLoading" :error="ohlcvError || null" :show-signals="false" />
          <p v-else class="tiny muted">暂无日K（{{ ohlcvError || '—' }}）</p>
        </div>
      </div>

      <!-- ③ 日/周/月 收益对比 vs 中证500 -->
      <div class="panel">
        <h6>收益对比：实盘持仓（当前权重） vs 中证500</h6>
        <p class="tiny muted">
          回填口径：按<strong>当前权重</strong>在窗口起点买入并持有到最新交易日（不含现金 {{ money(live.cash) }} 元、不含已实现 {{ money(live.realized_pnl) }}、不含交易成本）。
          它回答「这一篮子现在这个结构，过去这段跟中证500 比怎样」——<strong>不是账户业绩</strong>；账户业绩只能从快照日起向前累积（上面第 ① 块）。
        </p>
        <p class="tiny" v-if="perf?.account">
          账户实际累计（推算初始本金起）：
          <b :class="tone(live.cumulative_return)">{{ pct(live.cumulative_return) }}</b>
          · 快照起（含现金/已实现）：
          <b :class="tone(perf.account.cum_return)">{{ pct(perf.account.cum_return) }}</b>
          · {{ perf.account.start }} ~ {{ perf.account.end }}（{{ perf.account.rows }} 个交易日<template v-if="perf.account.rows < 30">，样本不足 30 天，仅参考</template>）
        </p>
        <div class="pill-row">
          <button v-for="f in freqs" :key="f.key" class="tab" :class="{ on: freq === f.key }" @click="freq = f.key">
            {{ f.label }}
          </button>
          <span class="sep" />
          <button v-for="w in windows" :key="w.key" class="tab" :class="{ on: window === w.key }" @click="window = w.key">
            {{ w.label }}
          </button>
          <span class="pill">区间 <b>{{ shownStats.start }} ~ {{ shownStats.end }}</b> · {{ shownStats.periods }} 期</span>
        </div>
        <SvgCurve :series="compareSeries" :height="280" :labels="shownLabels" />
        <div class="table-scroll mt">
          <table class="data-table">
            <thead><tr><th>口径</th><th>累计</th><th>年化</th><th>超额</th><th>胜率</th><th>最大回撤</th>
              <th>年化波动</th><th>最好一期</th><th>最差一期</th></tr></thead>
            <tbody>
              <tr v-for="row in statsRows" :key="row.name">
                <td>{{ row.name }}</td>
                <td :class="tone(row.total)">{{ pct(row.total) }}</td>
                <td>{{ pct(row.ann) }}</td>
                <td :class="tone(row.excess)">{{ row.excess === null ? '—' : pct(row.excess) }}</td>
                <td>{{ row.win === null ? '—' : (row.win * 100).toFixed(0) + '%' }}</td>
                <td class="down">{{ pct(row.dd) }}</td>
                <td>{{ pct(row.vol) }}</td>
                <td :class="tone(row.best)">{{ pct(row.best) }}</td>
                <td :class="tone(row.worst)">{{ pct(row.worst) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="tiny muted mt" v-for="n in (perf?.notes || [])" :key="n">· {{ n }}</p>
      </div>

      <!-- ④ 收益日历 -->
      <div class="panel">
        <h6>收益日历（格子 = 该期组合收益，颜色红涨绿跌；悬停看中证500 与超额）</h6>
        <div class="pill-row">
          <button v-for="f in freqs" :key="f.key" class="tab" :class="{ on: calFreq === f.key }" @click="calFreq = f.key">
            {{ f.label }}
          </button>
          <span class="pill">正收益 <b class="up">{{ calSummary.up }}</b> / 负收益 <b class="down">{{ calSummary.down }}</b>
            · 合计 <b :class="tone(calSummary.total)">{{ pct(calSummary.total) }}</b></span>
          <span class="pill">同期中证500 <b :class="tone(calSummary.benchTotal)">{{ pct(calSummary.benchTotal) }}</b></span>
          <span class="pill" :class="tone(calSummary.total - calSummary.benchTotal)">
            超额 <b>{{ pct(calSummary.total - calSummary.benchTotal) }}</b></span>
        </div>
        <div class="cal-blocks" v-if="calendarBlocks.length">
          <div class="cal-block" v-for="block in calendarBlocks" :key="block.label">
            <div class="cal-title">{{ block.label }}</div>
            <div class="cal-grid" :class="{ days: calFreq === 'daily' }">
              <span v-for="head in (calFreq === 'daily' ? ['一', '二', '三', '四', '五', '六', '日'] : [])"
                    :key="head" class="cal-head">{{ head }}</span>
              <span v-for="cell in block.cells" :key="cell.period" class="cal-cell"
                    :style="cell.col ? { ...cellStyle(cell), gridColumn: String(cell.col) } : cellStyle(cell)"
                    :title="cellTitle(cell)">
                <b>{{ cellLabel(cell) }}</b>
                <i>{{ pct(cell.port_ret) }}</i>
              </span>
            </div>
          </div>
        </div>
        <p class="tiny muted mt">
          日历只画<strong>回填口径</strong>的组合收益（同上第 ③ 块）。真实账户的收益日历要等每日自动化积累
          —— 账户从快照日 {{ live.asof }} 起才有净值，且快照日之前无法回填（买入日期未知）。
        </p>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { getIntraday, getLivePerformance, getLiveRealtime, getOhlcv, getPaperStatus } from '@/api/client'
import type {
  IntradayResponse, LiveCalendarCell, LiveMirror, LiveMirrorPosition, LivePerformance,
  LiveRealtime, OhlcvResponse, PaperStatus,
} from '@/types/api'
import PageHeader from '@/components/ui/PageHeader.vue'
import StateBlock from '@/components/ui/StateBlock.vue'
import StatTile from '@/components/ui/StatTile.vue'
import SvgCurve from '@/components/ui/SvgCurve.vue'
import KlineChart from '@/components/KlineChart.vue'
import IntradayChart from '@/components/IntradayChart.vue'

const paper = ref<PaperStatus | null>(null)
const perf = ref<LivePerformance | null>(null)
const error = ref('')
const intraday = ref<IntradayResponse | null>(null)
const intradayError = ref('')
const intradayLoading = ref(false)
const ohlcv = ref<OhlcvResponse | null>(null)
const ohlcvError = ref('')
const ohlcvLoading = ref(false)
const pickedCode = ref('')
/** 当前选中票的名称（从实盘镜像持仓里取；分时/日K 的标头要显示它 ——
    用户 2026-10-13 报："K 线选股 点中某只股票的时候，分时线看不到股票标头"。 */
const pickedName = computed(() =>
  (live.value?.positions || []).find((p) => String(p.stock_code) === pickedCode.value)?.name || '')
const chartTab = ref<'intraday' | 'daily'>('intraday')
const freq = ref<'daily' | 'weekly' | 'monthly'>('daily')
const window = ref<'m1' | 'm3' | 'y1' | 'y3'>('y1')
const calFreq = ref<'daily' | 'weekly' | 'monthly'>('daily')
const polling = ref(false)
let timer: ReturnType<typeof setInterval> | null = null
let rtTimer: ReturnType<typeof setInterval> | null = null
let rtInterval = 15000

const rt = ref<LiveRealtime | null>(null)
const realtimeUpdated = ref('')

/**
 * 页面统一走这一个 `live`：**账户级数字**用实时视图（实时价重算），
 * **一天只变一次的东西**（已实现明细、清仓列表、同窗 20 日、收益对比摘要）继续用盘后产物 ——
 * 两者拼起来才是完整视图，且数字口径只有一套（后端 `realtime_view` 复用 `account_state`）。
 */
/** 页面视图 = 盘后产物（一天的字段）+ 实时重算（账户级数字）；`day_pnl` 只在实时口径下有意义。 */
type LiveView = LiveMirror & { day_pnl?: number | null; realtime_priced?: number; realtime_unpriced?: number;
  price_basis_label?: string; price_date?: string | null }
const live = computed<LiveView | null>(() => {
  const disk = paper.value?.live ?? null
  if (!disk) return null
  const r = rt.value
  if (!r || !r.available) return disk
  return {
    ...disk,
    positions: r.positions as LiveMirrorPosition[],
    cash: r.cash ?? disk.cash,
    market_value: r.market_value ?? disk.market_value,
    total_assets: r.total_assets ?? disk.total_assets,
    cost_basis: r.cost_basis ?? disk.cost_basis,
    unrealized_pnl: r.unrealized_pnl ?? disk.unrealized_pnl,
    cumulative_return: r.cumulative_return ?? disk.cumulative_return,
    initial_capital_implied: r.initial_capital_implied ?? disk.initial_capital_implied,
    top1_weight: r.top1_weight ?? disk.top1_weight,
    top3_weight: r.top3_weight ?? disk.top3_weight,
    price_source: r.price_source ?? disk.price_source,
    price_basis_label: r.price_basis_label,
    price_date: r.price_date ?? null,
    day_pnl: r.day_pnl ?? null,
    realtime_priced: r.priced, realtime_unpriced: r.unpriced,
  }
})
const rtLive = computed(() => Boolean(rt.value?.session?.live))
const rtPhase = computed(() => String(rt.value?.session?.phase || 'closed'))
/** 报价口径：实时 / 今日收盘 / 上一收盘 / 盘后产物（用户 2026-10-13：实盘页价格不跟分时走） */
const PRICE_BASIS: Record<string, string> = {
  live: '实时', close: '今日收盘', prev_close: '上一收盘', disk: '盘后产物',
}
const priceBasis = computed(() => String(live.value?.price_source || 'disk'))
const priceBasisLabel = computed(() =>
  live.value?.price_basis_label || PRICE_BASIS[priceBasis.value] || '盘后产物')
/** 用的是行情价（实时/今日收盘/上一收盘）而不是早上那份盘后产物价 */
const usingQuotePrice = computed(() => priceBasis.value !== 'disk')
const quotedAt = computed(() => (live.value?.price_date ? `（报价 ${live.value.price_date}）` : ''))
function quoteTime(row: LiveMirrorPosition): string {
  const raw = String((row as { live_time?: string }).live_time || '')
  const m = raw.match(/^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})$/)
  // 报价时间就是行情源给的时间戳：盘中=实时、收盘后=当日收盘时刻、盘前/周末=上一交易日收盘时刻
  return m ? `${m[4]}:${m[5]}:${m[6]}` : '盘后产物'
}
async function loadRealtime() {
  try {
    const payload = await getLiveRealtime()
    rt.value = payload
    if (payload.updated_at) realtimeUpdated.value = payload.updated_at
  } catch {
    // 实时取不到就继续用盘后产物（不把页面打空），只是实时时间戳不前进
  }
}
const freqs = [
  { key: 'daily' as const, label: '日' },
  { key: 'weekly' as const, label: '周' },
  { key: 'monthly' as const, label: '月' },
]
const windows = [
  { key: 'm1' as const, label: '近 1 月' },
  { key: 'm3' as const, label: '近 3 月' },
  { key: 'y1' as const, label: '近 1 年' },
  { key: 'y3' as const, label: '近 3 年' },
]

async function reload() {
  try {
    paper.value = await getPaperStatus()
    error.value = ''
    if (!pickedCode.value && paper.value.live?.positions?.length) pick(paper.value.live.positions[0].stock_code)
  } catch (e) {
    error.value = String(e)
  }
  try { perf.value = await getLivePerformance() } catch { perf.value = null }
}

// ── 个股行情：分时（今日，交易时段 30 秒轮询）+ 日K ──
async function loadIntraday() {
  if (!pickedCode.value) return
  intradayLoading.value = true
  try {
    intraday.value = await getIntraday(pickedCode.value, '1', 'auto')
    intradayError.value = ''
    polling.value = true
  } catch (e) {
    intradayError.value = String(e)
    polling.value = false
  } finally {
    intradayLoading.value = false
  }
}
async function loadDaily() {
  if (!pickedCode.value) return
  ohlcvLoading.value = true
  try {
    ohlcv.value = await getOhlcv(pickedCode.value, 365, false, false)
    ohlcvError.value = ''
  } catch (e) {
    ohlcvError.value = String(e)
  } finally {
    ohlcvLoading.value = false
  }
}
function pick(code: string, tab?: 'intraday' | 'daily') {
  if (!code) return
  pickedCode.value = code
  if (tab) chartTab.value = tab
  if (chartTab.value === 'intraday') loadIntraday()
  else loadDaily()
}
// 同一只票切换 tab 时按需加载（不重复打接口）
watch(chartTab, (tab) => {
  if (!pickedCode.value) return
  if (tab === 'intraday' && !intraday.value) loadIntraday()
  if (tab === 'daily' && !ohlcv.value) loadDaily()
})
onMounted(async () => {
  await reload()
  await loadRealtime()
  // 分时轮询：只在交易时段（后端 session.live）且页面可见时跑
  timer = setInterval(async () => {
    if (chartTab.value !== 'intraday' || document.hidden) return
    if (intraday.value && !intraday.value.session?.live) return
    await loadIntraday()
  }, 30000)
  // 账户实时轮询（用户要求"每天数据都是实时更新的"）：交易时段 15 秒；非交易时段 120 秒兜底
  // （闭市后价格不动，但收盘后每日自动化会重写产物 —— 120 秒足够捡到那一版）。
  rtTimer = setInterval(async () => {
    if (document.hidden) return
    await loadRealtime()
    const every = rtLive.value ? 15000 : 120000
    if (every !== rtInterval) {
      rtInterval = every
      if (rtTimer) clearInterval(rtTimer)
      rtTimer = setInterval(loadRealtime, every)
    }
  }, 15000)
})
onUnmounted(() => { if (timer) clearInterval(timer); if (rtTimer) clearInterval(rtTimer) })



// ── 收益对比（日/周/月 × 区间）──
const block = computed(() => perf.value?.[freq.value] ?? null)
const shownSlice = computed(() => {
  const b = block.value
  if (!b) return { labels: [] as string[], port: [] as number[], bench: [] as number[] }
  const cut = { m1: 21, m3: 63, y1: 252, y3: 9999 }[window.value]
  const step = freq.value === 'daily' ? 1 : (freq.value === 'weekly' ? 5 : 21)
  const n = Math.min(b.dates.length, Math.max(2, Math.round(cut / step) + 1))
  const start = Math.max(0, b.dates.length - n)
  const port = b.port_nav.slice(start)
  const bench = b.bench_nav.slice(start)
  return {
    labels: b.dates.slice(start),
    port: port.map((v) => v / port[0]),
    bench: bench.map((v) => v / bench[0]),
  }
})
const compareSeries = computed(() => [
  { name: '实盘持仓（当前权重）', color: '#e07a5f', points: shownSlice.value.port },
  { name: '中证500', color: '#118ab2', points: shownSlice.value.bench },
])
const shownLabels = computed(() => shownSlice.value.labels)
const shownStats = computed(() => {
  const b = block.value
  const windowStats = perf.value?.windows?.[window.value]?.stats
  if (freq.value === 'daily' && windowStats) return windowStats
  const p = shownSlice.value.port
  const bench = shownSlice.value.bench
  const total = p.length ? p[p.length - 1] - 1 : 0
  const benchTotal = bench.length ? bench[bench.length - 1] - 1 : 0
  return {
    start: shownSlice.value.labels[0], end: shownSlice.value.labels[shownSlice.value.labels.length - 1],
    periods: Math.max(0, p.length - 1), total_return: total, benchmark_total_return: benchTotal,
    excess_return: total - benchTotal, annualized_return: null, benchmark_annualized_return: null,
    max_drawdown: (b?.stats?.max_drawdown ?? null), win_rate: null, ann_vol: null,
    best_period: null, worst_period: null,
  }
})
const statsRows = computed(() => {
  const s = shownStats.value
  return [
    { name: '实盘持仓（当前权重）', total: s.total_return ?? null, ann: s.annualized_return ?? null,
      excess: s.excess_return ?? null, win: s.win_rate ?? null, dd: s.max_drawdown ?? null,
      vol: s.ann_vol ?? null, best: s.best_period ?? null, worst: s.worst_period ?? null },
    { name: '中证500', total: s.benchmark_total_return ?? null, ann: s.benchmark_annualized_return ?? null,
      excess: null, win: null, dd: null, vol: null, best: null, worst: null },
  ]
})

// ── 收益日历 ──
const calCells = computed<LiveCalendarCell[]>(() => perf.value?.[calFreq.value]?.calendar ?? [])
const calSummary = computed(() => {
  let total = 1, benchTotal = 1, up = 0, down = 0
  for (const c of calCells.value) {
    total *= (1 + c.port_ret)
    benchTotal *= (1 + c.bench_ret)
    if (c.port_ret > 0) up += 1
    else if (c.port_ret < 0) down += 1
  }
  return { total: total - 1, benchTotal: benchTotal - 1, up, down }
})
const maxAbs = computed(() => Math.max(0.005, ...calCells.value.map((c) => Math.abs(c.port_ret))))
const calendarBlocks = computed(() => {
  const groups = new Map<string, LiveCalendarCell[]>()
  for (const c of calCells.value) {
    const key = calFreq.value === 'monthly' ? c.period.slice(0, 4) : c.period.slice(0, 7)
    const list = groups.get(key) ?? []
    list.push(c)
    groups.set(key, list)
  }
  const keys = [...groups.keys()].sort().reverse()
  const recent = keys.slice(0, calFreq.value === 'monthly' ? 4 : 12)
  return recent.map((key) => {
    const cells = (groups.get(key) ?? []).slice().sort((a, b) => a.period.localeCompare(b.period))
    const label = calFreq.value === 'monthly' ? `${key} 年`
      : `${key.slice(0, 4)}-${key.slice(5, 7)}`
    // 事故（2026-10-13 用户报"2026-10-12 不是星期一么？这个日期是不是排错了"）：
    // 原来只在月初补一次空格（pads），之后**按顺序填充** —— 而格子只有交易日，
    // 周末/节假日缺席 ⇒ 后面的日期整体左移。实测 2026-10 的格子落点：
    // 8(周四)✓ 9(周五)✓ 12 → 落在「六」列（正确是下一行的「一」）。
    // 修法：每个格子按**自己那天是周几**显式占列（grid-column），缺口自然留白。
    const placed = calFreq.value === 'daily'
      ? cells.map((c) => ({ ...c, col: weekdayCol(c.period) }))
      : cells.map((c) => ({ ...c, col: 0 }))
    return { label, cells: placed, pads: 0 }
  })
})
/** 该日期在"周一起算"的日历里占第几列（1..7）；周日=0 → 7。 */
function weekdayCol(period: string): number {
  return ((new Date(`${period}T00:00:00`).getDay() + 6) % 7) + 1
}
function cellLabel(cell: LiveCalendarCell) {
  return calFreq.value === 'daily' ? String(Number(cell.period.slice(8, 10)))
    : (calFreq.value === 'weekly' ? cell.period.slice(5) : `${Number(cell.period.slice(5, 7))}月`)
}
function cellStyle(cell: LiveCalendarCell) {
  const scale = Math.min(Math.abs(cell.port_ret) / maxAbs.value, 1)
  const color = cell.port_ret >= 0 ? 'var(--accent-up)' : 'var(--accent-down)'
  return { background: color, opacity: String(0.2 + 0.8 * scale) }
}
function cellTitle(cell: LiveCalendarCell) {
  return `${cell.period}｜组合 ${pct(cell.port_ret)}｜中证500 ${pct(cell.bench_ret)}｜超额 ${pct(cell.excess)}`
}

function money(v: number | null | undefined) {
  return v === null || v === undefined || Number.isNaN(Number(v)) ? '—' : `${Math.round(Number(v)).toLocaleString('zh-CN')} 元`
}
function pct(v: number | null | undefined) {
  return v === null || v === undefined || Number.isNaN(Number(v)) ? '—' : `${(Number(v) * 100).toFixed(2)}%`
}
function tone(v: number | null | undefined) { return Number(v ?? 0) >= 0 ? 'up' : 'down' }
</script>

<style scoped>
.tab {
  background: var(--bg-input); border: 1px solid var(--border-color); color: inherit;
  border-radius: 6px; padding: 3px 10px; font-size: var(--fs-xs); cursor: pointer;
}
.tab.on { border-color: var(--accent-gold); color: var(--accent-gold); }
.sep { width: 1px; background: var(--border-color); margin: 0 6px; }
tr.picked { background: rgba(233, 196, 106, 0.08); }
tr { cursor: pointer; }
.mt { margin-top: var(--sp-3); }
.muted { color: var(--text-dim); }
code { font-family: var(--font-mono, monospace); font-size: 0.92em; }
.cal-blocks { display: flex; flex-wrap: wrap; gap: var(--sp-3); margin-top: var(--sp-2); }
.cal-block { border: 1px solid var(--border-color); border-radius: 8px; padding: 6px 8px; }
.cal-title { font-size: var(--fs-xs); color: var(--text-dim); margin-bottom: 4px; }
.cal-grid { display: grid; grid-template-columns: repeat(4, 40px); gap: 3px; }
.cal-grid.days { grid-template-columns: repeat(7, 32px); }
.cal-head { font-size: 10px; color: var(--text-dim); text-align: center; }
.cal-pad { width: 32px; height: 26px; }
.cal-cell {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  border-radius: 4px; color: #101216; font-size: 10px; height: 26px; line-height: 1.05;
}
.cal-cell.days { width: 32px; }
.cal-cell b { font-size: 10px; }
.cal-cell i { font-style: normal; font-size: 9px; opacity: 0.85; }
</style>
