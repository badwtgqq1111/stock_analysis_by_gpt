<template>
  <div class="page">
    <PageHeader
      title="分时行情"
      description="当日分时（1 分钟 / 5 分钟）+ 均价线，盘中自动刷新。<code>腾讯分时</code> 为默认实时源（实测最稳），失败自动退 <code>东财分时</code> → <code>腾讯 5 分钟 K</code> → 本地 5 分钟归档；页面顶部角标始终显示这次数据到底来自哪条通路，不会把旧数据当实时价。"
    >
      <template #actions>
        <select v-model="period" class="picker">
          <option value="1">1 分钟</option>
          <option value="5">5 分钟</option>
        </select>
        <select v-model="mode" class="picker">
          <option value="auto">自动（实时→本地）</option>
          <option value="live">只用实时源</option>
          <option value="store">只用本地归档</option>
        </select>
        <select v-model.number="intervalMs" class="picker">
          <option :value="5000">每 5 秒</option>
          <option :value="15000">每 15 秒</option>
          <option :value="30000">每 30 秒</option>
          <option :value="60000">每 60 秒</option>
          <option :value="0">不自动刷新</option>
        </select>
        <button class="btn-ghost" :disabled="loading" @click="reload()">刷新</button>
      </template>
    </PageHeader>

    <div class="panel">
      <div class="toolbar">
        <StockSelector v-model="code" />
        <span class="pill" :class="sessionTone">
          {{ sessionText }}
        </span>
        <span class="pill" v-if="data">
          数据源 <b>{{ data.source_label }}</b>
          <span class="dim tiny">（{{ data.date }}）</span>
        </span>
        <span class="pill dim tiny" v-if="intervalMs && left > 0">下次刷新 {{ left }}s</span>
        <span class="pill dim tiny" v-if="loadedAt">更新于 {{ loadedAtText }}</span>
      </div>

      <div class="chips" v-if="chips.length">
        <button v-for="c in chips" :key="c.stock_code" class="chip"
                :class="{ active: c.stock_code === code }" @click="code = c.stock_code">
          <span class="chip-name">{{ c.name || c.stock_code }}</span>
          <span class="chip-px" :class="tone(c.change_pct)">{{ fmt(c.price, 2) }}</span>
          <span class="chip-pct" :class="tone(c.change_pct)">{{ pct(c.change_pct) }}</span>
        </button>
      </div>

      <StateBlock v-if="error" state="error" title="取不到分时数据" :detail="error" :retry="() => reload()" />
      <StateBlock v-else-if="!data" state="loading" title="加载分时…" />
    </div>

    <template v-if="data">
      <div class="stat-grid">
        <StatTile label="最新价" :value="fmt(data.latest.price, 2)" :tone="tone(data.latest.change_pct)"
                  :sub="`前收 ${fmt(data.prev_close, 2)}`" />
        <StatTile label="涨跌幅" :value="pct(data.latest.change_pct)" :tone="tone(data.latest.change_pct)"
                  :sub="`涨跌 ${fmt(data.latest.change, 2)}`" />
        <StatTile label="均价" :value="fmt(data.latest.avg, 2)" tone="gold" sub="累计额 ÷ 累计量" />
        <StatTile label="最高 / 最低" :value="`${fmt(data.latest.high, 2)} / ${fmt(data.latest.low, 2)}`"
                  :sub="`开盘 ${fmt(data.latest.open, 2)}`" />
        <StatTile label="成交额" :value="money(data.latest.amount_yuan)" sub="元 → 亿/万" />
        <StatTile label="成交量" :value="shares(data.latest.volume_shares)" :sub="`${data.latest.points} 个分时点`" />
      </div>

      <div class="panel">
        <IntradayChart :code="data.code" :date="data.date" :points="data.points" :prev-close="data.prev_close" :height="340" />
        <p class="hint tiny mt">
          {{ data.name }}（{{ data.code }}）· {{ data.period }} 分钟 ·
          {{ data.points[0]?.time }} → {{ data.latest.time }}
        </p>
        <p class="hint tiny" v-if="!data.live.available && data.live.reason">
          实时源不可用，已降级到本地归档：<span class="dim">{{ data.live.reason }}</span>
        </p>
        <p class="hint tiny" v-if="data.latest.amount_yuan === null">
          该数据源不给成交额（腾讯 5 分钟 K 接口限制）→ 成交额与均价线留空，不猜数。
        </p>
        <ul class="hint tiny">
          <li v-for="n in data.notes" :key="n">{{ n }}</li>
        </ul>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { getIntraday, getIntradayQuotes, getStocks } from '@/api/client'
import type { IntradayResponse, IntradayQuote, SessionPhase } from '@/types/api'
import PageHeader from '@/components/ui/PageHeader.vue'
import StateBlock from '@/components/ui/StateBlock.vue'
import StatTile from '@/components/ui/StatTile.vue'
import StockSelector from '@/components/StockSelector.vue'
import IntradayChart from '@/components/IntradayChart.vue'

const route = useRoute()
// 支持从排行榜/连板页点「分时」带过来的 ?code=600519.SH
const code = ref(String(route.query.code || '000001.SZ').toUpperCase())
const period = ref<'1' | '5'>('1')
const mode = ref<'auto' | 'live' | 'store'>('auto')
const intervalMs = ref(15000)
const data = ref<IntradayResponse | null>(null)
const chips = ref<IntradayQuote[]>([])
const session = ref<SessionPhase | null>(null)
const error = ref('')
const loading = ref(false)
const loadedAt = ref<Date | null>(null)
const left = ref(0)

let timer: ReturnType<typeof setInterval> | null = null
let countdown: ReturnType<typeof setInterval> | null = null

const sessionText = computed(() => {
  const phase = data.value?.session?.phase ?? session.value?.phase
  const map: Record<string, string> = {
    auction: '集合竞价', am: '上午盘', lunch: '午间休市', pm: '下午盘', closed: '已收盘',
  }
  return map[phase || 'closed'] || '—'
})
const sessionTone = computed(() => ((data.value?.session?.live ?? session.value?.live) ? 'live' : 'dim'))
const loadedAtText = computed(() => (loadedAt.value ? loadedAt.value.toLocaleTimeString('zh-CN') : ''))

async function reload(silent = false) {
  if (!silent) loading.value = true
  try {
    data.value = await getIntraday(code.value, period.value, mode.value)
    error.value = ''
    loadedAt.value = new Date()
    left.value = intervalMs.value ? Math.round(intervalMs.value / 1000) : 0
  } catch (e) {
    error.value = String(e)
  } finally {
    loading.value = false
  }
}

async function loadChips() {
  try {
    const list = await getStocks()
    const picked = list.stocks.filter((s) => s.is_selected).slice(0, 10)
    const codes = (picked.length ? picked : list.stocks.slice(0, 8)).map((s) => s.code)
    if (!codes.length) return
    const res = await getIntradayQuotes(codes)
    chips.value = res.quotes.filter((q) => q.available)
    session.value = res.session
  } catch { /* 快捷条拉不到不影响主图 */ }
}

function restartTimer() {
  if (timer) clearInterval(timer)
  if (countdown) clearInterval(countdown)
  timer = null
  countdown = null
  if (!intervalMs.value) { left.value = 0; return }
  left.value = Math.round(intervalMs.value / 1000)
  timer = setInterval(() => { if (!document.hidden) { void reload(true); void loadChips() } }, intervalMs.value)
  countdown = setInterval(() => { left.value = Math.max(left.value - 1, 0) }, 1000)
}

onMounted(async () => {
  await reload()
  void loadChips()
  restartTimer()
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
  if (countdown) clearInterval(countdown)
})

watch([code, period, mode], () => void reload())
watch(intervalMs, () => restartTimer())
// 已经在 /intraday 时，从其它页再点「分时」只变 query，不重新挂载组件 → 必须显式跟随
watch(() => route.query.code, (value) => {
  const next = String(value || '').toUpperCase()
  if (next && next !== code.value) code.value = next
})

const fmt = (v: number | null | undefined, nd = 2) =>
  v === null || v === undefined ? '—' : Number(v).toFixed(nd)
const pct = (v: number | null | undefined) =>
  v === null || v === undefined ? '—' : `${v >= 0 ? '+' : ''}${(Number(v) * 100).toFixed(2)}%`
const tone = (v: number | null | undefined) =>
  v === null || v === undefined ? '' : Number(v) >= 0 ? 'up' : 'down'
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
</script>

<style scoped>
.picker { padding: 5px 10px; background: var(--bg-input); color: var(--text-primary);
  border: 1px solid var(--border-color); border-radius: var(--r-2); }
.pill.live { color: var(--state-ok); border-color: var(--state-ok); }
.chips { display: flex; gap: var(--sp-2); flex-wrap: wrap; margin-top: var(--sp-2); }
.chip {
  display: flex; align-items: center; gap: 6px; padding: 4px 10px; cursor: pointer;
  background: var(--bg-input); border: 1px solid var(--border-color); border-radius: var(--r-pill);
  color: var(--text-muted); font-size: var(--fs-xs);
}
.chip:hover { border-color: var(--text-dim); color: var(--text-primary); }
.chip.active { border-color: var(--accent-gold); color: var(--accent-gold); background: rgba(255, 209, 102, 0.08); }
.chip-name { max-width: 76px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chip-px { font-family: var(--font-mono); }
.chip-pct { font-family: var(--font-mono); }
.mt { margin-top: var(--sp-2); }
</style>
