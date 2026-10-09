<template>
  <div class="page">
    <PageHeader
      title="涨跌 / 资金流排行"
      description="每日与每周（近 5 个交易日）两个口径的全市场排行。<strong>涨跌</strong>取本地前复权日线；<strong>资金流</strong>取资金流面板（tushare / 东财 / 同花顺三种口径可切换，单位为亿元）。行点开进 K 线，「分时」直达当日分时。"
    >
      <template #actions>
        <select v-model="period" class="picker">
          <option value="daily">每日</option>
          <option value="weekly">每周（近 5 日）</option>
        </select>
        <button class="btn-ghost" :disabled="loading" @click="reload()">刷新</button>
      </template>
    </PageHeader>

    <div class="panel">
      <div class="tabs">
        <button class="tab" :class="{ active: view === 'change' }" @click="view = 'change'">涨跌排行</button>
        <button class="tab" :class="{ active: view === 'moneyflow' }" @click="view = 'moneyflow'">资金流排行</button>
      </div>

      <div class="toolbar">
        <template v-if="view === 'change'">
          <select v-model="changeDirection" class="picker">
            <option value="up">涨幅榜</option>
            <option value="down">跌幅榜</option>
          </select>
        </template>
        <template v-else>
          <select v-model="flowDirection" class="picker">
            <option value="in">净流入榜</option>
            <option value="out">净流出榜</option>
          </select>
          <select v-model="source" class="picker">
            <option v-for="s in overview?.moneyflow_sources || []" :key="s.key" :value="s.key">{{ s.label }}</option>
          </select>
          <select v-model="flowDate" class="picker" v-if="(overview?.moneyflow_dates || []).length">
            <option v-for="d in (overview?.moneyflow_dates || []).slice().reverse()" :key="d" :value="d">{{ d }}</option>
          </select>
        </template>
        <select v-model="board" class="picker">
          <option value="">全部板块</option>
          <option v-for="b in overview?.boards || []" :key="b" :value="b">{{ b }}</option>
        </select>
        <select v-model.number="limit" class="picker">
          <option :value="30">前 30</option>
          <option :value="50">前 50</option>
          <option :value="100">前 100</option>
          <option :value="200">前 200</option>
        </select>
        <label class="chk"><input type="checkbox" v-model="excludeSt" /> 排除 ST</label>
        <span class="pill dim tiny" v-if="session">行情状态 <b>{{ sessionLabel }}</b> · {{ session?.now }}</span>
      </div>

      <StateBlock v-if="error" state="error" title="排行取数失败" :detail="error" :retry="() => reload()" />
      <StateBlock v-else-if="!current" state="loading" title="加载排行…" />

      <template v-else>
        <!-- 涨跌榜 -->
        <template v-if="view === 'change'">
          <div class="pill-row">
            <span class="pill">交易日 <b>{{ changeData?.date }}</b></span>
            <span class="pill" v-if="period === 'weekly'">
              区间 <b>{{ changeData?.base_date }} → {{ changeData?.date }}</b>
            </span>
            <span class="pill">上涨 <b class="up">{{ stats.n_up ?? '—' }}</b></span>
            <span class="pill">下跌 <b class="down">{{ stats.n_down ?? '—' }}</b></span>
            <span class="pill">平盘 <b>{{ stats.n_flat ?? '—' }}</b></span>
            <span class="pill">涨停 <b class="up">{{ stats.n_limit_up ?? '—' }}</b></span>
            <span class="pill">跌停 <b class="down">{{ stats.n_limit_down ?? '—' }}</b></span>
            <span class="pill">全市场均值 <b :class="tone(stats.mean_ret)">{{ pct(stats.mean_ret) }}</b></span>
            <span class="pill dim tiny">共 {{ stats.n_total ?? 0 }} 只</span>
          </div>

          <BarChart :bars="changeBars" :label-width="96" :max-height="360" />

          <div class="table-scroll mt">
            <table class="data-table">
              <thead>
                <tr>
                  <th>#</th><th class="text-left">代码</th><th class="text-left">名称</th>
                  <th class="text-left">行业</th><th class="text-left">板块</th>
                  <th>{{ period === 'weekly' ? '区间涨跌' : '涨跌幅' }}</th><th>当日</th><th>收盘</th>
                  <th>成交额</th><th>连板</th><th class="text-left">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="r in changeData?.rows || []" :key="r.stock_code">
                  <td>{{ r.rank }}</td>
                  <td class="mono text-left link" @click="gotoKline(r.stock_code)">{{ r.stock_code }}</td>
                  <td class="text-left">
                    {{ r.name }}
                    <span v-if="r.is_st" class="flag flag-bad">ST</span>
                  </td>
                  <td class="text-left tiny dim">{{ r.industry || '—' }}</td>
                  <td class="text-left tiny dim">{{ r.market || '—' }}</td>
                  <td :class="tone(r.value)"><b>{{ pct(r.value) }}</b></td>
                  <td :class="tone(r.pct_chg)">{{ pct(r.pct_chg) }}</td>
                  <td>{{ fmt(r.close, 2) }}</td>
                  <td class="dim">{{ money(r.amount) }}</td>
                  <td>
                    <span v-if="r.boards >= 2" class="tag gold">{{ cn(r.boards) }}板</span>
                    <span v-else-if="r.is_limit_up" class="tag ok">首板</span>
                    <span v-else-if="r.is_limit_down" class="tag bad">跌停</span>
                    <span v-else class="dim">—</span>
                  </td>
                  <td class="text-left">
                    <a class="link tiny" @click="gotoIntraday(r.stock_code)">分时</a>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>

        <!-- 资金流榜 -->
        <template v-else>
          <div class="pill-row">
            <span class="pill">数据日 <b>{{ flowData?.date }}</b></span>
            <span class="pill">口径 <b>{{ flowData?.source_label }}</b></span>
            <span class="pill">净流入 <b class="up">{{ stats.n_in ?? '—' }}</b></span>
            <span class="pill">净流出 <b class="down">{{ stats.n_out ?? '—' }}</b></span>
            <span class="pill">全市场合计 <b :class="tone(stats.sum_net_yi)">{{ fmt(stats.sum_net_yi, 1) }} 亿</b></span>
            <span class="pill dim tiny">共 {{ stats.n_total ?? 0 }} 只</span>
            <span class="pill dim tiny" v-if="period === 'weekly' && flowData?.stats?.sum_net_yi != null">
              5 日合计 <b class="mono">{{ fmt(flowData.stats.sum_net_5d_yi, 0) }} 亿</b>
            </span>
          </div>

          <BarChart :bars="flowBars" :label-width="96" :max-height="360" />

          <div class="table-scroll mt">
            <table class="data-table">
              <thead>
                <tr>
                  <th>#</th><th class="text-left">代码</th><th class="text-left">名称</th>
                  <th class="text-left">行业</th>
                  <th>{{ period === 'weekly' ? '5 日净额' : '当日净额' }}</th>
                  <th>5 日</th><th>10 日</th><th>20 日</th>
                  <th>当日涨跌</th><th>收盘</th><th>连板</th><th class="text-left">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="r in flowData?.rows || []" :key="r.stock_code">
                  <td>{{ r.rank }}</td>
                  <td class="mono text-left link" @click="gotoKline(r.stock_code)">{{ r.stock_code }}</td>
                  <td class="text-left">{{ r.name }}</td>
                  <td class="text-left tiny dim">{{ r.industry || '—' }}</td>
                  <td :class="tone(r.value)"><b>{{ fmt(r.value, 2) }}</b></td>
                  <td class="dim">{{ fmt(r.net_5d_yi, 2) }}</td>
                  <td class="dim">{{ fmt(r.net_10d_yi, 2) }}</td>
                  <td class="dim">{{ fmt(r.net_20d_yi, 2) }}</td>
                  <td :class="tone(r.pct_chg)">{{ pct(r.pct_chg) }}</td>
                  <td>{{ fmt(r.close, 2) }}</td>
                  <td>
                    <span v-if="r.boards >= 2" class="tag gold">{{ cn(r.boards) }}板</span>
                    <span v-else-if="r.is_limit_up" class="tag ok">首板</span>
                    <span v-else class="dim">—</span>
                  </td>
                  <td class="text-left">
                    <a class="link tiny" @click="gotoIntraday(r.stock_code)">分时</a>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="hint tiny mt">净流入额为亿元；原始面板单位是万元。三口径定义不同，别跨口径比名次。</p>
        </template>

        <ul class="hint tiny mt">
          <li v-for="n in current.notes || []" :key="n">{{ n }}</li>
        </ul>
        <p class="hint tiny" v-if="changeData?.stats?.error">{{ changeData.stats.error }}</p>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { getChangeRanking, getMoneyflowRanking, getRankingsOverview } from '@/api/client'
import type { RankingRow, RankingsChangeResponse, RankingsMoneyflowResponse, RankingsOverview, SessionPhase } from '@/types/api'
import PageHeader from '@/components/ui/PageHeader.vue'
import StateBlock from '@/components/ui/StateBlock.vue'
import BarChart, { type Bar } from '@/components/ui/BarChart.vue'

const router = useRouter()
const overview = ref<RankingsOverview | null>(null)
const session = ref<SessionPhase | null>(null)
const view = ref<'change' | 'moneyflow'>('change')
const period = ref<'daily' | 'weekly'>('daily')
const limit = ref(50)
const board = ref('')
const excludeSt = ref(false)
const changeDirection = ref<'up' | 'down'>('up')
const flowDirection = ref<'in' | 'out'>('in')
const source = ref('moneyflow')
const flowDate = ref('')
const changeData = ref<RankingsChangeResponse | null>(null)
const flowData = ref<RankingsMoneyflowResponse | null>(null)
const error = ref('')
const loading = ref(false)

const current = computed(() => (view.value === 'change' ? changeData.value : flowData.value))
const stats = computed<Record<string, any>>(() => (current.value?.stats as Record<string, any>) || {})
const sessionLabel = computed(() => {
  const map: Record<string, string> = { auction: '集合竞价', am: '上午盘', lunch: '午间休市', pm: '下午盘', closed: '已收盘' }
  return map[session.value?.phase || 'closed'] || '—'
})

async function reload() {
  loading.value = true
  try {
    if (!overview.value) {
      overview.value = await getRankingsOverview()
      session.value = overview.value.session
      if (!flowDate.value && overview.value.moneyflow_dates?.length) {
        flowDate.value = overview.value.moneyflow_dates[overview.value.moneyflow_dates.length - 1]
      }
    }
    if (view.value === 'change') {
      changeData.value = await getChangeRanking({
        period: period.value, direction: changeDirection.value, limit: limit.value,
        board: board.value, exclude_st: excludeSt.value,
      })
    } else {
      flowData.value = await getMoneyflowRanking({
        period: period.value, direction: flowDirection.value, limit: limit.value,
        source: source.value, date: flowDate.value, board: board.value, exclude_st: excludeSt.value,
      })
    }
    error.value = ''
  } catch (e) {
    error.value = String(e)
  } finally {
    loading.value = false
  }
}

onMounted(reload)

const changeBars = computed<Bar[]>(() =>
  (changeData.value?.rows || []).slice(0, 20).map((r: RankingRow) => ({
    label: r.name || r.stock_code,
    value: r.value || 0,
    text: pct(r.value),
    tone: (r.value || 0) >= 0 ? 'pos' : 'neg',
  })),
)
const flowBars = computed<Bar[]>(() =>
  (flowData.value?.rows || []).slice(0, 20).map((r: RankingRow) => ({
    label: r.name || r.stock_code,
    value: r.value || 0,
    text: `${fmt(r.value, 1)} 亿`,
    tone: (r.value || 0) >= 0 ? 'pos' : 'neg',
  })),
)

function gotoKline(code: string) { void router.push({ path: '/kline', query: { code } }) }
function gotoIntraday(code: string) { void router.push({ path: '/intraday', query: { code } }) }

const fmt = (v: number | null | undefined, nd = 2) =>
  v === null || v === undefined || Number.isNaN(Number(v)) ? '—' : Number(v).toFixed(nd)
const pct = (v: number | null | undefined) =>
  v === null || v === undefined || Number.isNaN(Number(v)) ? '—' : `${Number(v) >= 0 ? '+' : ''}${(Number(v) * 100).toFixed(2)}%`
const tone = (v: number | null | undefined) =>
  v === null || v === undefined || Number.isNaN(Number(v)) ? '' : Number(v) >= 0 ? 'up' : 'down'
function money(yuan: number | null | undefined) {
  if (yuan === null || yuan === undefined) return '—'
  if (yuan >= 1e8) return (yuan / 1e8).toFixed(2) + ' 亿'
  if (yuan >= 1e4) return (yuan / 1e4).toFixed(0) + ' 万'
  return yuan.toFixed(0)
}
const CN_NUM = ['零', '一', '两', '三', '四', '五', '六', '七', '八', '九', '十']
const cn = (n: number) => CN_NUM[n] || String(n)

watch([view, period, board, excludeSt, limit], () => void reload())
watch([changeDirection, flowDirection, source, flowDate], () => void reload())
</script>

<style scoped>
.picker { padding: 5px 10px; background: var(--bg-input); color: var(--text-primary);
  border: 1px solid var(--border-color); border-radius: var(--r-2); }
.chk { display: flex; align-items: center; gap: 5px; font-size: var(--fs-sm); color: var(--text-muted); }
.mt { margin-top: var(--sp-3); }
</style>
