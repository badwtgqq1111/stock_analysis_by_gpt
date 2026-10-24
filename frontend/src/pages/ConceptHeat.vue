<template>
  <div class="page">
    <PageHeader
      title="概念热度"
      description="概念排行与行业热度并列：广度 + 涨幅 + 成交额占比 + 涨停密度合成热度，点概念看成分股。数据来自新浪概念与 iFinD 概念快照；只做观察，不生成买卖指令。"
    >
      <template #actions>
        <select v-model="sortBy" class="picker">
          <option value="score">按热度</option>
          <option value="chg">按涨幅</option>
          <option value="breadth">按广度</option>
          <option value="limit_up">按涨停数</option>
          <option value="amount_share">按成交额占比</option>
        </select>
        <button class="btn-ghost" :disabled="loading" @click="reload">刷新</button>
      </template>
    </PageHeader>

    <StateBlock v-if="error" state="error" title="概念排行取不到" :detail="error" :retry="reload" />
    <StateBlock v-else-if="loading && !data" state="loading" title="加载概念排行…" />

    <template v-else-if="data?.available">
      <div class="panel">
        <div class="pill-row">
          <span class="pill">概念 <b>{{ rows.length }}</b> 个</span>
          <span class="pill" v-if="data.coverage">覆盖 <b>{{ data.coverage.coverage_pct }}%</b>
            （{{ data.coverage.covered }}/{{ data.coverage.stocks }} 只）</span>
          <span class="pill" v-if="data.coverage?.watchlist_date">观察池 <b>{{ data.coverage.watchlist_date }}</b></span>
          <span class="pill dim tiny">概念成员：新浪 + iFinD 快照 · 行情 15 秒刷新</span>
        </div>
      </div>

      <div class="two-col">
        <div class="col-main">
          <div class="panel">
            <h6>概念热度网格 <span class="dim tiny">颜色越红越热 · 点击看成分股</span></h6>
            <div class="heat-grid">
              <button v-for="r in rows" :key="r.concept" class="heat-chip"
                      :class="{ active: r.concept === selected }"
                      :style="chipStyle(r)" @click="select(r.concept)">
                <span class="hc-name">{{ r.concept }}</span>
                <span class="hc-heat">{{ r.heat.toFixed(0) }}</span>
                <span class="hc-ret" :class="tone(r.chg)">{{ r.chg.toFixed(2) }}%</span>
                <span v-if="r.family && r.family !== r.concept" class="hc-family">{{ r.family }}</span>
              </button>
            </div>
          </div>
        </div>

        <div class="col-side">
          <div class="panel">
            <div class="chart-head">
              <div>
                <b>{{ selected }}</b>
                <span class="tiny dim" v-if="stocks?.available">
                  · {{ stocks.n }} 只 · 等权 {{ (stocks.chg ?? 0).toFixed(2) }}% · 广度 {{ ((stocks.breadth ?? 0) * 100).toFixed(1) }}% · 涨停 {{ stocks.limit_up }}
                </span>
              </div>
              <span class="pill tiny" v-if="stocks?.available">成交额 {{ stocks.amount_yi }} 亿</span>
            </div>
            <StateBlock v-if="stockError" state="error" title="成分股取不到" :detail="stockError" />
            <div v-else-if="stockLoading" class="loading-mini">加载成分股…</div>
            <div v-else-if="stocks?.available" class="table-scroll">
              <table class="data-table">
                <thead><tr><th>#</th><th class="text-left">代码</th><th class="text-left">名称</th>
                  <th>涨幅</th><th>成交额</th><th>换手</th></tr></thead>
                <tbody>
                  <tr v-for="(s, i) in stocks.stocks" :key="s.stock_code" class="clickable"
                      @click="gotoKline(s.stock_code)">
                    <td class="mono dim">{{ i + 1 }}</td>
                    <td class="text-left mono tiny">{{ s.stock_code }}</td>
                    <td class="text-left"><b>{{ s.name }}</b></td>
                    <td class="mono" :class="tone(s.pct)">{{ s.pct.toFixed(2) }}%</td>
                    <td class="mono dim">{{ s.amount_yi.toFixed(1) }} 亿</td>
                    <td class="mono dim">{{ s.turnover.toFixed(1) }}%</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div v-else class="empty tiny">该概念暂无成分行情</div>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import PageHeader from '@/components/ui/PageHeader.vue'
import StateBlock from '@/components/ui/StateBlock.vue'
import { getMarketConceptStocks, getMarketConcepts } from '@/api/client'
import type { MarketConcepts, MarketConceptStocks } from '@/types/api'

const router = useRouter()
const data = ref<MarketConcepts | null>(null)
const stocks = ref<MarketConceptStocks | null>(null)
const selected = ref('')
const sortBy = ref('score')
const loading = ref(false)
const stockLoading = ref(false)
const error = ref('')
const stockError = ref('')

const rows = computed(() => {
  const items = [...(data.value?.items || [])].map(r => ({ ...r, heat: Math.round((r.score ?? 0) * 100) }))
  const key = sortBy.value as 'score' | 'chg' | 'breadth' | 'limit_up' | 'amount_share'
  items.sort((a, b) => Number(b[key] ?? 0) - Number(a[key] ?? 0))
  return items
})

function maxOf(key: 'chg' | 'breadth' | 'amount_share' | 'limit_up') {
  return Math.max(...rows.value.map(r => Number(r[key] ?? 0)), 1)
}
function chipStyle(r: { chg: number; breadth: number; limit_up: number; score: number }) {
  const heat = Math.max(0, Math.min(1, (r.score ?? 0)))
  const alpha = 0.10 + heat * 0.45
  return { background: `rgba(220, 38, 38, ${alpha})`, borderColor: `rgba(248, 113, 113, ${0.3 + heat * 0.5})` }
}
function tone(v: number | null) { return (v ?? 0) >= 0 ? 'up' : 'down' }
function gotoKline(code: string) { void router.push({ path: '/kline', query: { code } }) }

async function select(name: string) {
  selected.value = name
  stockLoading.value = true; stockError.value = ''
  try { stocks.value = await getMarketConceptStocks(name, 200) }
  catch (e) { stockError.value = String(e); stocks.value = null }
  finally { stockLoading.value = false }
}

async function reload() {
  loading.value = true; error.value = ''
  try {
    data.value = await getMarketConcepts(300)
    if (data.value.items.length) await select(selected.value || data.value.items[0].concept)
  } catch (e) { error.value = String(e) }
  finally { loading.value = false }
}

let timer: number | undefined
onMounted(() => {
  reload()
  timer = window.setInterval(reload, 15000)
})
onUnmounted(() => { if (timer) window.clearInterval(timer) })
</script>

<style scoped>
.hc-family { display: block; font-size: 10px; color: var(--text-dim); margin-top: 2px; }
.col-side { width: 560px; }
@media (max-width: 1280px) { .col-side { width: 100%; } }
</style>
