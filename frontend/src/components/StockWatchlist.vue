<template>
  <div class="panel watchlist">
    <h6>
      自选股
      <span class="dim tiny">{{ local.length }} 自选 / {{ visibleRows.length }} 显示</span>
      <button class="btn-ghost btn-sm" :disabled="refreshing" @click="reloadGroups">刷新</button>
      <button class="btn-ghost btn-sm add-btn" @click="clearAll" v-if="local.length" title="清空本地自选">清空</button>
    </h6>

    <section class="stock-discovery" aria-label="搜索与自选">
      <label class="search-label">全市场搜股</label>
      <StockSelector :model-value="modelValue" @update:model-value="viewStock" @selected="rememberSearch" />
      <p class="hint tiny">代码、名称或拼音搜索；选中先看图，不会自动加入自选。</p>
      <div class="search-actions">
        <button class="btn-ghost btn-sm search-add" :disabled="!modelValue" @click="addCurrent">
          {{ currentIsLocal ? '★ 已在自选 · 查看' : '☆ 加入自选' }}
        </button>
        <span class="tiny dim current-stock">{{ modelValue || '请先选择股票' }} {{ currentName }}</span>
      </div>
      <p v-if="searchNotice" class="hint tiny" role="status">{{ searchNotice }}</p>
    </section>

    <!-- 快速分组：直接取各策略的最新名单，省得一只一只搜 -->
    <div class="groups">
      <button class="chip" :class="{ active: group === 'local' }" @click="group = 'local'">我的自选 {{ local.length }}</button>
      <button class="chip" :class="{ active: group === 'all' }" @click="group = 'all'">全部策略 {{ allRows.length }}</button>
      <button v-for="g in sortedGroups" :key="g.key" v-show="g.key === 'live_holdings' || groupsExpanded" class="chip" :class="{ active: group === g.key }"
              :title="g.note + (g.date ? ' · ' + g.date : '') + (g.verdict ? '｜' + g.verdict.detail : '')
                      + (g.annual_rank ? `｜年化 ${(g.annual_rank.value * 100).toFixed(2)}% · ${g.annual_rank.observations} 观测 · ${g.annual_rank.window}` : '｜' + (g.annual_rank_reason || ''))
                      + (markOf(g) ? '｜' + markOf(g)!.detail : '')"
              @click="toggleGroup(g.key)">
        {{ g.label
        }}<span v-if="g.key === 'live_holdings'" class="tiny"> · 置顶</span
        ><span v-if="g.key !== 'paper'" class="g-annual tiny">年化 {{ g.annual_rank ? (g.annual_rank.value * 100).toFixed(2) + '%' : '—' }}</span
        ><span v-if="g.verdict" class="g-verdict" :class="g.verdict.tone">{{ g.verdict.label }}</span
        ><span v-if="markOf(g)" class="g-refresh" :class="markOf(g)!.tone">{{ markOf(g)!.icon }}</span
        ><span class="dim tiny"> {{ g.loading ? '…' : g.rows.length }}</span>
      </button>
    </div>
    <button class="btn-ghost btn-sm groups-toggle" :aria-expanded="groupsExpanded" @click="groupsExpanded = !groupsExpanded">
      {{ groupsExpanded ? '收起策略分组' : `展开其余 ${sortedGroups.filter(g => g.key !== 'live_holdings').length} 个分组` }}
    </button>
    <div class="row-sort" v-if="group !== 'local'">
      <span class="dim tiny">个股排序</span>
      <select v-model="rowSort">
        <option value="composite">综合评分（默认）</option>
        <option value="combo">资金流 × 个股多空（50/50）</option>
        <option value="default">策略原始顺序</option>
      </select>
      <span class="tiny dim" v-if="rank?.date">评分日 {{ rank.date }}</span>
      <span class="tiny dim" v-else-if="rank && !rank.available">暂无综合评分</span>
      <span class="tag bad tiny" v-if="rank?.validation?.status === 'failed'" title="核心层首次历史评估未通过；综合分只作实验排序">未通过验证</span>
      <span class="tag dim tiny" v-else-if="rank?.validation?.status === 'missing'">未验证</span>
    </div>
    <div class="group-sort" v-show="groupsExpanded">
      <span class="dim tiny">分组排序</span>
      <select v-model="sortKey">
        <option value="annual">按年化收益（默认）</option>
        <option value="default">策略池顺序</option>
        <option value="total">按累计收益</option>
        <option value="excess">按相对中证500</option>
        <option value="t">按日度 t</option>
        <option value="name">按名称</option>
      </select>
      <span class="dim tiny" v-if="verdictNote">{{ verdictNote }}</span>
    </div>
    <p v-show="groupsExpanded" class="hint tiny">实盘固定置顶；其余按所选规则排序。年化按252个日收益折算，不足一年/缺数置后，各策略样本窗口仍不同。</p>
    <p v-if="poolError" class="hint tiny">{{ poolError }}</p>
    <p v-if="activeGroup" class="hint tiny">{{ activeGroup.label }} · {{ activeGroup.stage }} · 名单交易日 {{ activeGroup.date || '—' }}
      <span v-if="markOf(activeGroup)" :class="markOf(activeGroup)!.tone">
        · 刷新 {{ activeGroup.refresh?.status }}（{{ activeGroup.refresh?.date_after || '—' }}）</span>
      <span v-if="activeGroup.loading"> · 加载中…</span>
      <span v-if="activeGroup.error"> · {{ activeGroup.error }}</span>
    </p>
    <p v-if="group === 'all' && failedGroups.length" class="hint tiny">部分名单待加载：{{ failedGroups.join('、') }}</p>
    <button v-if="group !== 'local' && visibleRows.length" class="btn-ghost btn-sm batch-add" @click="addGroupToLocal">★ 当前分组全部加入自选</button>

    <ul class="rows">
      <li v-for="row in visibleRows" :key="row.code" :class="{ active: row.code === modelValue }"
          v-memo="[row.code, row.name, row.tag, row.local, row.rank, row.score, row.displayRank, row.displayScore, row.flowScore, row.longshort, row.isLive, row.code === modelValue, liveKey(row.code), hoverOn(row.code)]"
          :title="`${row.code} ${row.name || '（缺名称）'}${row.tag ? ' · ' + row.tag : ''}`"
          @mouseenter="hoverCode = row.code" @mouseleave="hoverCode = null"
          @focusin="hoverCode = row.code" @focusout="hoverCode = null"
          @click="$emit('update:modelValue', row.code)">
        <span class="rk mono tiny" v-if="(row.displayRank ?? row.rank) != null"
              :title="rowSort === 'combo' ? '资金流 × 个股多空排名' : '候选池综合评分排名'">
          #{{ row.displayRank ?? row.rank }}</span>
        <span class="mono code">{{ row.code }}</span>
        <span class="name">{{ row.name || '—' }}</span>
        <span class="tag gold tiny live-tag" v-if="row.isLive" title="实盘持仓（指定置顶，仍显示候选池排名）">实盘</span>
        <span class="score mono tiny" v-if="(row.displayScore ?? row.score) != null"
              :title="rowSort === 'combo' ? '资金流 × 个股多空合成分（50/50）' : '综合评分（标准分）'">
          {{ Number(row.displayScore ?? row.score).toFixed(2) }}</span>
        <span class="score dim tiny" v-else-if="row.isLive" title="实盘持仓不在当日策略候选池里">未入候选</span>
        <span class="flow tiny" v-if="rowSort === 'combo' && (row.flowScore != null || row.longshort != null)"
              :title="`资金流 5d ${((row.flow5 ?? 0) * 100).toFixed(2)}% / 20d ${((row.flow20 ?? 0) * 100).toFixed(2)}%（分 ${row.flowScore?.toFixed(2) ?? '—'}）· 个股多空 ${row.longshort?.toFixed(2) ?? '—'}`">
          资金 {{ row.flowScore?.toFixed(2) ?? '—' }} · 多空 {{ row.longshort?.toFixed(2) ?? '—' }}
        </span>
        <span class="tag dim tiny" v-if="row.tag">{{ row.tag }}</span>
        <span class="live" v-if="livePrice(row.code) !== null"
              :title="`${liveLabel(row.code)} · ${liveQuoteTime(row.code)}`"
              :class="liveTone(row.code)">
          {{ livePrice(row.code)!.toFixed(2) }}
          <span class="tiny">{{ livePct(row.code) }}</span>
          <span class="src tiny">{{ liveLabel(row.code) }}</span>
        </span>
        <button v-if="row.source" class="goto" title="在「选股结果」里看它的分数 / 理由 / SHAP"
                @click.stop="openSelection(row)">↗</button>
        <button class="star" :title="row.local ? '从自选移除' : '加入自选'"
                @click.stop="toggleLocal(row)">{{ row.local ? '★' : '☆' }}</button>
      </li>
      <li v-if="!visibleRows.length" class="empty tiny">{{ emptyText }}</li>
    </ul>
    <!-- 悬停一行 → 入选摘要（哪条策略 / rank / 权重 / T+5，走共用卡片，与 K 线浮层同一套文案。
         数据来自 `/api/calendar/by-code` 批量接口；这一段刻意放在 <ul> 外面：
         列表自己有 overflow:auto，放里面会被裁掉。 -->
    <SelectionDetailCard v-if="hoverSummary" class="row-pop" show-dates
                         :date="hoverSummary.last_date" :rows="hoverSummary.rows"
                         :names="poolNames" :code="hoverCode" :month="month || undefined"
                         :title="`台账共 ${hoverSummary.n} 条入选（${hoverSummary.first_date} ~ ${hoverSummary.last_date}，${hoverSummary.n_sources} 条策略）`" />
    <p v-else-if="hoverCode && hoverProbed && !hoverSummaryLoading" class="hint tiny row-pop-note">
      台账里没有 {{ hoverCode }} 的入选记录（台账截至 {{ asOf || '—' }}）。
    </p>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { getCandidateRank, getPaperStatus, getSelection, getStrategyPool } from '@/api/client'
import type { StockSearchHit } from '@/api/client'
import { useWatchlist } from '@/composables/useWatchlist'
import { useLiveQuotes, quoteLabel } from '@/composables/useLiveQuotes'
import { useSelectionSummaries } from '@/composables/useSelectionSummaries'
import { LEDGER_SOURCE_LABEL } from '@/composables/selectionLabels'
import StockSelector from './StockSelector.vue'
import SelectionDetailCard from './SelectionDetailCard.vue'
import { groupRows, refreshMark, sortGroups, strategyGroups } from './watchlistStrategies'
import type { GroupSortKey, WatchlistGroup, WatchlistRow } from './watchlistStrategies'
import type { CandidateRankResponse } from '@/types/api'
import { strategyDetail } from './strategyDetail'

const props = defineProps<{
  modelValue: string | null
  /** 当前看的月份（YYYY-MM）：带上它，从浮层里点决策日进 K 线时日历条停在同一月 */
  month?: string | null
}>()
const router = useRouter()
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()

// 自选状态来自全局 composable（`composables/useWatchlist.ts`）：
// 选股日历 / 行业热度页勾的 ★ 会实时出现在这里，反之亦然。
const watchlist = useWatchlist()

const local = watchlist.codes
const groups = ref<WatchlistGroup[]>([])
const group = ref('all')
const refreshing = ref(false)
const poolError = ref('')
const activeGroup = computed(() => groups.value.find((g) => g.key === group.value))
/** 分组上的名单刷新标记（数据来自每日名单刷新契约，与策略池同一份产物） */
const markOf = (g: WatchlistGroup) => refreshMark(g.refresh)
const allRows = computed(() => groupRows(sortedGroups.value, 'all'))
const failedGroups = computed(() => groups.value.filter((g) => g.key !== 'paper' && g.error).map((g) => g.label))
const groupsExpanded = ref(false)
const searchNotice = ref('')
const currentName = computed(() => props.modelValue ? watchlist.names.value[props.modelValue] || '' : '')
const currentIsLocal = computed(() => !!props.modelValue && watchlist.has(props.modelValue))
watch(() => props.modelValue, () => { searchNotice.value = '' })
const sortKey = ref<GroupSortKey>('annual')
/** 个股排序：综合评分（默认）/ 资金流 / 个股多空 / 策略原始顺序 */
const ROW_SORT_KEY = 'kline.watchlist.rowSort'
function initialRowSort(): 'composite' | 'combo' | 'default' {
  try {
    const saved = localStorage.getItem(ROW_SORT_KEY)
    if (saved === 'composite' || saved === 'combo' || saved === 'default') return saved
  } catch { /* 隐私模式 */ }
  return 'combo'
}
const rowSort = ref<'composite' | 'combo' | 'default'>(initialRowSort())
watch(rowSort, (v) => { try { localStorage.setItem(ROW_SORT_KEY, v) } catch { /* ignore */ } })
const rank = ref<CandidateRankResponse | null>(null)
const rankMap = computed(() => new Map((rank.value?.rows || []).map((r) => [r.code, r])))
const liveHoldingCodes = computed(() => new Set(
  (groups.value.find((g) => g.key === 'live_holdings')?.rows || []).map((r) => r.code)))
/** 实盘固定置顶，其余与策略池共用后端年化名次；组内个股仍保持选股顺序。 */
const sortedGroups = computed(() => sortGroups(groups.value, sortKey.value))
const verdictNote = computed(() => {
  const positive = groups.value.filter((g) => g.verdict?.tone === 'ok').length
  const negative = groups.value.filter((g) => g.verdict?.tone === 'bad').length
  return positive || negative ? `正期望 ${positive} · 负期望 ${negative}` : ''
})
const visibleRows = computed<WatchlistRow[]>(() => {
  if (group.value === 'local') return local.value.map((code) => ({
    code, name: watchlist.names.value[code] || '', local: true, tag: '自选',
  }))
  const rows: WatchlistRow[] = groupRows(sortedGroups.value, group.value).map((row): WatchlistRow => {
    const r = rankMap.value.get(row.code)
    return {
      ...row,
      name: row.name || watchlist.names.value[row.code] || '',
      local: watchlist.has(row.code),
      rank: r?.rank ?? null,
      score: r?.composite ?? null,
      flowScore: r?.flow_score ?? null,
      flow5: r?.flow_5d_ratio ?? null,
      flow20: r?.flow_20d_ratio ?? null,
      longshort: r?.longshort_score ?? null,
      comboScore: r?.flow_longshort_score ?? null,
      isLive: liveHoldingCodes.value.has(row.code),
      displayRank: null,
      displayScore: null,
    }
  })
  // 每种模式在**完整候选池**内排名（实盘也参与排名，只是显示时置顶）
  const metric = rowSort.value === 'composite' ? 'rank'
    : rowSort.value === 'combo' ? 'comboScore' : null
  if (metric) {
    const valid = rows.filter((r) => (r as any)[metric] != null)
    valid.sort((a, b) => metric === 'rank'
      ? Number(a.rank) - Number(b.rank)
      : Number((b as any)[metric]) - Number((a as any)[metric]))
    valid.forEach((r, i) => {
      r.displayRank = i + 1
      r.displayScore = metric === 'rank' ? r.score : Number((r as any)[metric])
    })
  }
  rows.sort((a, b) => {
    // 实盘持仓**始终置顶**（所有排序模式都一样），但仍显示它在候选池里的排名/分数
    if (a.isLive !== b.isLive) return a.isLive ? -1 : 1
    if (rowSort.value === 'default') return 0
    return (a.displayRank ?? 1e9) - (b.displayRank ?? 1e9)
  })
  return rows
})
const emptyText = computed(() => refreshing.value || activeGroup.value?.loading ? '正在加载策略名单…'
  : activeGroup.value?.error ? '名单加载失败，点击刷新重试'
  : group.value === 'local' ? '还没有自选股：用搜索或策略分组添加'
  : '当前分组暂无每日名单')

// ── 实时快照（数据驱动 UI）：只轮询**当前可见**的自选（交易时段 + 页面可见才轮询） ──
const liveCodes = computed(() => visibleRows.value.map((row) => row.code))

/** 悬停那一行（鼠标或键盘聚焦都算）：用来出"入选摘要"浮层（2026-10-15 第九十八轮） */
const hoverCode = ref<string | null>(null)
const hoverOn = (code: string) => hoverCode.value === code
/** 批量取入选摘要（100+ 行只发 1~2 个请求；查过就缓存，见 composable 注释） */
const { hoverSummary, hoverProbed, asOf, loading: hoverSummaryLoading } = useSelectionSummaries(liveCodes, hoverCode)
/** 浮层里策略 id → 中文名：台账来源兜底表 + 该分组自己的中文名 */
const poolNames = computed<Record<string, string>>(() => {
  const map: Record<string, string> = { ...LEDGER_SOURCE_LABEL }
  for (const g of groups.value) if (g.label) map[g.key] = g.label
  return map
})
const live = useLiveQuotes(liveCodes, { intervalMs: 4000 })
const liveUpdatedText = computed(() => (live.updatedAt.value
  ? `更新于 ${live.updatedAt.value.toLocaleTimeString('zh-CN')}${live.polling.value ? '（轮询中）' : ''}`
  : '尚未取到'))

/**
 * v-memo 的比较键：价格/涨跌/来源/行情时间**任一变化**才重渲染这一行（否则整行跳过）。
 *
 * 为什么要把 `quote.time` 也放进键里：行内 title 显示的是**行情时间**，
 * 只比价格的话"价格没动但时间在走"的行会一直不重渲染 ⇒ 时间戳冻住在几分钟前
 * （实测：13:43 抓到的 title 到 13:43:46 之后一直是 13:43:46）。
 * 行情时间每 4 秒轮询都会变，所以这一项让 memo 的"值不变不重渲染"依然成立。
 */
function liveKey(code: string): string {
  const quote = live.quotes.value[code]
  if (!quote) return '-'
  return `${quote.price ?? ''}|${quote.change_pct ?? ''}|${quote.source ?? ''}|${quote.time ?? ''}`
}

function livePrice(code: string): number | null {
  const quote = live.quotes.value[code]
  return quote && quote.price != null ? Number(quote.price) : null
}

function livePct(code: string): string {
  const quote = live.quotes.value[code]
  const pct = quote?.change_pct
  if (pct == null) return ''
  // 事故（2026-10-13）：`/api/intraday/quotes` 的 `change_pct` 是**小数**（0.0076 = 0.76%），
  // 这里原来直接 `toFixed(2)` ⇒ 侧栏显示 "+0.01%"，而同页分时面板显示 "+0.76%"（那里有 ×100）。
  // 与 `Intraday.vue` / `KlinePage.vue` 的 `pct()` 口径统一：小数 × 100。
  const value = Number(pct) * 100
  return `${value > 0 ? '+' : ''}${value.toFixed(2)}%`
}

function liveLabel(code: string): string {
  return quoteLabel(live.quotes.value[code])
}

/** 每行的行情时间（交易所/源给出的时刻，不是页面抓取时刻）。 */
function liveQuoteTime(code: string): string {
  const raw = String(live.quotes.value[code]?.time || '')
  // 源给的是 `20261012134333` 这种紧凑格式
  const m = raw.match(/^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})$/)
  return m ? `${m[4]}:${m[5]}:${m[6]}` : (raw || '时间未知')
}

function liveTone(code: string): string {
  const pct = Number(live.quotes.value[code]?.change_pct ?? 0)
  return pct > 0 ? 'up' : (pct < 0 ? 'down' : '')
}

function openSelection(row: WatchlistRow) {
  // 跳到「选股结果」对应来源 + 定位该票（2026-10-15：把"自选 → 审查"这条单行道补上）
  // ⚠️ 选股结果页的路由是 `path: '/'`（name='selection'），不是 '/selection'
  // （实测：跳到 #/selection 匹配不到路由 ⇒ main 里什么都没有）
  void router.push({ path: '/', query: { source: row.source || '', code: row.code } })
}

function toggleLocal(row: WatchlistRow) {
  watchlist.toggle(row.code, row.name)
}

function addGroupToLocal() {
  for (const row of visibleRows.value) watchlist.add(row.code, row.name)
}

function rememberSearch(hit: StockSearchHit) {
  watchlist.rememberName(hit.code, hit.name)
}
function viewStock(code: string) {
  searchNotice.value = ''
  emit('update:modelValue', code)
}
function addCurrent() {
  if (!props.modelValue) return
  const existed = watchlist.has(props.modelValue)
  watchlist.add(props.modelValue, currentName.value)
  group.value = 'local'
  searchNotice.value = `${props.modelValue} ${currentName.value}：${existed ? '已在自选，已切换到我的自选' : '已加入我的自选'}`
}

async function toggleGroup(key: string) {
  group.value = key
  const target = groups.value.find((g) => g.key === key)
  if (target && !target.loading && (!target.rows.length || target.error)) await loadGroup(key)
}

async function loadGroup(key: string) {
  const target = groups.value.find((g) => g.key === key)
  if (!target) return
  target.loading = true
  target.error = ''
  try {
    if (key === 'paper') {
      const status = await getPaperStatus()
      const positions = status.positions || []
      target.rows = positions.map((p) => ({ code: String(p.stock_code), name: String(p.name || '') }))
    } else {
      const data = await getSelection(target.source)
      if (data.source?.key !== target.source) throw new Error('名单来源不匹配')
      target.date = data.source.trade_date
      target.rows = [...new Map((data.rows || []).map((r) => [String(r.stock_code), {
        code: String(r.stock_code), name: String(r.stock_name || r.name || ''),
      }])).values()]
    }
    watchlist.rememberMany(target.rows)
  } catch (error) {
    target.rows = []
    target.error = error instanceof Error ? error.message : '名单加载失败'
  } finally { target.loading = false }
}

async function reloadGroups() {
  if (refreshing.value) return
  refreshing.value = true
  poolError.value = ''
  try {
    const [pool, rankPayload] = await Promise.all([
      getStrategyPool(),
      getCandidateRank().catch(() => null),
    ])
    rank.value = rankPayload
    groups.value = [...strategyGroups(pool.strategies, (entry) => {
      const item = pool.strategies.find((x) => x.id === entry.id)
      if (!item) return undefined
      const detail = strategyDetail(item)
      return { label: detail.verdict.headline.label, tone: detail.verdict.headline.tone,
               detail: detail.verdict.headline.detail, total: detail.curve.total,
               excess: detail.curve.excess, t: detail.curve.t }
    }), {
      key: 'paper', label: '模拟盘持仓', note: '模拟盘当前持仓', source: '', stage: '模拟盘',
      date: null, rows: [], loading: false, error: '',
    }]
    if (!['all', 'local'].includes(group.value) && !groups.value.some((g) => g.key === group.value)) group.value = 'all'
    await Promise.all(groups.value.map((g) => loadGroup(g.key)))
  } catch (error) {
    poolError.value = '策略分组加载失败：' + (error instanceof Error ? error.message : '请点击刷新重试')
  } finally { refreshing.value = false }
}

function clearAll() {
  watchlist.clear()
}

onMounted(async () => {
  await reloadGroups()
  if (!props.modelValue && visibleRows.value.length) emit('update:modelValue', visibleRows.value[0].code)
})
</script>

<style scoped>
.stock-discovery { margin-bottom: var(--sp-3); padding-bottom: var(--sp-3); border-bottom: 1px solid var(--border-color); }
.search-label { display: block; font-weight: 600; margin-bottom: 6px; }
.search-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.current-stock { overflow-wrap: anywhere; }
.groups-toggle { margin-bottom: 8px; }
.watchlist .group-sort { flex-wrap: wrap; display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }
.group-sort select { background: var(--bg-input); border: 1px solid var(--border-color);
  border-radius: var(--r-2); color: var(--text-primary); font-size: var(--fs-xs); padding: 1px 4px; }
.row-sort { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin: 4px 0; }
.row-sort select { background: var(--bg-input); border: 1px solid var(--border-color);
  border-radius: var(--r-2); color: var(--text-primary); font-size: var(--fs-xs); padding: 1px 4px; }
.g-annual { margin-left: 5px; color: var(--text-secondary); }
.g-verdict { margin-left: 5px; font-size: 10px; padding: 0 5px; border-radius: var(--r-pill);
  border: 1px solid var(--border-color); }
.g-verdict.ok { color: var(--state-ok); border-color: var(--state-ok); }
.g-verdict.warn { color: var(--state-warn); border-color: var(--state-warn); }
.g-verdict.bad { color: var(--state-bad); border-color: var(--state-bad); }
.g-verdict.dim { color: var(--text-dim); }
.g-refresh { margin-left: 4px; font-size: 10px; }
.g-refresh.ok { color: var(--state-ok); }
.g-refresh.warn { color: var(--state-warn); }
.g-refresh.bad { color: var(--state-bad); }
.g-refresh.dim { color: var(--text-dim); }
.groups { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: var(--sp-2); }
/* 悬停摘要浮层：贴在列表右侧（列表自己 overflow:auto，浮层必须在 <ul> 外面才不被裁） */
.watchlist { position: relative; overflow: visible; }
.row-pop { position: absolute; z-index: 30; top: 6px; left: 100%; margin-left: 8px; }
.row-pop-note { position: absolute; z-index: 30; top: 6px; left: 100%; margin-left: 8px;
  background: #151b23; border: 1px solid var(--border-color); border-radius: 6px; padding: 6px 8px;
  max-width: 240px; }
@media (max-width: 1100px) { .row-pop, .row-pop-note { left: auto; right: 0; margin-left: 0; } }
.batch-add { margin-bottom: var(--sp-2); }
.chip { padding: 3px 8px; border-radius: var(--r-pill); border: 1px solid var(--border-color);
  background: var(--bg-input); color: var(--text-muted); font-size: var(--fs-xs); cursor: pointer; }
.chip.active { border-color: var(--accent-gold); color: var(--accent-gold); background: rgba(255, 209, 102, 0.08); }
.live { margin-left: auto; display: inline-flex; align-items: baseline; gap: 4px; font-family: var(--font-mono);
  font-size: var(--fs-xs); flex: 0 0 auto; }
.live .src { max-width: 3.4em; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.live.up { color: var(--accent-up); }
.live.down { color: var(--accent-down); }
.live .src { color: var(--text-dim); }
.rows { list-style: none; margin: 0; padding: 0; max-height: 46vh; overflow: auto; }
.rows li { display: flex; align-items: center; gap: 6px; padding: 4px 6px; border-radius: var(--r-2);
  cursor: pointer; font-size: var(--fs-sm); min-width: 0; }
/* 事故（2026-10-13 用户报：「K 线图左侧自选股票名称看不见」）：
   `.name` 原来写 `flex: 1`（= flex-basis:0），一行里 code + 来源标签 + 实时价 又都不可压缩，
   于是名称成为**第一个被压到 0px** 的列 —— 实测 83 行里 42 行名称宽度 ≤1px（标签越长越惨）。
   修法：给名称一个下限（4.6em），让**来源标签**去收缩省略；实时价那一格也不再吃满。 */
.rows .code { color: inherit; flex: 0 0 auto; }
.rows .rk { color: var(--accent-gold); flex: 0 0 auto; min-width: 2.4em; }
.rows .score { color: var(--text-secondary); flex: 0 0 auto; min-width: 3.1em; text-align: right; }
.rows .live-tag { flex: 0 0 auto; }
.rows .flow { color: var(--text-muted); flex: 0 1 auto; min-width: 0; max-width: 40%;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 9px; }
.rows .name { flex: 1 1 auto; min-width: 4.6em; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; }
.rows .tag { flex: 0 1 auto; min-width: 0; max-width: 34%; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.star { background: none; border: none; color: var(--accent-gold); cursor: pointer; font-size: var(--fs-md); padding: 0 2px; }
.add-btn { margin-left: auto; }
</style>
