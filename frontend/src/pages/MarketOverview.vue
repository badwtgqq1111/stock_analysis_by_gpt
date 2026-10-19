<template>
  <div class="page">
    <PageHeader
      title="大盘行情"
      description="指数日线与全 A 成交额。指数只做市场温度参考，不进入个股策略池；成交额为 clean 日线名义金额汇总（元 → 亿元）。"
    >
      <template #actions>
        <span class="pill tiny" v-if="overview?.asof">日线交易日 <b>{{ overview.asof }}</b></span>
        <button class="btn-ghost" :disabled="loading" @click="reload">刷新</button>
      </template>
    </PageHeader>

    <StateBlock v-if="error" state="error" title="大盘数据取不到" :detail="error" :retry="reload" />
    <StateBlock v-else-if="loading && !overview" state="loading" title="加载大盘数据…" />

    <template v-else-if="overview?.available">
      <div class="summary-grid">
        <StatTile label="日线交易日" :value="overview.asof || '—'" sub="指数收盘日" />
        <StatTile label="全 A 成交额" :value="money(overview.total_amount_yi)" tone="gold"
                  :sub="turnoverSub" />
        <StatTile label="上涨 / 下跌" :value="upDown" :sub="`${overview.indices.length} 个指数`" />
        <StatTile label="20 日成交额变化" :value="trendText" :sub="trendSub" />
      </div>

      <div class="panel turnover-panel">
        <div class="chart-head">
          <div>
            <b>全 A 每日成交额</b>
            <span class="tiny dim" v-if="turnover?.stats?.ma20 != null">
              · 20 日均 {{ money(turnover.stats.ma20) }}
            </span>
          </div>
          <div class="range-tabs">
            <button v-for="r in turnoverRanges" :key="r.days" class="tab tiny"
                    :class="{ active: turnoverDays === r.days }"
                    @click="turnoverDays = r.days">{{ r.label }}</button>
          </div>
        </div>
        <StateBlock v-if="turnoverError" state="error" title="成交额曲线取不到" :detail="turnoverError" />
        <StateBlock v-else-if="turnoverLoading && !turnover" state="loading" title="加载成交额…" />
        <TurnoverChart v-else-if="turnover?.available" :series="turnover.series" :height="280" />
        <p class="hint tiny">
          X 轴为交易日，Y 轴为亿元；黄色为每日成交额，绿色虚线为 20 日均值。
        </p>
      </div>

      <div class="panel">
        <div class="market-head">
          <div>
            <b>指数一览</b>
            <span class="tiny dim">点一张卡切换下方 K 线</span>
          </div>
          <div class="turnover-mini" v-if="overview.turnover_series.length">
            <span class="tiny dim">近 20 日成交额</span>
            <Sparkline :values="overview.turnover_series.map(x => x.amount_yi)" :height="30"
                       color="var(--accent-gold)" />
          </div>
        </div>
        <div class="index-grid">
          <button v-for="q in overview.indices" :key="q.code"
                  class="index-card" :class="{ active: q.code === selected }"
                  @click="selectIndex(q.code)">
            <div class="ic-top">
              <span class="ic-name">{{ q.name }}</span>
              <span class="ic-code mono tiny">{{ q.code }}</span>
            </div>
            <div class="ic-mid">
              <b class="ic-price mono">{{ fmt(q.close) }}</b>
              <span class="ic-chg mono" :class="tone(q.pct_chg)">{{ pct(q.pct_chg) }}</span>
            </div>
            <div class="ic-foot">
              <span class="tiny dim">成交 {{ money(q.amount_yi) }}</span>
              <Sparkline :values="(q.spark || []).map(x => x.close)" :height="22"
                         :color="(q.pct_chg ?? 0) >= 0 ? 'var(--state-bad)' : 'var(--state-ok)'" />
            </div>
          </button>
        </div>
      </div>

      <div class="panel chart-panel">
        <div class="chart-head">
          <div class="chart-title">
            <b>{{ selectedQuote?.name || selected }}</b>
            <span class="mono tiny dim">{{ selected }}</span>
            <span v-if="chartMode === 'intraday' && intraday?.date" class="pill tiny">分时 {{ intraday.date }}</span>
            <span v-if="activePrice != null" class="mono chart-last">{{ fmt(activePrice) }}</span>
            <span v-if="activeChange != null" class="mono" :class="tone(activeChange)">
              {{ pct(activeChange) }}
            </span>
          </div>
          <div class="chart-tools">
            <div class="mode-tabs">
              <button class="tab tiny" :class="{ active: chartMode === 'intraday' }"
                      @click="chartMode = 'intraday'">分时</button>
              <button class="tab tiny" :class="{ active: chartMode === 'kline' }"
                      @click="chartMode = 'kline'">日K</button>
            </div>
            <div class="range-tabs" v-if="chartMode === 'kline'">
              <button v-for="r in ranges" :key="r.days" class="tab tiny"
                      :class="{ active: days === r.days }" @click="days = r.days">{{ r.label }}</button>
            </div>
            <span v-else class="pill tiny">实时 · 30 秒刷新</span>
          </div>
        </div>
        <template v-if="chartMode === 'intraday'">
          <StateBlock v-if="intradayError" state="error" title="指数分时取不到" :detail="intradayError" />
          <StateBlock v-else-if="intradayLoading && !intraday" state="loading" title="加载指数分时…" />
          <StateBlock v-else-if="intraday && !intraday.available" state="empty"
                      title="当前没有指数分时" :detail="intraday.reason || '非交易时段或源暂不可用'" />
          <IntradayChart v-else-if="intraday" :points="intraday.points"
                         :prev-close="intraday.prev_close" :height="520"
                         code="" :date="intraday.date" />
          <p class="hint tiny chart-note">
            指数分时来自腾讯 `minute/query`，盘中约 30 秒刷新；价格线/均价线与成交量均按源数据展示。
            <span v-if="intraday && intraday.points.length <= 1" class="dim">
              当前只有开盘/竞价快照，连续竞价开始后会自动铺开分时线。
            </span>
          </p>
        </template>
        <template v-else>
          <StateBlock v-if="chartError" state="error" title="指数 K 线取不到" :detail="chartError" />
          <KlineChart v-else :data="chart" :is-loading="chartLoading" :error="null"
                      :show-signals="false" :height="560" simple />
          <p class="hint tiny chart-note">
            均线：MA5 / MA10 / MA20 / MA60；成交量按指数原始 vol。指数不含个股入选记号、筹码与威科夫读数。
          </p>
        </template>
      </div>

      <div class="panel">
        <div class="table-scroll">
          <table class="data-table">
            <thead>
              <tr><th class="text-left">指数</th><th class="text-left">代码</th><th>收盘</th>
                <th>涨跌幅</th><th>成交额</th><th>最高 / 最低</th><th class="text-left">走势</th></tr>
            </thead>
            <tbody>
              <tr v-for="q in overview.indices" :key="q.code" class="clickable"
                  :class="{ selected: q.code === selected }" @click="selectIndex(q.code)">
                <td class="text-left"><b>{{ q.name }}</b></td>
                <td class="text-left mono tiny dim">{{ q.code }}</td>
                <td class="mono">{{ fmt(q.close) }}</td>
                <td class="mono" :class="tone(q.pct_chg)">{{ pct(q.pct_chg) }}</td>
                <td class="dim">{{ money(q.amount_yi) }}</td>
                <td class="dim mono">{{ fmt(q.high) }} / {{ fmt(q.low) }}</td>
                <td class="text-left">
                  <Sparkline :values="(q.spark || []).map(x => x.close)" :height="20"
                             :color="(q.pct_chg ?? 0) >= 0 ? 'var(--state-bad)' : 'var(--state-ok)'" />
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="hint tiny">
          数据源：{{ overview.source.indices }}；成交额：{{ overview.source.turnover }}。
          成交额单位口径：{{ overview.source.turnover_unit }}。
        </p>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import PageHeader from '@/components/ui/PageHeader.vue'
import StateBlock from '@/components/ui/StateBlock.vue'
import StatTile from '@/components/ui/StatTile.vue'
import Sparkline from '@/components/ui/Sparkline.vue'
import KlineChart from '@/components/KlineChart.vue'
import IntradayChart from '@/components/IntradayChart.vue'
import TurnoverChart from '@/components/TurnoverChart.vue'
import { getMarketIndexIntraday, getMarketIndexOhlcv, getMarketOverview, getMarketTurnover } from '@/api/client'
import type { MarketIndexIntraday, MarketIndexOhlcv, MarketOverview, MarketTurnoverSeries } from '@/types/api'

const route = useRoute()
const router = useRouter()
const overview = ref<MarketOverview | null>(null)
const chart = ref<MarketIndexOhlcv | null>(null)
const intraday = ref<MarketIndexIntraday | null>(null)
const turnover = ref<MarketTurnoverSeries | null>(null)
const selected = ref(String(route.query.code || '000001.SH'))
const chartMode = ref<'intraday' | 'kline'>('intraday')
const days = ref(365)
const turnoverDays = ref(365)
const loading = ref(false)
const chartLoading = ref(false)
const intradayLoading = ref(false)
const turnoverLoading = ref(false)
const error = ref('')
const chartError = ref('')
const intradayError = ref('')
const turnoverError = ref('')
const ranges = [
  { label: '3M', days: 90 },
  { label: '6M', days: 180 },
  { label: '1Y', days: 365 },
  { label: '3Y', days: 1095 },
  { label: '全部', days: 9999 },
]
const turnoverRanges = ranges

const selectedQuote = computed(() => overview.value?.indices.find(x => x.code === selected.value) || null)
const activePrice = computed(() => chartMode.value === 'intraday'
  ? (intraday.value?.latest?.price ?? null)
  : (chart.value?.latest.close ?? null))
const activeChange = computed(() => chartMode.value === 'intraday'
  ? (intraday.value?.latest?.change_pct ?? null)
  : (chart.value?.latest.change_pct ?? null))
const upDown = computed(() => {
  const list = overview.value?.indices || []
  const up = list.filter(x => (x.pct_chg ?? 0) > 0).length
  const down = list.filter(x => (x.pct_chg ?? 0) < 0).length
  return `${up} / ${down}`
})
const trendText = computed(() => {
  const s = overview.value?.turnover_series || []
  const vals = s.map(x => x.amount_yi).filter((x): x is number => x != null)
  if (vals.length < 2) return '—'
  const delta = vals[vals.length - 1] - vals[0]
  return `${delta >= 0 ? '+' : ''}${delta.toFixed(1)} 亿`
})
const trendSub = computed(() => {
  const s = overview.value?.turnover_series || []
  return s.length ? `较 ${s[0].date} 的 ${money(s[0].amount_yi)}` : '样本不足'
})
const turnoverSub = computed(() => {
  const o = overview.value
  if (!o?.n_stocks) return 'clean 日线汇总'
  const stale = o.turnover_asof && o.turnover_asof !== o.asof ? ` · 截至 ${o.turnover_asof}` : ''
  return `${o.n_stocks.toLocaleString('zh-CN')} 只有成交${stale}`
})

function fmt(v: number | null) {
  return v == null ? '—' : v.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}
function pct(v: number | null) {
  return v == null ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
}
function money(v: number | null) {
  return v == null ? '—' : `${v.toLocaleString('zh-CN', { maximumFractionDigits: 1 })} 亿`
}
function tone(v: number | null) { return (v ?? 0) >= 0 ? 'up' : 'down' }

async function loadOverview() {
  loading.value = true; error.value = ''
  try {
    overview.value = await getMarketOverview(20)
    if (!overview.value.indices.some(x => x.code === selected.value)) {
      selected.value = overview.value.indices[0]?.code || selected.value
    }
  } catch (e) { error.value = String(e) }
  finally { loading.value = false }
}

async function loadChart() {
  chartLoading.value = true; chartError.value = ''
  try { chart.value = await getMarketIndexOhlcv(selected.value, days.value) }
  catch (e) { chartError.value = String(e); chart.value = null }
  finally { chartLoading.value = false }
}

async function loadIntraday() {
  intradayLoading.value = true; intradayError.value = ''
  try { intraday.value = await getMarketIndexIntraday(selected.value) }
  catch (e) { intradayError.value = String(e); intraday.value = null }
  finally { intradayLoading.value = false }
}

async function loadTurnover() {
  turnoverLoading.value = true; turnoverError.value = ''
  try { turnover.value = await getMarketTurnover(turnoverDays.value) }
  catch (e) { turnoverError.value = String(e); turnover.value = null }
  finally { turnoverLoading.value = false }
}

function selectIndex(code: string) {
  selected.value = code
  void router.replace({ path: '/market', query: { code } })
}

async function reload() {
  await loadOverview()
  await loadChart()
  await loadIntraday()
  await loadTurnover()
}

let intradayTimer: number | undefined
onMounted(async () => {
  await loadOverview()
  await loadChart()
  await loadIntraday()
  await loadTurnover()
  intradayTimer = window.setInterval(() => {
    if (chartMode.value === 'intraday' && !document.hidden) void loadIntraday()
  }, 30000)
})
onUnmounted(() => { if (intradayTimer) window.clearInterval(intradayTimer) })
watch(selected, () => { void loadChart(); void loadIntraday() })
watch(days, () => { void loadChart() })
watch(turnoverDays, () => { void loadTurnover() })
watch(() => route.query.code, (v) => {
  const code = String(v || '')
  if (code && code !== selected.value) selected.value = code
})
</script>

<style scoped>
.summary-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--sp-2); margin-bottom: var(--sp-3); }
.market-head { display: flex; align-items: flex-end; gap: var(--sp-4); flex-wrap: wrap; margin-bottom: var(--sp-2); }
.market-head b { margin-right: 8px; }
.turnover-mini { margin-left: auto; width: min(280px, 40vw); }
.turnover-mini :deep(.sparkline) { margin-top: 2px; }
.index-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--sp-2); }
.index-card {
  text-align: left; padding: var(--sp-2) var(--sp-3); border: 1px solid var(--border-color);
  border-radius: var(--r-3); background: var(--bg-input); color: var(--text-primary);
  cursor: pointer; transition: border-color .15s, background .15s;
}
.index-card:hover, .index-card.active { border-color: var(--accent-gold); background: rgba(255, 209, 102, .06); }
.ic-top, .ic-mid, .ic-foot { display: flex; align-items: baseline; justify-content: space-between; gap: 6px; }
.ic-name { font-weight: 700; }
.ic-code { color: var(--text-dim); }
.ic-price { font-size: var(--fs-xl); }
.ic-chg { font-size: var(--fs-sm); }
.ic-foot { margin-top: 2px; align-items: center; }
.ic-foot :deep(.sparkline) { width: 96px; opacity: .85; }
.chart-panel { padding-top: var(--sp-2); }
.chart-head { display: flex; align-items: center; gap: var(--sp-3); flex-wrap: wrap; margin-bottom: var(--sp-2); }
.chart-title { display: flex; align-items: baseline; gap: 8px; }
.chart-title b { font-size: var(--fs-lg); }
.chart-last { font-size: var(--fs-lg); }
.chart-tools { margin-left: auto; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.mode-tabs { display: flex; gap: 2px; }
.mode-tabs .tab { padding: 4px 12px; }
.range-tabs { margin-left: auto; display: flex; gap: 2px; }
.chart-tools .range-tabs { margin-left: 0; }
.range-tabs .tab { padding: 4px 10px; }
.chart-note { margin-top: var(--sp-2); }
@media (max-width: 1000px) {
  .summary-grid, .index-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 600px) {
  .summary-grid, .index-grid { grid-template-columns: 1fr; }
  .turnover-mini { width: 100%; margin-left: 0; }
}
</style>
