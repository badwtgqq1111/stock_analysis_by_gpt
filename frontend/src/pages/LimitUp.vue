<template>
  <div class="page">
    <PageHeader
      title="连板记录统计"
      description="一板 / 两板 / 三板 / 四板+ 的每日家数、最高板与<strong>晋级率</strong>（昨日 N 板 → 今日 N+1 板的比例）。历史来自本地日线（离线可算），当日梯队由全市场实时快照和历史日线连续计算；盘中开板即移出，卡片和趋势同源。封板资金等缺失字段留空。"
    >
      <template #actions>
        <select v-model.number="lookback" class="picker">
          <option :value="15">近 15 个交易日</option>
          <option :value="30">近 30 个交易日</option>
          <option :value="60">近 60 个交易日</option>
        </select>
        <input v-model="date" type="date" class="picker" :max="cnToday()" aria-label="连板统计历史日期" />
        <button v-if="date" class="btn-ghost btn-sm" @click="date = ''">最新</button>
        <label class="hint tiny"><input type="checkbox" v-model="realtime" /> 当日实时（15秒）</label>
        <button class="btn-ghost" :disabled="loading" @click="reload()">刷新</button>
      </template>
    </PageHeader>

    <RealtimeStatus :meta="detail?.realtime" :enabled="liveEnabled" />
    <StateBlock v-if="error" state="error" title="连板数据取数失败" :detail="error" :retry="() => reload()" />
    <StateBlock v-else-if="!ladder" state="loading" title="加载连板阶梯…" />

    <template v-else>
      <!-- 统计卡 -->
      <div class="stat-grid">
        <StatTile label="涨停家数" :value="detail?.total ?? 0" tone="up"
                  :sub="selectedDate ? `${selectedDate} ${detail?.realtime?.available ? '当前封板' : '收板'}` : '—'" />
        <StatTile label="最高板" :value="`${cn(detail?.max_boards || 0)}板`" tone="gold"
                  :sub="`来自 ${detail?.realtime?.available ? '实时快照+历史' : '本地日线'}`" />
        <StatTile v-for="b in buckets" :key="b.key" :label="b.label"
                  :value="detail?.counts?.[b.key] ?? 0"
                  :sub="promotionText(b.key)" />
      </div>
      <p class="hint tiny" v-if="detail && ladderTotal !== null && detail.total !== ladderTotal">
        本页合计 <b>{{ detail.total }}</b> 只 = 本地日线 {{ ladderTotal }} 只 + 东财涨停池补充
        {{ detail.total - ladderTotal }} 只（本地日线里没有的票，主要是北交所新股）；
        因此卡片家数会比阶梯趋势图（纯本地日线口径）多。
      </p>

      <!-- 阶梯趋势 -->
      <div class="panel">
        <h6>近 {{ lookback }} 个交易日：各板家数</h6>
        <SvgCurve :series="ladderSeries" :height="220" :labels="ladder.dates" />
        <h6 class="mt">晋级率（昨日 N 板 → 今日 N+1 板，%）</h6>
        <SvgCurve :series="promotionSeries" :height="170" :labels="ladder.dates" />
        <p class="hint tiny mt">
          {{ ladder.notes.join(' ') }}
        </p>
      </div>

      <!-- 明细 -->
      <div class="panel">
        <h6>
          涨停明细
          <span class="dim tiny"> {{ selectedDate }} · {{ detail?.total ?? 0 }} 只</span>
          <span class="tag" :class="detail?.live?.available ? 'ok' : 'dim'">
            {{ detail?.realtime?.available ? '当前报价封板，非收盘结果' : '本地归档' }}
          </span>
        </h6>
        <div class="table-scroll">
          <table class="data-table">
            <thead>
              <tr>
                <th>#</th><th>连板</th><th class="text-left">代码</th><th class="text-left">名称</th>
                <th class="text-left">行业</th><th>涨跌幅</th><th>收盘</th><th>成交额</th>
                <th>封板资金</th><th>首封</th><th>炸板</th><th class="text-left">来源</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(r, i) in detail?.rows || []" :key="r.stock_code">
                <td>{{ i + 1 }}</td>
                <td><span class="tag gold">{{ cn(r.boards) }}板</span></td>
                <td class="mono text-left link" @click="gotoKline(r.stock_code)">{{ r.stock_code }}</td>
                <td class="text-left">
                  {{ r.name }}
                  <span v-if="r.st_pit" class="flag flag-bad">ST</span>
                </td>
                <td class="text-left tiny dim">{{ r.industry || '—' }}</td>
                <td :class="tone(r.pct_chg)">{{ pct(r.pct_chg) }}</td>
                <td>{{ fmt(r.close, 2) }}</td>
                <td class="dim">{{ money(r.amount) }}</td>
                <td class="dim">{{ money(r.seal_amount) }}</td>
                <td class="mono tiny dim">{{ seal(r.first_seal) }}</td>
                <td class="tiny dim">{{ r.open_times ?? '—' }}</td>
                <td class="text-left tiny">
                  <span class="tag" :class="r.source === 'live' ? 'info' : r.source === 'both' ? 'ok' : ''">
                    {{ sourceLabel(r.source) }}
                  </span>
                  <a class="link" @click="gotoIntraday(r.stock_code)">分时</a>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="hint tiny mt" v-if="detail?.live?.reason && !detail.live.available">
          东财涨停池不可用（{{ detail.live.reason }}）→ 本页只用本地日线口径。
        </p>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { getLimitUpDetail, getLimitUpLadder } from '@/api/client'
import type { LimitUpDetail, LimitUpLadder } from '@/types/api'
import PageHeader from '@/components/ui/PageHeader.vue'
import StateBlock from '@/components/ui/StateBlock.vue'
import StatTile from '@/components/ui/StatTile.vue'
import SvgCurve from '@/components/ui/SvgCurve.vue'

import RealtimeStatus from '@/components/RealtimeStatus.vue'
import { cnToday, useMarketPoll } from '@/composables/useMarketPoll'

const realtime = ref(true)
const liveEnabled = computed(() => realtime.value && !date.value)
const router = useRouter()
const lookback = ref(30)
const date = ref('')
const ladder = ref<LimitUpLadder | null>(null)
const detail = ref<LimitUpDetail | null>(null)
const error = ref('')
const loading = ref(false)

const buckets = computed(() => ladder.value?.buckets || [
  { key: 'b1', label: '一板' }, { key: 'b2', label: '两板' },
  { key: 'b3', label: '三板' }, { key: 'b4p', label: '四板+' },
])
const selectedDate = computed(() => detail.value?.date || date.value)
const promotion = computed(() => detail.value && ladder.value
  ? (ladder.value.series.find((s) => s.date === detail.value?.date)?.promotion || {})
  : {})
/** 阶梯趋势里当天的"纯本地日线"家数（用来解释与合并口径的差额） */
const ladderTotal = computed(() => {
  if (!detail.value || !ladder.value) return null
  const hit = ladder.value.series.find((s) => s.date === detail.value?.date)
  return hit ? hit.total : null
})

const COLORS: Record<string, string> = {
  b1: '#118ab2', b2: '#ffd166', b3: '#ff9f1c', b4p: '#ef476f',
}
const ladderSeries = computed(() => {
  const series = ladder.value
  if (!series) return []
  return buckets.value.map((b) => ({
    name: b.label, color: COLORS[b.key] || '#8a8f97',
    points: series.series.map((s) => s.counts[b.key] ?? 0),
  }))
})
const promotionSeries = computed(() => {
  const data = ladder.value
  if (!data) return []
  return buckets.value.map((b) => ({
    name: `${b.label}晋级`, color: COLORS[b.key] || '#8a8f97',
    points: data.series.map((s) => {
      const rate = s.promotion?.[b.key]?.rate
      return rate === null || rate === undefined ? 0 : rate * 100
    }),
  }))
})

function promotionText(key: string) {
  const p = promotion.value?.[key]
  if (!p) return '—'
  const rate = p.rate === null || p.rate === undefined ? '—' : `${(p.rate * 100).toFixed(0)}%`
  // 分母是**本地日线口径**的昨日家数（东财补进来的票没有历史日线，算不出晋级），标注清楚免得对不上卡片数
  return `本地晋级 ${p.next}/${p.prev} = ${rate}`
}

async function fetchLimitup(live = false) {
  const request = ++requestId
  loading.value = true
  try {
    const [lad, det] = await Promise.all([
      live ? getLimitUpLadder(lookback.value, date.value, true) : getLimitUpLadder(lookback.value, date.value),
      live ? getLimitUpDetail(date.value, true) : getLimitUpDetail(date.value, false),
    ])
    if (request !== requestId) return
    ladder.value = lad
    detail.value = det
    error.value = ''
  } catch (e) {
    if (request === requestId) error.value = String(e)
  } finally {
    if (request === requestId) loading.value = false
  }
}

let reloadId = 0
let alive = true
async function reload() {
  const request = ++reloadId
  await fetchLimitup(false)
  if (alive && request === reloadId && liveEnabled.value) await fetchLimitup(true)
}
let requestId = 0
onMounted(reload)
watch([lookback, date, realtime], () => void reload())
useMarketPoll(() => fetchLimitup(true), () => liveEnabled.value && !loading.value)
onUnmounted(() => { alive = false; ++reloadId; ++requestId })

function gotoKline(code: string) { void router.push({ path: '/kline', query: { code } }) }
function gotoIntraday(code: string) { void router.push({ path: '/intraday', query: { code } }) }

const fmt = (v: number | null | undefined, nd = 2) =>
  v === null || v === undefined || Number.isNaN(Number(v)) ? '—' : Number(v).toFixed(nd)
const pct = (v: number | null | undefined) =>
  v === null || v === undefined ? '—' : `${Number(v) >= 0 ? '+' : ''}${(Number(v) * 100).toFixed(2)}%`
const tone = (v: number | null | undefined) => (v === null || v === undefined ? '' : Number(v) >= 0 ? 'up' : 'down')
const CN_NUM = ['零', '一', '两', '三', '四', '五', '六', '七', '八', '九', '十']
const cn = (n: number) => CN_NUM[n] || `${n}`
function money(v: number | null | undefined) {
  if (v === null || v === undefined) return '—'
  if (v >= 1e8) return (v / 1e8).toFixed(2) + ' 亿'
  if (v >= 1e4) return (v / 1e4).toFixed(0) + ' 万'
  return v.toFixed(0)
}
function seal(v: string | null | undefined) {
  if (!v || v.length < 4) return '—'
  return `${v.slice(0, 2)}:${v.slice(2, 4)}`
}
function sourceLabel(source?: string) {
  return ({ both: '东财+本地', live: '东财', local: '本地', realtime: '实时快照' } as Record<string, string>)[source || 'local'] || '本地'
}
</script>

<style scoped>
.picker { padding: 5px 10px; background: var(--bg-input); color: var(--text-primary);
  border: 1px solid var(--border-color); border-radius: var(--r-2); }
.mt { margin-top: var(--sp-3); }
.live-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: var(--sp-2); margin-top: var(--sp-2); }
.live-card { padding: 6px 8px; background: var(--bg-input); border: 1px solid var(--border-color); border-radius: var(--r-2); }
.lc-head { display: flex; align-items: center; gap: 6px; }
.lc-name { font-size: var(--fs-sm); }
.lc-code { margin-top: 2px; }
.lc-meta { display: flex; justify-content: space-between; margin-top: 2px; }
.gold { color: var(--accent-gold); }
</style>
