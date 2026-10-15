<template>
  <div class="page">
    <h4 class="page-title">K线图表</h4>
    <div class="two-col">
      <div class="col-side">
        <StockWatchlist v-model="selectedCode" />
        <!-- K 线自选的分组直接取各策略最新名单：这里挂同一份刷新契约，名单停更看得见 -->
        <StrategyListsStatusBar />
      </div>
      <div class="col-main">
    <ControlPanel
      :selected-code="selectedCode"
      :days="days"
      :overlays="overlays"
      @update:selected-code="onCode"
      @update:days="onDays"
      @update:overlays="onOverlays"
    />
    <div class="title-bar" v-if="data?.latest">
      <b>{{ data.code }} {{ data.name }}</b>
      &nbsp;收盘 {{ data.latest.close.toFixed(3) }}
      <span :class="data.latest.change_pct >= 0 ? 'up' : 'down'">
        &nbsp;{{ data.latest.change_pct >= 0 ? '+' : '' }}{{ data.latest.change_pct.toFixed(2) }}%
      </span>
      <span class="bar-count">({{ data.latest.total_bars }}根K线)</span>
      <span v-if="data.profile" class="profile">
        · {{ data.profile.board }}<template v-if="data.profile.industry"> / {{ data.profile.industry }}</template>
        <template v-if="data.profile.list_date"> · 上市 {{ data.profile.list_date }}</template>
        <span v-for="f in data.profile.flags" :key="f" class="flag" :class="flagClass(f)">· {{ f }}</span>
      </span>
    </div>
    <div class="stage-bar" v-if="data?.stage && data.stage.stage !== 'insufficient'">
      <span class="stage-chip" :class="'stage-' + data.stage.stage">{{ data.stage.label }}</span>
      <span class="stage-why">{{ data.stage.evidence }}</span>
    </div>
    <!-- 逐股 AI 多空综合分析（买入/持有/卖出）：与 K 线同页，用户要求 -->
    <AiAnalysisPanel :code="selectedCode" />
    <div class="chart-area" v-if="data && data.data.length > 0">
      <div class="chart-row">
        <KlineChart
          :data="data" :is-loading="isLoading" :error="error"
          :show-signals="overlays.includes('signals')" :height="600"
          :selected-date="pickedDate"
          @price-range="onPriceRange"
          @pick="onPickDate"
        />
        <ChipDistribution
          v-if="overlays.includes('chips') && data.chips"
          :chips="data.chips" :height="600" :price-range="priceRange"
        />
      </div>
      <p class="hint tiny click-hint">
        💡 点任意一根 K 线 = 看这一天的分时（本地 5 分钟归档 <b>{{ coverageText }}</b>；更近的交易日走实时源）。
        当前选中：<b class="link" @click="focusSelected">{{ pickedDate || '—' }}</b>
      </p>
    </div>
    <div v-else-if="!selectedCode" class="empty-state">请选择股票代码</div>
    <div v-else-if="isLoading" class="empty-state">加载中...</div>
    <div v-else class="empty-state">暂无行情数据</div>

    <!-- 威科夫点图（P&F）+ 阶段速读（用户 2026-10-14 要求补在 K 线页） -->
    <div class="panel wyckoff-panel" v-if="selectedCode">
      <h6>
        威科夫点图（P&amp;F）
        <span class="tag" :class="wyckoffTone" v-if="wyckoff?.available">{{ wyckoff.phase_label }}</span>
        <span class="dim tiny" v-if="wyckoff?.available">
          近 {{ wyckoffRangeDays }} 日区间 {{ fmt(wyckoff.support) }} ~ {{ fmt(wyckoff.resistance) }}
          · 横向计数目标 {{ fmt(wyckoff.count_target) }}
          <template v-if="wyckoff.volume_ratio">· 量能 {{ wyckoff.volume_ratio }}× 20 日均量</template>
        </span>
        <span class="wk-controls">
          <label class="tiny dim">箱 %<input v-model.number="wyckoffBoxPct" type="number" step="0.5" min="0.5" max="10" /></label>
          <label class="tiny dim">反转箱<input v-model.number="wyckoffReversal" type="number" step="1" min="1" max="5" /></label>
          <label class="tiny dim">区间天数<input v-model.number="wyckoffRangeDays" type="number" step="10" min="20" max="250" /></label>
          <button class="btn-ghost btn-sm" :disabled="wyckoffLoading" @click="loadWyckoff">重算</button>
        </span>
      </h6>
      <StateBlock v-if="wyckoffLoading" state="loading" title="算点数图…" />
      <StateBlock v-else-if="wyckoffError" state="error" title="威科夫读数取不到" :detail="wyckoffError" />
      <template v-else-if="wyckoff?.available">
        <p class="tiny dim wk-note">{{ wyckoff.phase_note }}</p>
        <WyckoffChart :read="wyckoff" :height="380" />
        <div class="wk-evidence">
          <div v-for="(e, i) in wyckoff.evidence || []" :key="i" class="wk-ev">
            <span class="tag tiny" :class="evidenceTone(e.type)">{{ evidenceLabel(e.type) }}</span>
            <span class="tiny">{{ e.detail }}</span>
          </div>
        </div>
        <!-- 事件概率云（民生证券 2025-01 公式）：当前 K 线的候选事件似然度 + 概率坍缩 + WSO 分数 -->
        <div class="wk-cloud" v-if="wyckoff.event_cloud?.available">
          <div class="wk-cloud-head">
            <b>事件概率云</b>
            <span class="tag tiny" :class="cloudTone">
              {{ wyckoff.event_cloud.collapsed
                  ? `坍缩：${wyckoff.event_cloud.collapsed.label}（${(wyckoff.event_cloud.collapsed.prob * 100).toFixed(0)}%）`
                  : `今日无显著形态（阈值 ${((wyckoff.event_cloud.threshold || 0.6) * 100).toFixed(0)}%）` }}
            </span>
            <span class="pill tiny">WSO <b :class="(wyckoff.event_cloud.score?.wso || 0) >= 0 ? 'up' : 'down'">
              {{ (wyckoff.event_cloud.score?.wso ?? 0).toFixed(3) }}</b>
              <span class="dim">（半衰期 {{ wyckoff.event_cloud.score?.half_life }} 日 · {{ wyckoff.event_cloud.score?.n_events }} 个坍缩事件）</span>
            </span>
          </div>
          <table class="data-table tiny">
            <thead><tr><th class="left">当前 K 线候选事件</th><th class="left">似然度</th><th class="left">方向</th></tr></thead>
            <tbody>
              <tr v-for="e in cloudRows" :key="e.type">
                <td class="left"><b>{{ e.type }}</b> {{ e.label }}
                  <span v-if="e.pending" class="tag tiny" title="末根 K 线之后还没有 K 线 ⇒ 尚未确认">待确认</span>
                </td>
                <td class="left">
                  <span class="wk-bar" :style="{ width: Math.max(2, e.prob * 100) + '%' }"></span>
                  <span class="mono">{{ (e.prob * 100).toFixed(1) }}%</span>
                </td>
                <td class="left" :class="e.side > 0 ? 'up' : 'down'">{{ e.side > 0 ? '偏多 +1' : '偏空 −1' }}</td>
              </tr>
            </tbody>
          </table>
          <p class="hint tiny">
            已实现 <b>{{ (wyckoff.event_cloud.implemented || []).length }}</b> 个事件代码
            （研报 §2.3 的 25 行全部落地；SOT / AR&ST 按多空各一支）<span
              v-if="(wyckoff.event_cloud.not_implemented || []).length">；仍未实现：
              <span class="mono">{{ (wyckoff.event_cloud.not_implemented || []).join(' / ') }}</span></span>。
            来源：{{ wyckoff.event_cloud.source }}
          </p>
          <p class="hint tiny" v-if="wyckoff.event_cloud.structure">
            结构：<b>{{ wyckoff.event_cloud.structure.pattern }}</b>
            （端点 {{ wyckoff.event_cloud.structure.endpoints }} 个 ·
            {{ wyckoff.event_cloud.structure.endpoint_source === 'swing_fallback' ? 'MACD 波段端点不足，用摆动高低点兜底' : '研报 §2.1 MACD 波段端点' }} ·
            辅助线 {{ wyckoff.event_cloud.structure.lines }} 条）
            <span v-if="wyckoff.event_cloud.structure.ob_line">· 超买线 {{ wyckoff.event_cloud.structure.ob_line }}</span>
            <span v-if="wyckoff.event_cloud.structure.os_line">· 超卖线 {{ wyckoff.event_cloud.structure.os_line }}</span>
          </p>
          <details class="hint tiny" v-if="(wyckoff.event_cloud.calibers || []).length">
            <summary>口径自定 {{ (wyckoff.event_cloud.calibers || []).length }} 处（研报没给构造细节，如实列明）</summary>
            <ul><li v-for="(c, i) in wyckoff.event_cloud.calibers" :key="i">{{ c }}</li></ul>
          </details>
        </div>
        <p class="hint tiny mt">{{ wyckoff.methodology }} {{ wyckoff.disclaimer }}</p>
      </template>
      <p v-else class="hint tiny">点数图不可用：{{ wyckoff?.reason || '—' }}</p>
    </div>

    <!-- 分时面板：K 线页点选的交易日 -->
    <div class="panel intraday-panel" v-if="selectedCode && (pickedDate || intraday || intradayError)">
      <h6>
        当日分时
        <b class="link" @click="focusSelected">{{ pickedDate }}</b>
        <span class="tag" :class="intradayTagClass" v-if="intraday">{{ intraday.source_label }}</span>
        <span class="dim tiny" v-if="intraday">
          前收 {{ fmt(intraday.prev_close) }} · {{ intraday.points.length }} 个点 ·
          {{ intraday.points[0]?.clock }} → {{ intraday.points[intraday.points.length - 1]?.clock }}
        </span>
        <button class="btn-ghost btn-sm close-btn" @click="clearIntraday">收起</button>
      </h6>

      <StateBlock v-if="loadingIntraday" state="loading" title="加载该日分时…" />
      <StateBlock v-else-if="intradayError" state="error" title="该交易日没有分时数据" :detail="intradayError" />
      <template v-else-if="intraday">
        <!-- 标头：这是**哪只票**的分时（用户 2026-10-13："点中某只股票的时候，分时线看不到股票标头"）。
             数据取当前 K 线标的（data）+ 分时口径的最新价/涨跌幅，滚动到面板时一眼知道看的是谁。 -->
        <div class="intraday-head">
          <b class="ih-code mono">{{ data?.code || selectedCode }}</b>
          <b class="ih-name">{{ data?.name || '—' }}</b>
          <span class="ih-px" :class="tone(intraday.latest.change_pct)">
            {{ fmt(intraday.latest.price) }} {{ pct(intraday.latest.change_pct) }}
          </span>
          <span class="ih-tags tiny dim">
            <span v-if="data?.profile?.industry">{{ data.profile.industry }} · </span>
            <span v-if="data?.profile?.board">{{ data.profile.board }} · </span>
            <span>分时标的</span>
          </span>
          <span class="ih-time tiny dim" v-if="intraday.points.length">
            {{ intraday.points[0]?.clock }} → {{ intraday.points[intraday.points.length - 1]?.clock }}
          </span>
        </div>
        <div class="stat-grid">
          <StatTile label="收盘/最新" :value="fmt(intraday.latest.price)"
                    :tone="tone(intraday.latest.change_pct)"
                    :sub="`前收 ${fmt(intraday.prev_close)}`" />
          <StatTile label="涨跌幅" :value="pct(intraday.latest.change_pct)"
                    :tone="tone(intraday.latest.change_pct)"
                    :sub="`涨跌 ${fmt(intraday.latest.change)}`" />
          <StatTile label="均价" :value="fmt(intraday.latest.avg)" tone="gold" sub="累计额 ÷ 累计量" />
          <StatTile label="最高/最低" :value="`${fmt(intraday.latest.high)} / ${fmt(intraday.latest.low)}`"
                    :sub="`开盘 ${fmt(intraday.latest.open)}`" />
          <StatTile label="成交额" :value="money(intraday.latest.amount_yuan)"
                    :sub="intraday.latest.amount_yuan === null ? '该源不提供' : '元 → 亿/万'" />
          <StatTile label="成交量" :value="shares(intraday.latest.volume_shares)"
                    :sub="`${intraday.period} 分钟`" />
        </div>
        <IntradayChart :points="intraday.points" :prev-close="intraday.prev_close" :height="300" />
        <p class="hint tiny" v-if="!intraday.live.available && intraday.live.reason">
          实时源不可用，用的是本地归档：<span class="dim">{{ intraday.live.reason }}</span>
        </p>
        <p class="hint tiny" v-if="intraday.latest.amount_yuan === null">
          该源不给成交额（腾讯 5 分钟 K 接口限制）→ 成交额与均价线留空。
        </p>
      </template>
    </div>

    <SignalStats :stats="data?.signal_stats ?? null" />
    <StockInfoPanel :code="selectedCode"
                    @update:code="(c: string) => { if (c) selectedCode = c }" />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import ControlPanel from '@/components/ControlPanel.vue'
import KlineChart from '@/components/KlineChart.vue'
import ChipDistribution from '@/components/ChipDistribution.vue'
import SignalStats from '@/components/SignalStats.vue'
import StatTile from '@/components/ui/StatTile.vue'
import StateBlock from '@/components/ui/StateBlock.vue'
import IntradayChart from '@/components/IntradayChart.vue'
import StockWatchlist from '@/components/StockWatchlist.vue'
import StrategyListsStatusBar from '@/components/StrategyListsStatusBar.vue'
import WyckoffChart from '@/components/WyckoffChart.vue'
import StockInfoPanel from '@/components/StockInfoPanel.vue'
import AiAnalysisPanel from '@/components/AiAnalysisPanel.vue'
import { useKlineData } from '@/composables/useKlineData'
import { getIntraday, getIntradayCoverage, getWyckoff } from '@/api/client'
import type { IntradayCoverage, IntradayResponse, WyckoffRead } from '@/types/api'

const route = useRoute()
const router = useRouter()
const { data, isLoading, error, stockCode, days, showSignals, showChips } = useKlineData()
const selectedCode = ref<string | null>(null)
const daysModel = ref(365)
const overlays = ref<string[]>(['signals', 'chips'])
const priceRange = ref<[number, number]>([0, 100])

const pickedDate = ref('')
const intraday = ref<IntradayResponse | null>(null)
const intradayError = ref('')
const loadingIntraday = ref(false)
const coverage = ref<IntradayCoverage | null>(null)

watch(selectedCode, v => { stockCode.value = v })
watch(daysModel, v => { days.value = v })
watch(overlays, v => { showSignals.value = v.includes('signals'); showChips.value = v.includes('chips') })

function onCode(v: string) { selectedCode.value = v }
function onDays(v: number) { daysModel.value = v }
function onOverlays(v: string[]) { overlays.value = v }
function onPriceRange(r: [number, number]) { priceRange.value = r }

function flagClass(flag: string) {
  if (flag.startsWith('已退市') || flag.startsWith('ST')) return 'flag-bad'
  if (flag.startsWith('次新')) return 'flag-warn'
  return 'flag-info'
}

// ── 分时：点 K 线 → 拉当天分时 ────────────────────────────────────────────────
const coverageText = computed(() => {
  const store = coverage.value?.store
  if (!store?.available) return '缺失（跑 sync_cn_intraday_full.py 补）'
  return `${store.first_date} ~ ${store.last_date}`
})
const intradayTagClass = computed(() => (intraday.value?.source === 'store' ? '' : 'ok'))

/** 这一天到底有没有分时（本地归档 ∪ 实时源覆盖）。没有就直接说明，不发注定 404 的请求。 */
function hasData(date: string) {
  if (!date) return false
  const local = coverage.value?.store_dates || []
  const live = coverage.value?.live?.dates || []
  return local.includes(date) || live.includes(date)
}

async function onPickDate(date: string) {
  if (!date || date === pickedDate.value) return
  pickedDate.value = date
  await loadIntraday(date)
}

async function loadIntraday(date: string) {
  intraday.value = null
  intradayError.value = ''
  if (!hasData(date)) {
    const store = coverage.value?.store
    const live = coverage.value?.live
    intradayError.value =
      `${date} 没有分时数据。`
      + (store?.available ? `本地归档覆盖 ${store.first_date} ~ ${store.last_date}（共 ${store.n_dates} 个交易日）；` : '本地归档缺失；')
      + (live?.available ? `实时源只覆盖 ${live.dates[0]} ~ ${live.dates[live.dates.length - 1]}。` : '实时源当前不可用。')
    return
  }
  loadingIntraday.value = true
  try {
    intraday.value = await getIntraday(selectedCode.value || '', '1', 'auto', date)
    intradayUpdatedAt.value = new Date()
  } catch (e) {
    intradayError.value = String(e)      // 保留上一次成功数据，只标错
  } finally {
    loadingIntraday.value = false
  }
  startIntradayPoll()
}

// ── 分时"实时推进"（2026-10-12）：只在**当日/最近交易日**且**交易时段**轮询；页面隐藏即停 ──
// 与自选侧栏的 useLiveQuotes 同一套门控口径（phase 由后端给），避免两处各写一套判断。
const liveTimer = ref<ReturnType<typeof setInterval> | null>(null)
const intradayUpdatedAt = ref<Date | null>(null)
const liveQuotesSession = ref<{ phase?: string } | null>(null)

function intradayPollable(date: string): boolean {
  const target = String(date || '')
  // `coverage.today` 就是"最近交易日"（后端 `IntradayCoverage.today`）；看历史某天时不动它。
  const latest = String(coverage.value?.today || '')
  if (!target || !latest || target !== latest) return false      // 看历史某天时不动它
  const phase = liveQuotesSession.value?.phase
  return phase === 'am' || phase === 'pm' || phase === 'auction'
}

function stopIntradayPoll() {
  if (liveTimer.value) clearInterval(liveTimer.value)
  liveTimer.value = null
}

function startIntradayPoll() {
  stopIntradayPoll()
  const target = pickedDate.value || String(coverage.value?.today || '')
  if (document.hidden || !intradayPollable(target)) return
  liveTimer.value = setInterval(() => {
    if (!document.hidden) void loadIntraday(pickedDate.value || String(coverage.value?.today || ''))
  }, 30000)
}

function clearIntraday() {
  pickedDate.value = ''
  intraday.value = null
  intradayError.value = ''
}

/** 默认看哪一天：**优先今天**（只要实时源今天有数据），否则退回"日线最后一根"（= 最近一个交易日）。

2026-10-12 修：原来两处都默认 `data.data[last]`（日线最后一根 = 本地/前收日），于是"今天上午的分时"
根本不显示 —— 实测后端 `mode=live` 明明有今天 121 根 1 分钟线（腾讯分时），页面却停在 10-09 的归档上。
*/
function preferredIntradayDate(): string {
  const today = String(coverage.value?.today || '')
  if (today && hasData(today)) return today
  return String(data.value?.data?.[data.value.data.length - 1]?.time || '')
}

/** K 线区间里还没有选中日期时，默认看最近一个交易日 */
watch(() => data.value, (d) => {
  if (d && d.data.length && !pickedDate.value) {
    const last = preferredIntradayDate()
    pickedDate.value = last
    void loadIntraday(last)
  }
})

/** 换票：清掉旧分时，重新取一次覆盖范围 */
watch(selectedCode, async (code) => {
  clearIntraday()
  coverage.value = null
  if (!code) return
  try {
    coverage.value = await getIntradayCoverage(code)
    // 上一次选的日期可能在新票的覆盖范围外 → 默认回到"今天（若有实时）或最近交易日"
    const last = preferredIntradayDate()
    if (last) { pickedDate.value = last; void loadIntraday(last) }
  } catch { /* 覆盖范围拿不到时，点击仍会退回后端 404 文案 */ }
})

function focusSelected() {
  const el = document.querySelector('.intraday-panel')
  el?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

const fmt = (v: number | null | undefined, nd = 2) =>
  v === null || v === undefined ? '—' : Number(v).toFixed(nd)
const pct = (v: number | null | undefined) =>
  v === null || v === undefined ? '—' : `${Number(v) >= 0 ? '+' : ''}${(Number(v) * 100).toFixed(2)}%`
const tone = (v: number | null | undefined) => (v === null || v === undefined ? '' : Number(v) >= 0 ? 'up' : 'down')
function money(yuan: number | null | undefined) {
  if (yuan === null || yuan === undefined) return '—'
  if (yuan >= 1e8) return (yuan / 1e8).toFixed(2) + ' 亿'
  if (yuan >= 1e4) return (yuan / 1e4).toFixed(0) + ' 万'
  return yuan.toFixed(0)
}
function shares(n: number | null | undefined) {
  if (!n) return '—'
  if (n >= 1e8) return (n / 1e8).toFixed(2) + ' 亿股'
  if (n >= 1e4) return (n / 1e4).toFixed(0) + ' 万股'
  return n + ' 股'
}

// 支持从别处跳转：/#/kline?code=301230.SZ
onMounted(async () => {
  // 轻量拉一次"当前时段"，用于分时轮询的门控（失败不影响页面其它部分）
  try {
    const overview = await (await fetch('/api/rankings/overview')).json()
    liveQuotesSession.value = { phase: overview?.session?.phase }
  } catch { /* 忽略：拿不到时段就不轮询 */ }
})
onMounted(() => {
  const code = String(route.query.code || '')
  if (code) selectedCode.value = code
})
watch(() => route.query.code, v => { if (v && String(v) !== selectedCode.value) selectedCode.value = String(v) })
// 页面内换票（自选股侧栏 / 手动输入）也写回 URL：刷新、分享、从别的页返回都停在同一只票。
// 不回写的话，用侧栏切了 5 只票再刷新，又回到最开始的代码（实测踩过）。
watch(selectedCode, (code) => {
  if (!code || String(route.query.code || '') === code) return
  void router.replace({ query: { ...route.query, code } })
})

// ── 威科夫点图（P&F）：随选中标的自动算，箱 %/反转箱/区间天数可调 ───────────────
const wyckoff = ref<WyckoffRead | null>(null)
/** 概率云面板只渲染似然度靠前的候选（接口仍返回全部 27 个代码；一屏排不下，
 *  低似然度的候选也不该抢视觉权重）。 */
const cloudRows = computed(() => {
  const rows = wyckoff.value?.event_cloud?.cloud || []
  const shown = rows.filter((e) => e.prob >= 0.05)
  return shown.length > 12 ? shown.slice(0, 12) : shown
})
const wyckoffLoading = ref(false)
const wyckoffError = ref('')
const wyckoffBoxPct = ref(2)          // 页面用"百分比"输入，接口要小数
const wyckoffReversal = ref(3)
const wyckoffRangeDays = ref(60)
const wyckoffTone = computed(() => {
  const phase = wyckoff.value?.phase || ''
  if (phase === 'markup') return 'ok'
  if (phase === 'accumulation') return 'info'
  if (phase === 'markup_watch') return 'warn'
  if (phase === 'distribution' || phase === 'markdown') return 'bad'
  return 'dim'
})
const EVIDENCE_LABEL: Record<string, string> = {
  range: '区间', context: '位置', spring: 'Spring', upthrust: 'Upthrust', sos: 'SOS',
}
const EVIDENCE_TONE: Record<string, string> = {
  range: 'dim', context: 'dim', spring: 'ok', upthrust: 'bad', sos: 'warn',
}
const cloudTone = computed(() => {
  const c = wyckoff.value?.event_cloud?.collapsed
  if (!c) return 'dim'
  return c.side > 0 ? 'ok' : 'bad'
})
const evidenceLabel = (type: string) => EVIDENCE_LABEL[type] || type
const evidenceTone = (type: string) => EVIDENCE_TONE[type] || 'dim'

async function loadWyckoff() {
  if (!selectedCode.value) return
  wyckoffLoading.value = true
  wyckoffError.value = ''
  try {
    wyckoff.value = await getWyckoff(selectedCode.value, {
      days: 250,
      box_pct: Math.min(Math.max(Number(wyckoffBoxPct.value) || 2, 0.5), 10) / 100,
      reversal: Math.min(Math.max(Math.round(Number(wyckoffReversal.value) || 3), 1), 5),
      range_days: Math.min(Math.max(Math.round(Number(wyckoffRangeDays.value) || 60), 20), 250),
    })
  } catch (err) {
    wyckoffError.value = String(err)
  } finally {
    wyckoffLoading.value = false
  }
}
watch(selectedCode, (code) => { if (code) loadWyckoff() })
onMounted(() => { if (selectedCode.value) loadWyckoff() })

onUnmounted(() => { stopIntradayPoll() })
</script>

<style scoped>
/* 左侧自选股在长页面上保持可见：K 线页要滚很久（图 + 分时 + 资料），
   侧栏跟着滚走的话，用户每次换票都得先滚回顶部。 */
.col-side { position: sticky; top: 64px; align-self: flex-start; }
@media (max-width: 1100px) { .col-side { position: static; } }
.click-hint { margin-top: var(--sp-2); }
.intraday-panel h6 { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.wyckoff-panel h6 { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.wk-controls { margin-left: auto; display: inline-flex; align-items: center; gap: var(--sp-2); }
.wk-controls input { width: 56px; margin-left: 4px; background: var(--bg-input); border: 1px solid var(--border-color);
  color: inherit; border-radius: 4px; padding: 2px 4px; font-size: var(--fs-xs); }
.wk-note { margin: 2px 0 var(--sp-2); }
.wk-evidence { display: flex; flex-direction: column; gap: 3px; margin-top: var(--sp-2); }
.wk-ev { display: flex; gap: 6px; align-items: baseline; }
.wk-cloud { margin-top: var(--sp-3); }
.wk-cloud-head { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; margin-bottom: 4px; }
.wk-bar { display: inline-block; height: 8px; background: var(--accent-gold); border-radius: 2px;
  margin-right: 6px; vertical-align: middle; }
.intraday-head { display: flex; align-items: baseline; gap: var(--sp-2); flex-wrap: wrap; margin: 2px 0 var(--sp-2); }
.intraday-head .ih-code { font-size: var(--fs-md); }
.intraday-head .ih-name { font-size: var(--fs-md); color: var(--accent-gold); }
.intraday-head .ih-px { font-family: var(--font-mono); font-size: var(--fs-sm); }
.intraday-head .ih-time { margin-left: auto; }
.close-btn { margin-left: auto; }
.intraday-panel .stat-grid { margin-bottom: var(--sp-3); }
</style>
