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
          <span class="dim tiny">新闻/研报见上方 tab；本地证据不足时用外站（东财/同花顺/雪球/巨潮）</span>
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
        <p class="hint tiny">财报字段来自本地 PIT 财务面板（按报告期/披露日）；公告原文见「公告」tab 或巨潮链接。</p>
      </template>

      <!-- 研报：Bynewlt 优先，缺则公开来源 dossier -->
      <template v-else-if="tab === 'research'">
        <template v-if="bundle.research?.available">
          <div class="pill-row">
            <span class="pill">立场 <b class="gold">{{ bundle.research.stance || '证据不足' }}</b></span>
            <span class="pill">来源 <b>{{ bundle.research.source === 'public_web' ? '公开来源' : 'Bynewlt' }}</b></span>
            <span class="pill" v-if="bundle.research.updated_at">更新 {{ String(bundle.research.updated_at).slice(0, 10) }}</span>
            <span class="pill dim tiny" v-if="bundle.research.window">{{ bundle.research.window }}</span>
          </div>
          <p class="ls-summary" v-if="bundle.research.summary">{{ bundle.research.summary }}</p>
          <div class="case-grid">
            <div class="case bull">
              <b>多头依据</b>
              <ul v-if="bundle.research.bull?.length">
                <li v-for="(p, i) in bundle.research.bull" :key="'rb' + i">{{ p }}</li>
              </ul>
              <p v-else class="dim">—</p>
            </div>
            <div class="case bear">
              <b>空头依据</b>
              <ul v-if="bundle.research.bear?.length">
                <li v-for="(p, i) in bundle.research.bear" :key="'rs' + i">{{ p }}</li>
              </ul>
              <p v-else class="dim">—</p>
            </div>
          </div>
          <p class="risk" v-if="bundle.research.risks?.length"><b>风险：</b>{{ bundle.research.risks.join('；') }}</p>
          <table class="data-table tiny" v-if="bundle.research.ratings?.length">
            <thead><tr><th>机构</th><th>评级</th><th>目标价</th><th>日期</th></tr></thead>
            <tbody>
              <tr v-for="(r, i) in bundle.research.ratings" :key="'rr' + i">
                <td>{{ r.broker || '—' }}</td><td>{{ r.rating || '—' }}</td>
                <td>{{ r.target_price || '—' }}</td><td class="mono">{{ r.date || '—' }}</td>
              </tr>
            </tbody>
          </table>
          <details class="cites" v-if="bundle.research.citations?.length">
            <summary>引用明细（{{ bundle.research.citations.length }} 条）</summary>
            <div class="cite-row" v-for="(c, i) in bundle.research.citations" :key="'rc' + i">
              <span class="cite-id mono">[{{ c.id || i + 1 }}]</span>
              <div>
                <p class="cite-quote">{{ c.quote }}</p>
                <p class="cite-quote zh" v-if="c.quote_zh">{{ c.quote_zh }}</p>
                <p class="dim tiny">{{ [c.broker, c.reportTitle || c.title, c.reportDate || c.date].filter(Boolean).join(' · ') }}
                  <span v-if="c.page">· p{{ c.page }}</span></p>
              </div>
            </div>
          </details>
          <p class="hint tiny">研报是原文证据口径，和 AI 决策口径分开；完整合并在下方「个股多空」面板。</p>
        </template>
        <div v-else class="empty tiny">{{ bundle.research?.reason || '暂无研报稿' }}</div>
      </template>

      <!-- 新闻：公开来源证据；没有时给外站搜索 -->
      <template v-else-if="tab === 'news'">
        <ul class="news" v-if="(bundle.news?.rows || []).length">
          <li v-for="(n, i) in bundle.news.rows" :key="'n' + i">
            <span class="mono dim tiny">{{ n.date || '—' }}</span>
            <a v-if="n.url" :href="n.url" target="_blank" rel="noopener">{{ n.title || n.url }}</a>
            <span v-else>{{ n.title || '—' }}</span>
            <span class="tag tiny" :class="n.text_ok ? 'ok' : 'dim'">{{ n.text_ok ? '全文' : '摘要' }}</span>
            <span class="dim tiny">{{ n.source || '' }}</span>
          </li>
        </ul>
        <div v-else class="empty tiny">{{ bundle.news?.reason || '暂无本地新闻证据' }}</div>
        <p class="hint tiny">
          来源：{{ bundle.news?.source || '公开来源' }}。本地没有独立新闻抓取器；没有证据时请用外站搜索，不伪造新闻。
        </p>
        <div class="pill-row">
          <a class="btn-ghost btn-sm" :href="bundle.links?.news_eastmoney" target="_blank" rel="noopener">东财新闻搜索 ↗</a>
          <a class="btn-ghost btn-sm" :href="bundle.links?.news_ths" target="_blank" rel="noopener">同花顺资讯 ↗</a>
          <a class="btn-ghost btn-sm" :href="bundle.links?.news_xueqiu" target="_blank" rel="noopener">雪球搜索 ↗</a>
        </div>
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
            <tr v-for="r in bundle.selections.rows" :key="r.decision_date + r.source"
                class="rec-row" title="点这一行 → K 线跳到该决策日"
                @click="openInKline(r.decision_date)">
              <td class="mono">
                <router-link class="link" :to="{ path: '/calendar', query: { date: r.decision_date } }"
                             :title="`去选股日历看 ${r.decision_date} 那天的全部选股`"
                             @click.stop>{{ r.decision_date }}</router-link>
              </td><td class="mono tiny">{{ r.source }}</td>
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
      <template v-else-if="tab === 'announcements'">
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
import { useRouter } from 'vue-router'
import type { Bar } from '@/components/ui/BarChart.vue'
import BarChart from '@/components/ui/BarChart.vue'
import StateBlock from '@/components/ui/StateBlock.vue'

const props = defineProps<{ code: string | null }>()
const bundle = ref<any>(null)
const router = useRouter()
/** 点选股记录的一行 → K 线页跳到该决策日（2026-10-15：日历 / 记录 / K 线三个视图互相可跳） */
function openInKline(day: string) {
  if (!day) return
  void router.push({ path: '/kline', query: { code: props.code, date: day } })
}
const error = ref('')
const tab = ref<'profile' | 'financials' | 'research' | 'news' | 'flow' | 'lhb' | 'selection' | 'announcements'>('profile')
const tabs = [
  { key: 'profile', label: '概况' },
  { key: 'financials', label: '财报' },
  { key: 'research', label: '研报' },
  { key: 'news', label: '新闻' },
  { key: 'flow', label: '资金' },
  { key: 'lhb', label: '龙虎榜' },
  { key: 'selection', label: '选股记录' },
  { key: 'announcements', label: '公告' },
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
tr.rec-row { cursor: pointer; }
tr.rec-row:hover { background: rgba(255, 255, 255, 0.05); }

.info-panel h6 { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.tabs.inline { margin-left: auto; margin-bottom: 0; }
.news { list-style: none; margin: 0; padding: 0; max-height: 320px; overflow: auto; }
.news li { display: flex; gap: var(--sp-2); padding: 4px 0; border-bottom: 1px solid var(--border-soft); font-size: var(--fs-sm); }
.news a { color: var(--accent-blue); text-decoration: none; }
.news a:hover { text-decoration: underline; }
.ls-summary { font-size: var(--fs-md); line-height: 1.65; margin: var(--sp-2) 0; }
.case-grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-3); margin: var(--sp-2) 0; }
.case { border: 1px solid var(--border-color); border-radius: var(--r-2); padding: var(--sp-2);
  background: rgba(255, 255, 255, 0.02); }
.case.bull > b { color: var(--accent-up); }
.case.bear > b { color: var(--accent-down); }
.case ul { margin: 4px 0 0; padding-left: 16px; }
.case li { font-size: var(--fs-sm); line-height: 1.6; margin: 3px 0; }
.risk { color: var(--accent-gold); font-size: var(--fs-sm); }
.cites { margin-top: var(--sp-2); }
.cite-row { display: flex; gap: var(--sp-2); padding: 6px 0; border-bottom: 1px dashed var(--border-color); }
.cite-id { color: var(--accent-gold); }
.cite-quote { font-size: var(--fs-sm); line-height: 1.55; margin: 0 0 2px; }
.cite-quote.zh { color: var(--text-muted); }
@media (max-width: 760px) { .case-grid { grid-template-columns: 1fr; } }
.gold { color: var(--accent-gold); }
.kv { margin: var(--sp-2) 0; }
</style>
