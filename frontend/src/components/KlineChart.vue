<template>
  <div class="chart-wrapper" @mouseleave="onChartLeave">
    <div ref="containerRef" class="chart-container" :style="{ height: height + 'px' }"></div>
    <div v-if="isLoading" class="chart-loading">加载中...</div>
    <div v-if="error" class="chart-error">{{ error }}</div>
    <!-- 左上角"身份条"：常驻代码 + 名称 + 最新价（2026-10-15 用户反馈"K 线图里没有直接看到股票代码/名称"）。
         只放"这是谁"，不放逐根 K 线的读数 —— 读数在下面那个悬停面板里。 -->
    <div class="chart-legend">
      <span class="lg-code mono">{{ data?.code || '—' }}</span>
      <span class="lg-name">{{ data?.name || name || '' }}</span>
      <span class="lg-sep" v-if="props.data?.latest">·</span>
      <span class="lg-v mono" v-if="props.data?.latest">{{ fmtNum(props.data.latest.close) }}</span>
      <span class="lg-v mono" v-if="props.data?.latest" :class="legend.chgUp ? 'tt-up' : 'tt-down'">
        {{ legend.changePct }}</span>
      <span class="lg-live tiny dim" title="这是常驻的标的身份条；逐根 K 线读数在左下角悬停面板里">最新</span>
    </div>
    <!-- 悬停读数面板（用户要的是"悬停才显示、但不压光标"）：**固定停在左下角**，
         位置与光标无关 ⇒ 指哪根 K 线都不会被挡；鼠标离开图区即消失（不留残影）。 -->
    <div v-if="hoverPanel.visible" class="hover-readout">
      <span class="hr-date mono">{{ hoverPanel.date }}</span>
      <span class="hr-group">
        <span class="hr-k">开</span><span class="hr-v mono">{{ hoverPanel.open }}</span>
        <span class="hr-k">高</span><span class="hr-v mono tt-high">{{ hoverPanel.high }}</span>
        <span class="hr-k">低</span><span class="hr-v mono tt-low">{{ hoverPanel.low }}</span>
        <span class="hr-k">收</span><span class="hr-v mono" :class="hoverPanel.chgUp ? 'tt-up' : 'tt-down'">{{ hoverPanel.close }}</span>
        <span class="hr-v mono" :class="hoverPanel.chgUp ? 'tt-up' : 'tt-down'">{{ hoverPanel.changePct }}</span>
        <span class="hr-k">振幅</span><span class="hr-v mono">{{ hoverPanel.amplitude }}</span>
        <span class="hr-k">量</span><span class="hr-v mono">{{ hoverPanel.volume }}</span>
      </span>
    </div>
    <!-- 入选记号的可交互命中区（2026-10-15）：lightweight-charts 的 marker 画在 canvas 上，
         没有 DOM 也没有 hover 事件 ⇒ 在图下方铺一层透明"热区"，悬停/聚焦就出明细卡。 -->
    <button v-for="hb in hitboxes" :key="hb.date" type="button" class="sel-hit"
            :style="{ left: hb.x + 'px' }" :title="`${hb.date}：入选 ${hb.hits} 条（悬停看明细）`"
            :aria-label="`${hb.date} 入选 ${hb.hits} 条策略，悬停或聚焦看明细`"
            @mouseenter="onHitEnter(hb.date)" @mouseleave="onHitLeave(hb.date)"
            @focus="onHitEnter(hb.date)" @blur="onHitLeave(hb.date)"
            @click="emit('pick', hb.date)"></button>
    <!-- 悬停在入选记号上 → 当天入选明细（策略中文名 / rank / 权重 / T+5）；与迷你日历条同一张卡 -->
    <SelectionDetailCard v-if="selDetail.date" ref="popRef" flat class="sel-pop"
                         :style="{ left: selDetail.x + 'px', top: selDetail.y + 'px' }"
                         :date="selDetail.date" :rows="selDetail.rows" :names="selectionNames"
                         :month="selectionMonth" />
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, watch, onMounted, onUnmounted, nextTick } from 'vue'
import {
  createChart, CrosshairMode, ColorType,
  type IChartApi, type ISeriesApi, type CandlestickData, type LineData, type HistogramData, type Time,
} from 'lightweight-charts'
import type { OhlcvResponse, OhlcvDataPoint, ChartMarker } from '@/types/api'
import SelectionDetailCard, { type SelectionRow } from '@/components/SelectionDetailCard.vue'

const props = defineProps<{
  data: OhlcvResponse | null
  isLoading: boolean
  error: string | null
  showSignals: boolean
  height: number
  /** 被选中的交易日（K 线页点开后在该日打点标记，用来对应下方的分时图） */
  selectedDate?: string | null
  /** 台账「选股记录」的决策日 → 在 K 线上打「入选」记号（2026-10-15：把选股日历搬进 K 线的第一件事） */
  selectionMarks?: { date: string; text?: string }[]
  /** 入选记号的明细数据（悬停浮层用；和迷你日历条同一份口径） */
  selectionRows?: SelectionRow[]
  /** 策略 id → 中文名（悬停浮层显示中文策略名，而不是 arm_concept 这种 id） */
  selectionNames?: Record<string, string>
  /** 当前看的月份（YYYY-MM）：只为把浮层里「当日全部 →」的落点对齐到同一月 */
  selectionMonth?: string
  /** 股票名称兜底（图例里显示"代码 + 名称"；`data.name` 缺的时候用） */
  name?: string
}>()

const emit = defineEmits<{
  priceRange: [range: [number, number]]
  pick: [date: string]
  /** 鼠标钻进/离开某个入选记号的命中区（父组件可据此收起别的浮层） */
  selectionHover: [date: string | null]
}>()

const containerRef = ref<HTMLElement | null>(null)
let chart: IChartApi | null = null
let candleSeries: ISeriesApi<'Candlestick'> | null = null
let volumeSeries: ISeriesApi<'Histogram'> | null = null
let ma5Series: ISeriesApi<'Line'> | null = null
let ma10Series: ISeriesApi<'Line'> | null = null
let ma20Series: ISeriesApi<'Line'> | null = null
let ma60Series: ISeriesApi<'Line'> | null = null
let volMa5Series: ISeriesApi<'Line'> | null = null
let volMa20Series: ISeriesApi<'Line'> | null = null
let selMarkSeries: ISeriesApi<'Line'> | null = null

let dataMap: Record<string, OhlcvDataPoint> = {}

/** 入选日 → 当天明细（悬停浮层的数据源；只在 selectionRows 变时重建） */
let selRowsByDate: Record<string, SelectionRow[]> = {}
/** 入选日期 → x 像素（命中区与浮层锚点共用；`undefined` = 不在当前可视区间内） */
let selXByDate: Record<string, number> = {}
/** 命中区左边界按格子宽度的一半算，窄屏翻页时自动变窄（下限 6px） */
let hitHalfWidth = 14

const hitboxes = ref<{ date: string; x: number; hits: number }[]>([])
const selDetail = reactive({ date: '' as string, x: 0, y: 0, rows: [] as SelectionRow[], cursorX: 0 })
/** 卡片根元素：宽度由**内容**决定（策略中文名长短不一），夹左右边界要用真实宽度 */
const popRef = ref<{ $el?: HTMLElement } | null>(null)

const COLORS = {
  bg: '#11161d', text: '#a3a8af', grid: 'rgba(42, 46, 57, 0.5)',
  border: 'rgba(197, 203, 206, 0.3)', up: '#ef476f', down: '#00d4aa',
  ma5: '#ffd166', ma10: '#ff9f1c', ma20: '#06d6a0', ma60: '#118ab2',
  crosshair: 'rgba(255, 209, 102, 0.85)', crosshairHz: 'rgba(255, 209, 102, 0.50)',
  sel: '#ffd166',
  gold: '#facc15',
}

/** 身份条上的涨跌（跟随光标那根 / 默认最新一根） */
const legend = reactive({ changePct: '', chgUp: true })

/** 悬停读数面板（固定停在左下角，位置与光标无关 ⇒ 不挡鼠标）。
 *  `visible` 只在光标真的落在一根 K 线上时为 true —— 这就是用户要的"悬停才显示"。 */
const hoverPanel = reactive({
  visible: false,
  date: '', open: '', high: '', low: '', close: '',
  changePct: '', amplitude: '', volume: '', chgUp: true,
})

function fmtNum(v: number | null | undefined): string {
  if (v == null) return '-'
  return v.toFixed(3)
}

function fmtVol(v: number): string {
  if (v >= 1e8) return (v / 1e8).toFixed(2) + '亿'
  if (v >= 1e4) return (v / 1e4).toFixed(0) + '万'
  return v.toFixed(0)
}

function showTooltip(param: any, container: HTMLElement) {
  const dp = param.point && param.time ? dataMap[param.time as string] : undefined
  // 图例是常驻的：光标不在图里（或落在没有 K 线的位置）就回到"最新一根"，不留旧读数
  if (!dp) { fillTooltipLatest(); return }
  fillTooltip(dp)
  // 明细卡按"重新避让"再摆一次（只在卡片可见时做，避免每次 crosshairMove 都重排）
  if (selDetail.date) void nextTick(placePop)
}

/** 把某一天的行情写进**悬停面板**（同时更新身份条上的涨跌）。
 *  两个入口共用：十字光标 / 入选记号命中区（命中区在画布外，crosshair 不会给读数）。 */
function fillTooltip(dp: OhlcvDataPoint) {
  hoverPanel.visible = true
  hoverPanel.date = dp.time
  hoverPanel.open = fmtNum(dp.open)
  hoverPanel.high = fmtNum(dp.high)
  hoverPanel.low = fmtNum(dp.low)
  hoverPanel.close = fmtNum(dp.close)
  hoverPanel.chgUp = (dp.change_pct ?? 0) >= 0
  hoverPanel.changePct = dp.change_pct != null ? (dp.change_pct >= 0 ? '+' : '') + dp.change_pct.toFixed(2) + '%' : '-'
  hoverPanel.amplitude = dp.amplitude != null ? dp.amplitude.toFixed(2) + '%' : '-'
  hoverPanel.volume = fmtVol(dp.volume)
  legend.chgUp = hoverPanel.chgUp
  legend.changePct = hoverPanel.changePct
}

/** 光标不在任何一根 K 线上（离开图区 / 落在空白处）⇒ 收起悬停面板，身份条回到"最新" */
function fillTooltipLatest() {
  hoverPanel.visible = false
  const bars = props.data?.data || []
  const last = bars[bars.length - 1] as OhlcvDataPoint | undefined
  const change = last?.change_pct
  legend.chgUp = (change ?? 0) >= 0
  legend.changePct = change != null ? (change >= 0 ? '+' : '') + change.toFixed(2) + '%' : '-'
}

function initChart() {
  if (!containerRef.value) return

  chart = createChart(containerRef.value, {
    layout: {
      background: { type: ColorType.Solid, color: COLORS.bg },
      textColor: COLORS.text,
    },
    grid: {
      vertLines: { color: COLORS.grid },
      horzLines: { color: COLORS.grid },
    },
    crosshair: {
      mode: CrosshairMode.Normal,
      vertLine: { color: COLORS.crosshair, width: 1, style: 2, labelVisible: true },
      horzLine: { color: COLORS.crosshairHz, width: 1, style: 2, labelVisible: true },
    },
    rightPriceScale: { borderColor: COLORS.border, entireTextOnly: true },
    timeScale: { borderColor: COLORS.border, timeVisible: true, secondsVisible: false },
    handleScroll: { vertTouchDrag: false },
  })

  candleSeries = chart.addCandlestickSeries({
    upColor: COLORS.up, downColor: COLORS.down,
    borderUpColor: COLORS.up, borderDownColor: COLORS.down,
    wickUpColor: COLORS.up, wickDownColor: COLORS.down,
  })

  ma5Series = chart.addLineSeries({ color: COLORS.ma5, lineWidth: 1, priceLineVisible: false, lastValueVisible: false })
  ma10Series = chart.addLineSeries({ color: COLORS.ma10, lineWidth: 1, priceLineVisible: false, lastValueVisible: false })
  ma20Series = chart.addLineSeries({ color: COLORS.ma20, lineWidth: 1, priceLineVisible: false, lastValueVisible: false })
  ma60Series = chart.addLineSeries({ color: COLORS.ma60, lineWidth: 2, priceLineVisible: false, lastValueVisible: false })

  volumeSeries = chart.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: 'volume' })
  chart.priceScale('volume').applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } })

  volMa5Series = chart.addLineSeries({
    color: COLORS.ma5, lineWidth: 1, lineStyle: 2,
    priceLineVisible: false, lastValueVisible: false, priceScaleId: 'volume',
  })
  volMa20Series = chart.addLineSeries({
    color: COLORS.ma20, lineWidth: 1, lineStyle: 2,
    priceLineVisible: false, lastValueVisible: false, priceScaleId: 'volume',
  })

  // 入选日期连线（只在入选日记点、落在该日收盘价上）：给"记号"一条连续的线，
  // 一眼看出这只票被哪些策略、隔多久反复选中（2026-10-15）。
  selMarkSeries = chart.addLineSeries({
    color: COLORS.gold, lineWidth: 2,
    priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
  })

  chart.timeScale().fitContent()

  chart.subscribeCrosshairMove((param) => {
    if (!containerRef.value) return
    showTooltip(param, containerRef.value)
    // 命中区（DOM）上的事件没有可靠坐标 ⇒ 不动
    if (param.point) refreshSelectionDetail(param.point)
  })

  // 点 K 线 → 把该交易日的"YYYY-MM-DD"抛给页面，页面据此拉当天分时
  chart.subscribeClick((param) => {
    if (param.time) emit('pick', String(param.time))
  })

  chart.timeScale().subscribeVisibleTimeRangeChange(() => { updatePriceRange(); updateHitboxes() })
}

function updatePriceRange() {
  if (!candleSeries) return
  const data = candleSeries.data() as CandlestickData[]
  if (!data || data.length === 0) return
  const visibleRange = chart?.timeScale().getVisibleRange()
  if (!visibleRange) return
  let lo = Infinity, hi = -Infinity
  for (const d of data) {
    const t = d.time as string
    if (t >= (visibleRange.from as string) && t <= (visibleRange.to as string)) {
      if (d.low < lo) lo = d.low
      if (d.high > hi) hi = d.high
    }
  }
  if (lo < Infinity && hi > -Infinity) emit('priceRange', [lo * 0.95, hi * 1.05])
}

function setData(resp: OhlcvResponse) {
  if (!chart || !candleSeries || !volumeSeries) return

  dataMap = {}
  for (const d of resp.data) { dataMap[d.time] = d }

  const marks = new Map((props.selectionMarks || []).map((m) => [m.date, m]))
  const candles: CandlestickData[] = []
  const volumes: HistogramData[] = []
  const ma5: LineData[] = []; const ma10: LineData[] = []
  const ma20: LineData[] = []; const ma60: LineData[] = []
  const volMa5: LineData[] = []; const volMa20: LineData[] = []
  // 只有入选日才有点 ⇒ 中间是断开的，不会在两根入选日之间连出一条假线
  const selLine: LineData[] = []
  const markers: ChartMarker[] = []

  for (const d of resp.data) {
    const time = d.time as Time
    candles.push({ time, open: d.open, high: d.high, low: d.low, close: d.close })
    volumes.push({ time, value: d.volume,
      color: d.close >= d.open ? 'rgba(239,71,111,0.5)' : 'rgba(0,212,170,0.5)' })
    if (d.ma5 != null) ma5.push({ time, value: d.ma5 })
    if (d.ma10 != null) ma10.push({ time, value: d.ma10 })
    if (d.ma20 != null) ma20.push({ time, value: d.ma20 })
    if (d.ma60 != null) ma60.push({ time, value: d.ma60 })
    if (d.vol_ma5 != null) volMa5.push({ time, value: d.vol_ma5 })
    if (d.vol_ma20 != null) volMa20.push({ time, value: d.vol_ma20 })
    if (props.showSignals) {
      if (d.buy_signal) markers.push({ time, position: 'belowBar', color: COLORS.up, shape: 'arrowUp', text: '买入', size: 2 })
      if (d.sell_signal) markers.push({ time, position: 'aboveBar', color: COLORS.down, shape: 'arrowDown', text: '卖出', size: 2 })
    }
    if (props.selectedDate && d.time === props.selectedDate) {
      markers.push({ time, position: 'aboveBar', color: COLORS.sel, shape: 'circle', text: '分时', size: 1 })
    }
    const picked = marks.get(d.time)
    if (picked) {
      // 入选：金色下箭头 + 该日收盘价上的一颗金点（悬停/聚焦出明细卡，见下方命中区）
      markers.push({ time, position: 'belowBar', color: COLORS.gold, shape: 'arrowUp',
                     text: picked.text || '入选', size: 1 })
      selLine.push({ time, value: d.close })
    }
  }

  candleSeries.setData(candles)
  volumeSeries.setData(volumes)
  ma5Series?.setData(ma5); ma10Series?.setData(ma10)
  ma20Series?.setData(ma20); ma60Series?.setData(ma60)
  volMa5Series?.setData(volMa5); volMa20Series?.setData(volMa20)
  selMarkSeries?.setData(selLine)
  candleSeries.setMarkers(markers)
  chart.timeScale().fitContent()
  nextTick(() => { updatePriceRange(); updateHitboxes() })
}

// ── 入选记号的可交互层（2026-10-15）─────────────────────────────────────────
/** 策略行按 decision_date 归档一次，悬停时不再扫全部行 */
function rebuildSelectionIndex() {
  const byDate: Record<string, SelectionRow[]> = {}
  for (const r of props.selectionRows || []) {
    (byDate[r.decision_date] ||= []).push(r)
  }
  selRowsByDate = byDate
}

/** marker 画在 canvas 上、没有 DOM ⇒ 在蜡烛窗格底部铺一层透明命中区：
 *  x 坐标随时间轴平移/缩放重算；宽度按格子宽度的一半（翻页变窄时不会互相盖住）。 */
function updateHitboxes() {
  if (!chart || !candleSeries || !props.selectionMarks?.length) { hitboxes.value = []; return }
  rebuildSelectionIndex()
  const xByDate = new Map<string, number>()
  for (const m of props.selectionMarks) {
    if (!dataMap[m.date]) continue                        // 这一天的 K 线不在当前区间里
    const x = chart.timeScale().timeToCoordinate(m.date as Time)
    if (x == null) continue
    xByDate.set(m.date, x)
  }
  selXByDate = Object.fromEntries(xByDate)
  // 相邻入选日的像素间距 → 命中区半宽（密度高时自动收窄，但保底 6px）
  const xs = [...xByDate.values()].sort((a, b) => a - b)
  let minGap = Infinity
  for (let i = 1; i < xs.length; i++) minGap = Math.min(minGap, xs[i] - xs[i - 1])
  hitHalfWidth = Math.max(6, Math.min(14, Number.isFinite(minGap) ? minGap / 2 : 14))
  hitboxes.value = [...xByDate.entries()].map(([date, x]) => ({
    date, x, hits: (selRowsByDate[date] || []).length || 1,
  }))
  // 时间轴动过 ⇒ 浮层锚点也过期了（否则会挂在旧位置上）
  if (selDetail.date) {
    const x = selXByDate[selDetail.date]
    if (x == null) { hideSelectionDetail(); return }
    const { y } = selDetail
    refreshSelectionDetail({ x, y })
  }
}

/** 从光标位置（相对容器）算出最近的入选日；不在任何入选日记号附近就清掉浮层。
 *  `x`/`y` 都传 null 表示"关掉"（鼠标离开图区时用）。 */
function refreshSelectionDetail(point: { x: number | null; y: number | null }) {
  if (point.x == null || point.y == null) { hideSelectionDetail(); return }
  if (!props.selectionMarks?.length) { hideSelectionDetail(); return }
  const date = nearestSelectionDate(point.x, hitHalfWidth + 4)
  if (!date) { hideSelectionDetail(); return }
  const rows = selRowsByDate[date] || []
  if (!rows.length) { hideSelectionDetail(); return }
  selDetail.cursorX = point.x                                // 光标的 x：判断"卡片该去左边还是右边"
  selDetail.x = Math.max(8, Math.min(point.x - 60, 800))     // 粗放定位，渲染后由 placePop() 精修
  selDetail.y = 8
  selDetail.date = date
  selDetail.rows = rows
  // 卡片尺寸（宽高由内容决定：策略中文名长短差很多）与行情 tooltip 的位置都要**渲染后**才知道，
  // 所以每次显示/换日之后重排一次：保证"明细卡 + 行情 tooltip 同时可见且不重叠"（用户要求两个都显示）。
  void nextTick(placePop)
}

/** 摆明细卡：不出图区（含底部那条 28px 命中区），也**不压住左上角的图例**。
 *
 *  为什么需要它：卡片宽高由内容决定（策略中文名长短差很多：`arm_select` vs
 *  「多因子策略（低市值+高ROE）」）⇒ 必须**渲染后**拿真实矩形来摆。
 *  取位顺序：① 贴命中区（时间轴那条）正上方、左边界跟随记号；
 *  ② 和图例撞了就挪到**光标的对角**（光标在右半图 ⇒ 卡片去左上；在左半图 ⇒ 去右上）；
 *  ③ 还撞就换另一侧的下方。
 *  2026-10-15 改：行情读数已从"跟随鼠标的浮层"改成**固定的左上角图例**，所以这里只需避让一个
 *  已知位置的矩形，不再依赖 `.kline-tooltip` 的实际位置（那个类已经删了）。
 */
function placePop() {
  const wrap = containerRef.value?.parentElement as HTMLElement | null
  const el = popRef.value?.$el as HTMLElement | undefined
  if (!wrap || !el) return
  const w = el.offsetWidth || 320
  const h = el.offsetHeight || 90
  const gap = 8
  const strip = 28                                   // 命中区（时间轴那一条）高度
  const width = wrap.clientWidth
  const height = wrap.clientHeight
  const belowY = Math.max(gap, height - strip - h - 6)   // 命中区正上方
  const clampX = (x: number) => Math.max(gap, Math.min(x, width - w - gap))
  const wr0 = wrap.getBoundingClientRect()
  /** 要避开的固定 UI：左上角身份条 + 左下角悬停读数面板（两个都不跟鼠标走，位置可预算） */
  const blockers = ['.chart-legend', '.hover-readout'].map((sel) => {
    const el = wrap.querySelector(sel) as HTMLElement | null
    if (!el) return null
    const r = el.getBoundingClientRect()
    return { x: r.x - wr0.x, y: r.y - wr0.y, w: r.width, h: r.height }
  }).filter(Boolean) as { x: number; y: number; w: number; h: number }[]
  const lg = blockers[0] || null
  const collides = (x: number, y: number) => blockers.some(
    (b) => !(x + w <= b.x || b.x + b.w <= x || y + h <= b.y || b.y + b.h <= y))
  const mx = selXByDate[selDetail.date] ?? selDetail.cursorX
  let x = clampX(mx - 60)
  let y = belowY
  if (collides(x, y)) {
    const left = selDetail.cursorX < width / 2
    x = left ? clampX(width - w - gap) : gap          // ② 对角
    y = lg ? lg.y + lg.h + gap : gap
    if (collides(x, y)) {                             // ③ 再撞就换另一侧的下方
      y = belowY
      x = clampX(left ? gap : width - w - gap)
    }
  }
  selDetail.x = x
  selDetail.y = y
}

function hideSelectionDetail() {
  if (selDetail.date) { selDetail.date = ''; selDetail.rows = [] }
}

/** 离光标水平距离 ≤ tol 的最近入选日（只在当前可视的入选日里找） */
function nearestSelectionDate(x: number, tol: number): string | null {
  let best: string | null = null
  let bestDist = tol
  for (const [date, mx] of Object.entries(selXByDate)) {
    const dist = Math.abs(mx - x)
    if (dist <= bestDist) { bestDist = dist; best = date }
  }
  return best
}

/** 命中区（DOM）的进入/离开：进 → 出明细卡，**图例同时切到那一天**（用户要求行情与明细都在）；
 *  出 → 交给 crosshair 重新判断（图例保持在那一天，直到光标移到别处）。 */
function onHitEnter(date: string) {
  const x = selXByDate[date]
  const dp = dataMap[date]
  // 命中区在画布外 ⇒ crosshair 不会更新图例，这里手动把图例切到这一天
  if (dp) fillTooltip(dp)
  if (x != null) refreshSelectionDetail({ x, y: 0 })
  emit('selectionHover', date)
}
function onHitLeave(_date: string) {
  emit('selectionHover', null)
}

/** 鼠标离开整张图（含命中区）⇒ 明细卡收起，图例回到"最新一根"（不留残影/旧读数） */
function onChartLeave() {
  refreshSelectionDetail({ x: null, y: null })
  fillTooltipLatest()
}

function destroyChart() {
  if (chart) { chart.remove(); chart = null }
  candleSeries = null; volumeSeries = null
  ma5Series = ma10Series = ma20Series = ma60Series = null
  volMa5Series = volMa20Series = null; selMarkSeries = null
  hitboxes.value = []
  selXByDate = {}
  hideSelectionDetail()
}

onMounted(() => {
  nextTick(() => {
    initChart()
    if (props.data) setData(props.data)
    fillTooltipLatest()
  })
})
onUnmounted(() => { destroyChart() })

watch(() => props.data, (newData) => {
  if (!chart) { nextTick(() => { initChart(); if (props.data) setData(props.data) }); return }
  if (newData) setData(newData)
  // 换票/换区间 ⇒ 身份条立刻显示新标的的涨跌（光标正悬停时不动，免得读数和光标打架）
  if (!hoverPanel.visible) nextTick(fillTooltipLatest)
})
watch(() => props.height, (h) => { chart?.applyOptions({ height: h }) })
watch(() => props.showSignals, () => { if (props.data) setData(props.data) })
// 选中日变化 → 重画一次标记（数据量在几千根级别，重建成本可忽略）
watch(() => props.selectedDate, () => { if (props.data) setData(props.data) })
// 入选记号是异步到的（台账接口晚于日线）⇒ 到了就重铺命中区
watch(() => props.selectionMarks, () => { nextTick(updateHitboxes) }, { deep: true })
watch(() => props.selectionRows, () => { hideSelectionDetail(); nextTick(updateHitboxes) }, { deep: true })
</script>

<style scoped>
.chart-wrapper { position: relative; flex: 1; min-width: 0; }
.chart-container { width: 100%; }
.chart-loading, .chart-error {
  position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
  font-size: 14px;
}
.chart-loading { color: var(--text-muted); }
.chart-error { color: #ef476f; }

/* 身份条：固定在左上角，**不跟鼠标**（用户反馈"鼠标把日期挡住了"）。只放"这是谁 + 最新价"。
   指针事件关掉，免得挡住图表的拖拽/缩放。 */
.chart-legend {
  position: absolute; z-index: 12; top: 6px; left: 8px;
  display: flex; align-items: center; gap: 6px;
  padding: 3px 8px; border-radius: 6px;
  background: rgba(17, 22, 29, 0.72);
  border: 1px solid rgba(255, 255, 255, 0.06);
  backdrop-filter: blur(2px);
  font-size: 12px; line-height: 1.6;
  pointer-events: none;
}
.chart-legend .lg-code { font-weight: 700; color: var(--text-primary); font-size: 13px; }
.chart-legend .lg-name { color: var(--accent-gold); font-weight: 600; }
.chart-legend .lg-sep { color: var(--text-dim); }
.chart-legend .lg-v { color: var(--text-primary); }
.chart-legend .lg-live { padding: 0 4px; border-radius: 3px; border: 1px solid var(--border-color); }

/* 悬停读数面板：**停在左下角**，位置与光标无关 ⇒ 指哪根 K 线都不会被光标压住。
   只在光标落在某根 K 线上时出现（悬停才显示）。 */
.hover-readout {
  position: absolute; z-index: 12; left: 8px; bottom: 96px;   /* 避开底部成交量一根 */
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  padding: 3px 8px; border-radius: 6px;
  background: rgba(20, 24, 32, 0.92);
  border: 1px solid rgba(255, 209, 102, 0.28);
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.45);
  font-size: 12px; line-height: 1.6;
  pointer-events: none;
}
.hover-readout .hr-date { color: var(--accent-gold); font-weight: 600; }
.hover-readout .hr-group { display: inline-flex; align-items: center; gap: 4px; margin-left: 2px; }
.hover-readout .hr-k { color: var(--text-muted); }
.hover-readout .hr-v { color: var(--text-primary); }
.tt-high { color: var(--accent-up) !important; }
.tt-low { color: var(--accent-down) !important; }
.tt-up { color: var(--accent-up) !important; }
.tt-down { color: var(--accent-down) !important; }

/* ── 入选记号的可交互层 ─────────────────────────────────────────────
   透明热区压在时间轴上方（0.85×高度以下不动，避免挡住点的 K 线/成交量），
   悬停或 Tab 聚焦时点亮 + 出明细卡。 */
.sel-hit {
  position: absolute; left: 0; width: 28px; height: 28px; z-index: 6;
  transform: translateX(-50%); margin: 0; padding: 0;
  background: transparent; border: 0; border-radius: 4px; cursor: pointer;
  top: calc(100% - 28px);          /* 压在时间轴那一条上，不挡蜡烛/成交量 */
}
.sel-hit:hover, .sel-hit:focus-visible {
  background: rgba(250, 204, 21, 0.16); outline: 1px solid rgba(250, 204, 21, 0.5);
}
/* 明细卡位置由 placePop() 每次渲染后算（left + top）：要和行情 tooltip **同时可见且不重叠**。
   z-index 30 > tooltip 的 20 ⇒ 万一真挤到一起，卡片在上、且卡片里的链接仍然可点。 */
.sel-pop { position: absolute; z-index: 30; }
@media (max-width: 900px) { .sel-hit { height: 20px; } }
</style>
