<template>
  <div class="page">
    <h4 class="page-title">K线图表</h4>
    <div class="two-col">
      <div class="col-side">
        <StockWatchlist v-model="selectedCode" :month="miniMonth" />
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
    <!-- 迷你选股日历条（可翻月；金点 = 台账里被策略选中过；悬停看当天明细；点一下切到那天） -->
    <div class="mini-strip" v-if="barDates.length">
      <span class="ms-label tiny dim">入选日历</span>
      <button class="ms-nav" title="上一个月" @click="shiftMiniMonth(-1)">◀</button>
      <span class="ms-month mono tiny">{{ miniMonth }}</span>
      <button class="ms-nav" title="下一个月" @click="shiftMiniMonth(1)">▶</button>
      <button class="ms-now tiny" title="回到最新交易日" @click="backToLatest">最新</button>
      <button v-for="d in miniDays" :key="d.date" class="ms-cell"
              :class="{ picked: d.date === pickedDate, hit: d.hits > 0 }"
              :title="d.hits ? `${d.date}：被 ${d.hits} 条策略选中（悬停看明细）` : d.date"
              @click="onPickDate(d.date)"
              @mouseenter="onMiniHover(d)"
              @mouseleave="hoverDay = null"
              @focus="onMiniHover(d)"
              @blur="hoverDay = null">
        <span class="ms-day tiny">{{ d.date.slice(5) }}</span>
        <span class="ms-dot" v-if="d.hits > 0"></span>
      </button>
      <span class="tiny dim" v-if="!miniDays.length"
            title="这一档 K 线区间里没有这个月的 K 线：换区间或点「最新」回来">
        这个月没有 K 线（区间 {{ barDates[0] }} ~ {{ barDates[barDates.length - 1] }}）
      </span>
      <span class="tiny dim" v-if="selectionRows.length">
        共 {{ selectionRows.length }} 条台账记录（选股记录 tab 可看 T+1/T+5/T+20）
      </span>
      <span class="tiny dim" v-else>台账里没有这只票的选股记录</span>
      <!-- 最新入选的快捷入口：台账里最老的一条可能是几年前（如 2017-08-16），
           迷你条只在已加载的 K 线区间内翻月 ⇒ 这里只提供"区间内最近一次入选"的一跳。 -->
      <button class="ms-now tiny" v-if="latestMark"
              :title="`跳到 ${latestMark.date}（${strategyLabel(latestMark.source)}）`"
              @click="jumpToDate(latestMark.date)">
        最近入选 {{ latestMark.date.slice(5) }}
      </button>
      <!-- 悬停某天的明细浮层：策略名 + rank + 权重 + T+5 超额，并可跳日历看当天全部 -->
      <SelectionDetailCard v-if="hoverRows.length" :date="hoverDay || ''" :rows="hoverRows"
                            :names="strategyNames" :month="miniMonth" />
    </div>
    <!-- 当前选中日（点 K 线 / 点日历条那一格）的入选策略速读条：不用悬停也能看到"这天是谁选的" -->
    <div class="sel-bar" v-if="pickedRows.length">
      <span class="tiny dim">当日入选 <b class="mono">{{ pickedDate }}</b></span>
      <span v-for="r in pickedRows" :key="r.source + r.decision_date" class="sel-chip"
            :title="`${strategyLabel(r.source)}｜rank ${r.rank ?? '—'}${r.weight ? '｜权重 ' + (r.weight * 100).toFixed(1) + '%' : ''}｜${fwText(r)}`">
        <span class="tag tiny">{{ strategyLabel(r.source) }}</span>
        <span class="tiny dim" v-if="r.rank != null">#{{ r.rank }}</span>
        <span class="tiny" :class="tone(r.excess_r5 ?? r.r5 ?? r.r1)" v-if="fwAvailable(r)">
          {{ r.excess_r5 != null ? '超额' : 'T+5' }} {{ pct(r.excess_r5 ?? r.r5) }}</span>
      </span>
      <router-link class="link tiny" :to="{ path: '/calendar', query: { date: pickedDate, month: miniMonth } }">
        当日全部 →</router-link>
    </div>
    <div class="chart-area" v-if="data && data.data.length > 0">
      <div class="chart-row">
        <KlineChart
          :data="data" :is-loading="isLoading" :error="error"
          :show-signals="overlays.includes('signals')" :height="600"
          :selected-date="pickedDate"
          :selection-marks="selectionMarks"
          :selection-rows="selectionRows"
          :selection-names="strategyNames"
          :selection-month="miniMonth"
          :name="data.name"
          @price-range="onPriceRange"
          @pick="onPickDate"
          @selection-hover="onChartSelectionHover"
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
import SelectionDetailCard, { type SelectionRow } from '@/components/SelectionDetailCard.vue'
import { useKlineData } from '@/composables/useKlineData'
import { mergeStrategyNames, readSharedMonth, writeSharedMonth } from '@/composables/selectionLabels'
import { getIntraday, getIntradayCoverage, getStockBundle, getWyckoff } from '@/api/client'
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
    // ⚠️ 显式带了 ?date=（从选股日历 / 选股记录跳进来）就以它为准；否则默认"今天（若有实时）或最近交易日"。
    // 不这么写的话这里会把 query date 覆盖掉（实测：?date=2026-10-13 被改成 10-15，迷你日历条没有选中格）。
    const queryDate = String(route.query.date || '')
    const last = queryDate || preferredIntradayDate()
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
// ?date=：从选股日历 / 选股记录跳过来时定位到某一天（2026-10-15 加）
// ⚠️ `immediate: true` 必须有：首次进入（URL 里就带 ?date=）时 watcher 不 immediate 就不会触发，
// 结果被"默认最近交易日"覆盖掉（实测：?date=2026-10-13 显示成 10-14）。
watch(() => route.query.date, v => {
  const day = String(v || '')
  if (day && day !== pickedDate.value) void onPickDate(day)
}, { immediate: true })

watch(pickedDate, (day) => {
  const cur = String(route.query.date || '')
  if (!day || cur === day) return
  void router.replace({ query: { ...route.query, date: day } })
})

const barDates = computed(() => (data.value?.data || []).map((b) => String(b.time || '')).filter(Boolean))
const miniDays = computed(() => {
  const days = barDates.value
  if (!days.length || !miniMonth.value) return []
  const hits = new Map<string, number>()
  for (const r of selectionRows.value) hits.set(r.decision_date, (hits.get(r.decision_date) || 0) + 1)
  return days.filter((d) => d.slice(0, 7) === miniMonth.value).map((day) => ({ date: day, hits: hits.get(day) || 0 }))
})
/** 翻月：只在已加载的 K 线区间内；再往前到头就自动把区间放成「全部」（免得看起来像没数据） */
function shiftMiniMonth(step: number) {
  const days = barDates.value
  if (!days.length) return
  const base = miniMonth.value || days[days.length - 1].slice(0, 7)
  const [y, m] = base.split('-').map(Number)
  const dt = new Date(y, m - 1 + step, 1)
  const key = `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, '0')}`
  if (key < days[0].slice(0, 7)) { if (daysModel.value !== 0) daysModel.value = 0; miniMonth.value = key; return }
  if (key > days[days.length - 1].slice(0, 7)) return
  miniMonth.value = key
}
function backToLatest() {
  const days = barDates.value
  if (days.length) miniMonth.value = days[days.length - 1].slice(0, 7)
}
// 页面内换票（自选股侧栏 / 手动输入）也写回 URL：刷新、分享、从别的页返回都停在同一只票。
// 不回写的话，用侧栏切了 5 只票再刷新，又回到最开始的代码（实测踩过）。
watch(selectedCode, (code) => {
  if (!code || String(route.query.code || '') === code) return
  void router.replace({ query: { ...route.query, code } })
})

// ── 台账「选股记录」→ K 线上的入选记号 + 迷你日历条（把选股日历搬进 K 线的核心）──
const selectionRows = ref<SelectionRow[]>([])
/** 迷你条当前看的月份（默认 = 最后一根 K 线所在月）；可 ◀▶ 翻月 */
const miniMonth = ref('')
const hoverDay = ref<string | null>(null)
/** 策略 id → 中文名（策略池一份；含 arm/wyckoff/主力追踪等） */
const strategyNames = ref<Record<string, string>>({})
const hoverRows = computed(() => hoverDay.value
  ? selectionRows.value.filter((r) => r.decision_date === hoverDay.value) : [])
/** 当前**选中日**的入选明细（点 K 线的每一天 / 迷你条每一格都会换）——
 *  比"悬停才看得到"更进一步：选中那天就一直显示是谁选的（2026-10-15 第九十八轮）。 */
const pickedRows = computed(() => pickedDate.value
  ? selectionRows.value.filter((r) => r.decision_date === pickedDate.value) : [])
const fwAvailable = (r: SelectionRow) =>
  r.excess_r5 != null || r.r5 != null || r.r1 != null || r.r20 != null
function fwText(r: SelectionRow): string {
  if (r.excess_r5 != null) return `T+5 超额 ${pct(r.excess_r5)}`
  if (r.r5 != null) return `T+5 收益 ${pct(r.r5)}`
  if (r.r1 != null) return `T+1 收益 ${pct(r.r1)}`
  if (r.r20 != null) return `T+20 收益 ${pct(r.r20)}`
  return 'T+5 待结算'
}
function strategyLabel(src: string): string {
  const key = String(src || '')
  return strategyNames.value[key] || key || '—'
}
/** 台账里最新的一条入选（接口按决策日倒序）⇒ 迷你条给一个"一跳到底"的入口 */
/** 迷你条悬停：悬停"最近入选"按钮时也要能出明细（它本身就是一条入选记录） */
function onMiniHover(d: { date: string; hits: number }) {
  hoverDay.value = d.hits ? d.date : null
}
/** 跳到某个月（越界就先把 K 线区间放成「全部」）：从"最近入选"按钮用 */
function jumpToDate(day: string) {
  const days = barDates.value
  if (!days.length || !day) return
  const key = day.slice(0, 7)
  if (key < days[0].slice(0, 7)) {
    if (daysModel.value !== 0) daysModel.value = 0
    miniMonth.value = key
    return
  }
  if (key > days[days.length - 1].slice(0, 7)) return
  miniMonth.value = key
}
/** 图上入选记号的悬停：把迷你条的浮层收起来（避免两张卡同时挂在页面上） */
function onChartSelectionHover(date: string | null) {
  if (date) hoverDay.value = null
}
const selectionMarks = computed(() => {
  const byDate = new Map<string, string[]>()
  for (const r of selectionRows.value) {
    const list = byDate.get(r.decision_date) || []
    if (!list.includes(r.source)) list.push(r.source)
    byDate.set(r.decision_date, list)
  }
  return [...byDate.entries()].map(([date, sources]) => ({
    date,
    text: sources.length > 1 ? `入选×${sources.length}` : '入选',
  }))
})
watch(selectedCode, async (code) => {
  if (!code) { selectionRows.value = []; return }
  try {
    const bundle = await getStockBundle(code)
    selectionRows.value = (bundle.selections?.rows || []) as SelectionRow[]
  } catch {
    selectionRows.value = []          // 拿不到就当没有记录，不阻断 K 线
  }
}, { immediate: true })
/** 迷你条里"最近一次入选"的快捷跳转目标：取**当前 K 线区间内**最新的一条入选记录。
 *  两条纪律：① 台账里的记录可能早于 K 线区间（实测 300229.SZ 的 2025-10-31 在 1 年区间之外），
 *  那种日期点了没反应 ⇒ 只在"图上真有记号"的日期里挑；② 接口按决策日**倒序**返回，
 *  所以必须按日期取最大值，不能取数组最后一项（实测踩过：拿到了 2017-08-16）。 */
const latestMark = computed(() => {
  const sorted = selectionMarks.value.map((m) => m.date).sort()
  if (!sorted.length) return null
  const date = sorted[sorted.length - 1]
  const row = selectionRows.value.find((r) => r.decision_date === date)
  return { date, source: row?.source || '' }
})
// 迷你条默认停在哪个月（2026-10-15 第九十八轮改）：
//   ① **先认显式 ?month=**（从选股日历点票过来时带的就是用户正在看的那个月）；
//   ② 再认选股日历存的月（`selection_calendar_state_v1`，两页共用一份）；
//   ③ 都没有才退回"最后一根 K 线所在月"。
// 翻月后若落在没有 K 线的月份（换票/换区间时可能发生），就夹回最近的有数据的那一端。
// `monthSettled` 只负责"别把初始的 clamp 结果当成用户翻月写回 URL/localStorage"。
// ⚠️ 换票时**不能置空**再等 barDates 重算：中间有一个窗口 `miniMonth === ''`，此时点
//    「最新」会走 `days[last] || ''` 那条分支、什么都不做（实测点"最新"停在 2026-08）。
//    改成：换票时直接写入"要不要跟随 query 的月份"，其余情况保持当前月，等下方 clamp 兜底。
watch(selectedCode, (code, prev) => {
  if (!code) { miniMonth.value = ''; return }
  if (prev === undefined) return                                  // 首次：交给 query 那条 watch
  const shared = readSharedMonth(route.query as Record<string, unknown>)
  if (shared) miniMonth.value = shared
}, { immediate: true })
// 首次（或带 ?month= 进来）：用显式 query / 日历存的月开局
watch(() => route.query.month, (v) => {
  const m = String(v || '')
  if (m && m !== miniMonth.value) miniMonth.value = m
}, { immediate: true })
// 兜底 clamp：当前月在这只票的 K 线区间里没有数据时，夹到最近的有数据的那一端
// （换票/换区间都会走到；`miniMonth` 为空 ⇒ 落到最新月）
watch(barDates, (days) => {
  if (!days.length) return
  const months = [...new Set(days.map((d) => d.slice(0, 7)))]
  const last = months[months.length - 1]
  const cur = miniMonth.value
  if (!cur) { miniMonth.value = last; return }
  if (!months.includes(cur)) miniMonth.value = cur > last ? last : months[0]
}, { immediate: true })
// 翻月 → 写回 ?month= 与日历那份 localStorage（不回写的话，从 K 线切回日历又丢）。
// **空值绝不写**：换票瞬间的空月如果落进 localStorage，会把日历存的月份清掉。
watch(miniMonth, (m) => {
  if (!m || !/^\d{4}-\d{2}$/.test(m)) return
  writeSharedMonth(m)
  if (String(route.query.month || '') === m) return
  void router.replace({ query: { ...route.query, month: m } })
})
// 策略 id → 中文名（策略池一份；拿不到就退回 id，不阻断渲染）
onMounted(async () => {
  try {
    const pool = await (await fetch('/api/strategies?refresh=false')).json()
    const map: Record<string, string> = {}
    for (const s of pool?.strategies || []) if (s?.id) map[String(s.id)] = String(s.name || s.id)
    // 台账里还有 preselected / notify 这类**口径来源**（不是策略），策略池查不到 ⇒ 兜底表补上，
    // 否则 K 线浮层会显示 `preselected` 这种内部 id（实测踩到）。
    strategyNames.value = mergeStrategyNames(map)
  } catch { /* 忽略 */ }
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
/* 迷你选股日历条（2026-10-15：把选股日历搬进 K 线页的轻量形态） */
.mini-strip { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; padding: 6px 2px 2px; }
.ms-label { margin-right: 2px; }
.ms-cell {
  position: relative; display: inline-flex; flex-direction: column; align-items: center;
  min-width: 40px; padding: 2px 4px; border-radius: 4px; cursor: pointer;
  background: rgba(255, 255, 255, 0.04); border: 1px solid transparent;
}
.ms-cell:hover { border-color: rgba(255, 209, 102, 0.5); }
.ms-cell.hit { background: rgba(250, 204, 21, 0.12); }
.ms-cell.picked { border-color: #ffd166; }
.ms-dot { width: 5px; height: 5px; border-radius: 50%; background: #facc15; margin-top: 1px; }
.ms-nav, .ms-now { padding: 1px 5px; border-radius: 4px; cursor: pointer; background: rgba(255,255,255,0.06); }
.ms-nav:hover, .ms-now:hover { background: rgba(255,209,102,0.2); }
.ms-month { min-width: 5.2em; text-align: center; }
.mini-strip { position: relative; }
/* 明细卡本体在 SelectionDetailCard.vue（迷你条与 K 线图记号共用一份）：
   嵌在迷你条里时跟着日历条走，绝对定位。 */
.mini-strip > .ms-pop { position: absolute; z-index: 20; top: 100%; left: 2px; margin-top: 4px; }
/* 选中日的入选策略速读条（不用悬停就能看到"这天是谁选的"） */
.sel-bar { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; padding: 4px 2px 2px; }
.sel-chip { display: inline-flex; align-items: center; gap: 4px; padding: 1px 6px; border-radius: 4px;
  background: rgba(250, 204, 21, 0.10); border: 1px solid rgba(250, 204, 21, 0.28); }

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
