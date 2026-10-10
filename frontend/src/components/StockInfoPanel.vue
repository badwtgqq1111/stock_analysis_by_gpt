<template>
  <div class="panel info-panel" v-if="code">
    <h6>
      个股资料
      <span class="mono dim tiny">{{ code }}</span>
      <span class="dim tiny" v-if="bundle?.built_at">更新 {{ bundle.built_at }}</span>
      <span class="tabs inline">
        <button v-for="t in tabs" :key="t.key" class="tab" :class="{ active: tab === t.key }"
                @click="tab = t.key">{{ t.label }}</button>
      </span>
    </h6>

    <StateBlock v-if="error" state="error" title="取不到个股资料" :detail="error" :retry="() => reload()" />
    <StateBlock v-else-if="!bundle" state="loading" title="加载个股资料…" />

    <template v-else>
      <!-- 概况 -->
      <template v-if="tab === 'profile'">
        <div class="pill-row">
          <span class="pill">{{ bundle.profile?.name }} <b>{{ bundle.profile?.board }}</b></span>
          <span class="pill" v-if="bundle.profile?.tushare_industry">行业 <b>{{ bundle.profile.tushare_industry }}</b></span>
          <span class="pill" v-if="bundle.profile?.sw2021?.length">申万 <b>{{ bundle.profile.sw2021.join('、') }}</b></span>
          <span class="pill" v-if="bundle.profile?.list_date">上市 <b>{{ bundle.profile.list_date }}</b></span>
          <span class="pill" v-if="bundle.limits?.available">
            近 {{ bundle.limits.window }} 日涨停 <b class="up">{{ bundle.limits.zt_days }}</b> 次
            <template v-if="bundle.limits.last_zt">（最近 {{ bundle.limits.last_zt }}）</template>
          </span>
          <span class="pill" v-if="bundle.limits?.boards >= 2">当前 <b class="gold">{{ cn(bundle.limits.boards) }}板</b></span>
        </div>
        <div class="kv" v-if="bundle.valuation?.available">
          <dt>估值（{{ bundle.valuation.trade_date }}）</dt>
          <dd>
            PE {{ bundle.valuation.pe ?? '—' }}｜PB {{ bundle.valuation.pb ?? '—' }}｜
            股息率 {{ bundle.valuation.dv_ratio ?? '—' }}%｜换手 {{ bundle.valuation.turnover_rate ?? '—' }}%｜
            总市值 {{ bundle.valuation.total_mv_yi ?? '—' }} 亿｜流通市值 {{ bundle.valuation.circ_mv_yi ?? '—' }} 亿
          </dd>
          <dt>所属概念</dt>
          <dd>{{ (bundle.profile?.concepts || []).join('、') || '—' }}</dd>
          <dt>标签</dt>
          <dd><span v-for="f in bundle.profile?.flags || []" :key="f" class="flag flag-bad">{{ f }}</span>
            <span v-if="!(bundle.profile?.flags || []).length" class="dim">无</span></dd>
        </div>
        <div class="pill-row">
          <a class="btn-ghost btn-sm" :href="bundle.links?.eastmoney" target="_blank" rel="noopener">东财行情 ↗</a>
          <a class="btn-ghost btn-sm" :href="bundle.links?.ths" target="_blank" rel="noopener">同花顺 F10 ↗</a>
          <a class="btn-ghost btn-sm" :href="bundle.links?.xueqiu" target="_blank" rel="noopener">雪球 ↗</a>
          <a class="btn-ghost btn-sm" :href="bundle.links?.cninfo" target="_blank" rel="noopener">巨潮公告 ↗</a>
          <span class="dim tiny">新闻：本仓库无本地新闻源，用上面的外站（东财/同花顺/雪球）</span>
        </div>
      </template>

      <!-- 财务 -->
      <template v-else-if="tab === 'financials'">
        <table class="data-table" v-if="bundle.financials?.available">
          <thead><tr><th>报告期</th><th>披露</th><th>ROE(加权)</th><th>毛利率</th><th>净利率</th>
            <th>负债率</th><th>EPS</th><th>BPS</th><th>营收YoY</th><th>净利YoY</th></tr></thead>
          <tbody>
            <tr v-for="r in (bundle.financials.rows || []).slice().reverse()" :key="r.end_date">
              <td class="mono">{{ r.end_date }}</td><td class="mono dim">{{ r.ann_date }}</td>
              <td>{{ fmt(r.roe_waa, 2) }}</td><td>{{ fmt(r.grossprofit_margin, 2) }}</td>
              <td>{{ fmt(r.netprofit_margin, 2) }}</td><td>{{ fmt(r.debt_to_assets, 2) }}</td>
              <td>{{ fmt(r.eps, 3) }}</td><td>{{ fmt(r.bps, 2) }}</td>
              <td :class="tone(r.or_yoy)">{{ fmt(r.or_yoy, 1) }}%</td>
              <td :class="tone(r.netprofit_yoy)">{{ fmt(r.netprofit_yoy, 1) }}%</td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty tiny">{{ bundle.financials?.reason || '无财务数据' }}</div>
      </template>

      <!-- 资金流 -->
      <template v-else-if="tab === 'flow'">
        <div class="pill-row" v-if="bundle.flow?.available">
          <span class="pill">5 日净流入 <b :class="tone(bundle.flow.net_5d_yi)">{{ fmt(bundle.flow.net_5d_yi, 2) }} 亿</b></span>
          <span class="pill">20 日 <b :class="tone(bundle.flow.net_20d_yi)">{{ fmt(bundle.flow.net_20d_yi, 2) }} 亿</b></span>
          <span class="pill">东财主力（最新） <b :class="tone(bundle.flow.dc_net_yi)">{{ fmt(bundle.flow.dc_net_yi, 2) }} 亿</b></span>
          <span class="pill">同花顺（最新） <b :class="tone(bundle.flow.ths_net_yi)">{{ fmt(bundle.flow.ths_net_yi, 2) }} 亿</b></span>
        </div>
        <BarChart v-if="bundle.flow?.available" :bars="flowBars" :label-width="66" :max-height="300" />
        <div v-else class="empty tiny">{{ bundle.flow?.reason || '无资金流数据' }}</div>
      </template>

      <!-- 龙虎榜 -->
      <template v-else-if="tab === 'lhb'">
        <table class="data-table" v-if="(bundle.lhb?.rows || []).length">
          <thead><tr><th>日期</th><th>上榜原因</th><th>净买入(亿)</th><th>买入(亿)</th><th>卖出(亿)</th>
            <th>机构净额(亿)</th><th>机构买/卖家数</th></tr></thead>
          <tbody>
            <tr v-for="r in bundle.lhb.rows" :key="r.date + r.reason">
              <td class="mono">{{ r.date }}</td><td class="tiny">{{ r.reason || '—' }}</td>
              <td :class="tone(r.net_yi)">{{ fmt(r.net_yi, 3) }}</td>
              <td class="dim">{{ fmt(r.buy_yi, 3) }}</td><td class="dim">{{ fmt(r.sell_yi, 3) }}</td>
              <td :class="tone(r.inst_net_yi)">{{ fmt(r.inst_net_yi, 3) }}</td>
              <td class="tiny dim">{{ r.inst_buyers ?? '—' }} / {{ r.inst_sellers ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty tiny">{{ bundle.lhb?.available ? '近端没有上榜记录' : (bundle.lhb?.reason || '—') }}</div>
      </template>

      <!-- 选股记录（哪些策略选过它）-->
      <template v-else-if="tab === 'selection'">
        <div class="pill-row" v-if="(bundle.selections?.latest || []).length">
          <span class="pill" v-for="l in bundle.selections.latest" :key="l.strategy">
            <b class="gold">{{ l.strategy }}</b> {{ l.trade_date }} rank {{ l.rank ?? '—' }}
            <template v-if="l.weight">｜权重 {{ (l.weight * 100).toFixed(1) }}%</template>
          </span>
        </div>
        <table class="data-table" v-if="(bundle.selections?.rows || []).length">
          <thead><tr><th>决策日</th><th>来源/策略</th><th>rank</th><th>权重</th>
            <th>T+1</th><th>T+5</th><th>T+20</th><th>超额T+5</th></tr></thead>
          <tbody>
            <tr v-for="r in bundle.selections.rows" :key="r.decision_date + r.source">
              <td class="mono">{{ r.decision_date }}</td><td class="mono tiny">{{ r.source }}</td>
              <td>{{ r.rank ?? '—' }}</td>
              <td>{{ r.weight ? (r.weight * 100).toFixed(1) + '%' : '—' }}</td>
              <td :class="tone(r.r1)">{{ pct(r.r1) }}</td><td :class="tone(r.r5)">{{ pct(r.r5) }}</td>
              <td :class="tone(r.r20)">{{ pct(r.r20) }}</td><td :class="tone(r.excess_r5)">{{ pct(r.excess_r5) }}</td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty tiny">台账里没有这只票的选股记录（{{ bundle.selections?.n_days ?? 0 }} 天）</div>
      </template>

      <!-- 公告 -->
      <template v-else>
        <ul class="news" v-if="(bundle.announcements?.rows || []).length">
          <li v-for="(n, i) in bundle.announcements.rows" :key="i">
            <span class="mono dim tiny">{{ n.date }}</span>
            <a :href="n.url" target="_blank" rel="noopener">{{ n.title }}</a>
          </li>
        </ul>
        <div v-else class="empty tiny">
          {{ bundle.announcements?.available ? '近期无公告' : (bundle.announcements?.reason || '取公告失败') }}
        </div>
        <p class="hint tiny">来源：{{ bundle.announcements?.source || '巨潮资讯' }}（外站链接）</p>
      </template>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { getStockBundle } from '@/api/client'
import type { Bar } from '@/components/ui/BarChart.vue'
import BarChart from '@/components/ui/BarChart.vue'
import StateBlock from '@/components/ui/StateBlock.vue'

const props = defineProps<{ code: string | null }>()
const bundle = ref<any>(null)
const error = ref('')
const tab = ref<'profile' | 'financials' | 'flow' | 'lhb' | 'selection' | 'news'>('profile')
const tabs = [
  { key: 'profile', label: '概况' },
  { key: 'financials', label: '财务' },
  { key: 'flow', label: '资金' },
  { key: 'lhb', label: '龙虎榜' },
  { key: 'selection', label: '选股记录' },
  { key: 'news', label: '公告' },
] as const

const flowBars = computed<Bar[]>(() => {
  const rows = (bundle.value?.flow?.rows || []) as { date: string; net_wan: number | null }[]
  return rows.slice(-20).map((r) => ({
    label: r.date.slice(5), value: (r.net_wan || 0) / 1e4,
    text: `${((r.net_wan || 0) / 1e4).toFixed(2)} 亿`,
    tone: (r.net_wan || 0) >= 0 ? 'pos' : 'neg',
  }))
})

async function reload() {
  if (!props.code) { bundle.value = null; return }
  error.value = ''
  try {
    bundle.value = await getStockBundle(props.code)
  } catch (e) {
    error.value = String(e)
    bundle.value = null
  }
}
watch(() => props.code, () => { void reload() }, { immediate: true })

const fmt = (v: unknown, nd = 2) =>
  v === null || v === undefined || Number.isNaN(Number(v)) ? '—' : Number(v).toFixed(nd)
const pct = (v: unknown) =>
  v === null || v === undefined || Number.isNaN(Number(v)) ? '—' : `${Number(v) >= 0 ? '+' : ''}${(Number(v) * 100).toFixed(2)}%`
const tone = (v: unknown) =>
  v === null || v === undefined || Number.isNaN(Number(v)) ? '' : Number(v) >= 0 ? 'up' : 'down'
const CN_NUM = ['零', '一', '两', '三', '四', '五', '六', '七', '八', '九', '十']
const cn = (n: number) => CN_NUM[n] || String(n)
</script>

<style scoped>
.info-panel h6 { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.tabs.inline { margin-left: auto; margin-bottom: 0; }
.news { list-style: none; margin: 0; padding: 0; max-height: 320px; overflow: auto; }
.news li { display: flex; gap: var(--sp-2); padding: 4px 0; border-bottom: 1px solid var(--border-soft); font-size: var(--fs-sm); }
.news a { color: var(--accent-blue); text-decoration: none; }
.news a:hover { text-decoration: underline; }
.gold { color: var(--accent-gold); }
.kv { margin: var(--sp-2) 0; }
</style>
