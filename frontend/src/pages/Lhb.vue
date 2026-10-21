<template>
  <div class="page">
    <PageHeader
      title="龙虎榜"
      description="每个交易日上榜个股、买卖金额、净买占比与机构席位。数据来自本地 <code>cn_lhb</code> 台账，只读展示；同一只票因多个上榜原因会出现多条记录。"
    >
      <template #actions>
        <select v-model="date" class="picker" aria-label="龙虎榜日期">
          <option value="">最新</option>
          <option v-for="d in data?.dates || []" :key="d" :value="d">{{ d }}</option>
        </select>
        <button class="btn-ghost" :disabled="loading" @click="reload">刷新</button>
      </template>
    </PageHeader>

    <StateBlock v-if="error" state="error" title="龙虎榜取数失败" :detail="error" :retry="reload" />
    <StateBlock v-else-if="!data" state="loading" title="加载龙虎榜…" />

    <template v-else-if="!data.available">
      <div class="panel"><div class="empty">{{ data.reason || '龙虎榜尚未同步' }}</div></div>
    </template>

    <template v-else>
      <div class="stat-grid">
        <StatTile label="上榜股票" :value="data.summary?.n_stocks ?? 0"
                  :sub="`${data.summary?.n_records ?? 0} 条原因记录`" tone="gold" />
        <StatTile label="净买入合计" :value="money(data.summary?.total_net_amount)"
                  :sub="`净买 ${data.summary?.n_net_buy ?? 0} · 净卖 ${data.summary?.n_net_sell ?? 0}`"
                  :tone="(data.summary?.total_net_amount || 0) >= 0 ? 'up' : 'down'" />
        <StatTile label="机构上榜" :value="data.summary?.n_institution ?? 0"
                  :sub="`机构净额 ${money(data.summary?.total_inst_net)}`" />
        <StatTile label="涨跌家数" :value="`${data.summary?.n_up ?? 0} / ${data.summary?.n_down ?? 0}`"
                  sub="上涨 / 下跌" />
      </div>

      <div class="panel">
        <div class="toolbar">
          <div class="filters">
            <button v-for="f in FILTERS" :key="f.key" class="chip" :class="{ active: filter === f.key }"
                    @click="filter = f.key">{{ f.label }}</button>
          </div>
          <div class="controls">
            <input v-model="query" class="picker search" type="search" placeholder="代码 / 名称 / 原因" />
            <select v-model="sortKey" class="picker">
              <option value="net">净买额降序</option>
              <option value="pct">涨跌幅降序</option>
              <option value="turnover">换手率降序</option>
              <option value="inst">机构净额降序</option>
              <option value="amount">龙虎榜成交额降序</option>
            </select>
          </div>
        </div>

        <h6>
          上榜明细
          <span class="dim tiny">{{ data.date }} · {{ filtered.length }}/{{ data.rows.length }} 条</span>
          <span class="dim tiny">生成于 {{ data.generated_at || '—' }}</span>
        </h6>
        <div class="table-scroll">
          <table class="data-table">
            <thead>
              <tr>
                <th class="text-left">代码 / 名称</th><th class="text-left">行业</th>
                <th>收盘</th><th>涨跌幅</th><th>净买额</th><th>买入 / 卖出</th>
                <th>净买占比</th><th>换手率</th><th>机构买 / 卖</th><th>机构净额</th>
                <th class="text-left">上榜原因</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in filtered" :key="r.stock_code + r.reason">
                <td class="text-left">
                  <a class="code-link mono" @click="gotoKline(r)">{{ r.stock_code }}</a>
                  <span class="name">{{ r.name }}</span>
                </td>
                <td class="text-left tiny dim">{{ r.industry || '—' }}</td>
                <td>{{ fmt(r.close, 2) }}</td>
                <td :class="tone(r.pct_chg)">{{ pct(r.pct_chg) }}</td>
                <td :class="tone(r.net_amount)">{{ money(r.net_amount) }}</td>
                <td class="dim tiny">{{ money(r.buy_amount) }} / {{ money(r.sell_amount) }}</td>
                <td :class="tone(r.net_pct)">{{ fmt(r.net_pct, 2) }}%</td>
                <td class="dim">{{ fmt(r.turnover, 2) }}%</td>
                <td class="tiny">
                  <span :class="tone(r.inst_net_amount)">{{ r.inst_buyers ?? '—' }} / {{ r.inst_sellers ?? '—' }}</span>
                </td>
                <td :class="tone(r.inst_net_amount)">{{ money(r.inst_net_amount) }}</td>
                <td class="text-left reason">{{ r.reason || '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-if="!filtered.length" class="empty tiny">当前筛选没有记录</p>
        <p class="hint tiny">
          “净买额”为龙虎榜买入额减卖出额；“机构买 / 卖”为机构席位数，机构净额为机构席位买入减卖出。
          龙虎榜是事后披露数据，只用于复盘与事件研究，不当作盘中信号。
        </p>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { getLhbDaily, type LhbDaily, type LhbRow } from '@/api/client'
import PageHeader from '@/components/ui/PageHeader.vue'
import StateBlock from '@/components/ui/StateBlock.vue'
import StatTile from '@/components/ui/StatTile.vue'

const router = useRouter()
const data = ref<LhbDaily | null>(null)
const date = ref('')
const filter = ref<'all' | 'buy' | 'sell' | 'inst'>('all')
type SortKey = 'net' | 'pct' | 'turnover' | 'inst' | 'amount'
const sortKey = ref<SortKey>('net')
const query = ref('')
const loading = ref(false)
const error = ref('')

const FILTERS = [
  { key: 'all', label: '全部' },
  { key: 'buy', label: '净买入' },
  { key: 'sell', label: '净卖出' },
  { key: 'inst', label: '机构净买' },
] as const
const SORT_FIELD: Record<SortKey, keyof LhbRow> = {
  net: 'net_amount',
  pct: 'pct_chg',
  turnover: 'turnover',
  inst: 'inst_net_amount',
  amount: 'lhb_amount',
}

async function reload() {
  loading.value = true
  error.value = ''
  try {
    data.value = await getLhbDaily(date.value)
  } catch (e) {
    error.value = String(e)
  } finally {
    loading.value = false
  }
}

const filtered = computed(() => {
  const q = query.value.trim().toLowerCase()
  let rows = [...(data.value?.rows || [])]
  if (filter.value === 'buy') rows = rows.filter((r) => (r.net_amount ?? 0) > 0)
  if (filter.value === 'sell') rows = rows.filter((r) => (r.net_amount ?? 0) < 0)
  if (filter.value === 'inst') rows = rows.filter((r) => (r.inst_net_amount ?? 0) > 0)
  if (q) rows = rows.filter((r) =>
    `${r.stock_code} ${r.name} ${r.industry} ${r.reason}`.toLowerCase().includes(q))
  const field = SORT_FIELD[sortKey.value]
  rows.sort((a, b) => (Number(b[field] ?? -1e30) - Number(a[field] ?? -1e30)))
  return rows
})

watch(date, () => void reload())

onMounted(reload)

function gotoKline(row: LhbRow) {
  const code = row.stock_code.includes('.') ? row.stock_code : `${row.stock_code}.${suffix(row.stock_code)}`
  void router.push({ path: '/kline', query: { code, date: row.trade_date } })
}
function suffix(code: string) {
  if (/^(4|8|92)/.test(code)) return 'BJ'
  if (/^6/.test(code)) return 'SH'
  if (/^(0|3)/.test(code)) return 'SZ'
  return 'BJ'
}
function fmt(v: number | null | undefined, nd = 2) {
  return v == null || Number.isNaN(Number(v)) ? '—' : Number(v).toFixed(nd)
}
function pct(v: number | null | undefined) {
  return v == null ? '—' : `${Number(v) >= 0 ? '+' : ''}${Number(v).toFixed(2)}%`
}
function money(v: number | null | undefined) {
  if (v == null || Number.isNaN(Number(v))) return '—'
  const n = Number(v)
  if (Math.abs(n) >= 1e8) return `${(n / 1e8).toFixed(2)}亿`
  if (Math.abs(n) >= 1e4) return `${(n / 1e4).toFixed(0)}万`
  return n.toFixed(0)
}
function tone(v: number | null | undefined) {
  return v == null ? '' : Number(v) >= 0 ? 'up' : 'down'
}
</script>

<style scoped>
.toolbar { display: flex; justify-content: space-between; gap: var(--sp-2); flex-wrap: wrap; margin-bottom: var(--sp-2); }
.filters, .controls { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.search { width: 220px; }
.code-link { cursor: pointer; color: var(--accent-blue); }
.name { margin-left: 6px; }
.reason { max-width: 360px; white-space: normal; line-height: 1.45; }
@media (max-width: 760px) { .search { width: 150px; } .reason { max-width: 220px; } }
</style>
