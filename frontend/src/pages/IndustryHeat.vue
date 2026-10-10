<template>
  <div class="page">
    <PageHeader
      title="行业热度"
      description="分行业热度排名（每日 / 每周 / 每月）。热度指数 = 5 个分项在行业之间的横截面 z 加权后取分位（0~100）：区间涨跌幅 0.30 · 上涨家数占比 0.15 · 净流入÷成交额 0.25 · 涨停密度 0.20 · 放量程度 0.10。热度只描述「最近在不在风口上」，不是未来收益预测。"
    >
      <template #actions>
        <div class="seg">
          <button class="seg-btn" :class="{ active: view === 'grid' }" @click="view = 'grid'">热度网格</button>
          <button class="seg-btn" :class="{ active: view === 'map' }" @click="view = 'map'">股票热力图</button>
        </div>
        <select v-model="period" class="picker">
          <option v-for="p in overview?.periods || []" :key="p.key" :value="p.key">{{ p.label }}</option>
        </select>
        <select v-model.number="minN" class="picker">
          <option :value="0">全部行业</option>
          <option :value="5">只看 ≥5 只</option>
          <option :value="10">只看 ≥10 只</option>
          <option :value="20">只看 ≥20 只</option>
        </select>
        <select v-model="market" class="picker" v-if="view === 'map'">
          <option value="">全市场</option>
          <option v-for="m in MARKETS" :key="m" :value="m">{{ m }}</option>
        </select>
        <button class="btn-ghost" :disabled="loading" @click="reload()">刷新</button>
        <a class="btn-ghost link-btn" href="#/?source=cn_arm_concept">概念/板块手臂名单 →</a>
      </template>
    </PageHeader>

    <StateBlock v-if="error" state="error" title="取不到行业热度" :detail="error" :retry="() => reload()" />
    <StateBlock v-else-if="!heat" state="loading" title="加载行业热度…" />
    <StateBlock v-else-if="!heat.available" state="empty" title="没有可用的行业热度"
                :detail="heat.hint || '本地日线/行业映射为空'" />

    <template v-else>
      <div class="panel">
        <div class="pill-row">
          <span class="pill">数据日 <b>{{ heat.market.date }}</b></span>
          <span class="pill" v-if="heat.market.span > 1">
            区间 <b>{{ heat.market.base_date }} → {{ heat.market.date }}</b>
            （{{ heat.market.span }} 个交易日）
          </span>
          <span class="pill">全市场 <b :class="tone(heat.market.ret)">{{ pct(heat.market.ret) }}</b></span>
          <span class="pill">上涨占比 <b>{{ (heat.market.up_ratio * 100).toFixed(1) }}%</b></span>
          <span class="pill">涨停 <b class="up">{{ heat.market.zt_days }}</b> 次</span>
          <span class="pill">净流入 <b :class="tone(heat.market.net_amount_wan)">{{ fmt(yi(heat.market.net_amount_wan), 1) }} 亿</b></span>
          <span class="pill dim tiny">{{ heat.n_total }} 个行业 · {{ heat.market.n_stocks }} 只成分</span>
          <span class="pill dim tiny" v-if="heat.market.n_unclassified">未分类 {{ heat.market.n_unclassified }} 只（不参与排名）</span>
        </div>
      </div>

      <!-- 左侧 = 视图（网格 / 热力图），右侧 = 选中行业的成分股（自动列出） -->
      <div class="two-col">
        <div class="col-main">
          <div class="panel" v-if="view === 'map'">
            <h6>
              股票热力图
              <span class="dim tiny">面积 = 流通市值 · 颜色 = {{ heat.period_label }}涨跌</span>
              <a class="btn-ghost btn-sm link-btn" href="#/rankings">涨跌/资金排行 →</a>
            </h6>
            <StateBlock v-if="heatmapError" state="error" title="取不到热力图" :detail="heatmapError"
                        :retry="() => loadHeatmap()" />
            <div v-else-if="!heatmap" class="loading-mini">加载热力图…</div>
            <IndustryHeatmap
              v-else
              :industries="heatmap.industries"
              :period="period"
              :period-label="heat.period_label"
              :selected="selected"
              :height="620"
              @select="selectIndustry"
              @open="gotoKline"
            />
            <div class="pill-row mt" v-if="heatmap">
              <span class="pill dim tiny">{{ heatmap.totals.n_industries }} 个行业 · {{ heatmap.totals.n_stocks }} 只个股</span>
              <span class="pill">合计流通市值 <b>{{ fmt(heatmap.totals.mktcap_yi, 0) }}</b> 亿</span>
              <span class="pill">上涨 <b class="up">{{ heatmap.totals.up }}</b> / 下跌 <b class="down">{{ heatmap.totals.down }}</b></span>
              <span class="pill dim tiny">市值快照 {{ heatmap.mktcap_date || '—' }}
                <template v-if="heatmap.mktcap_missing">（{{ heatmap.mktcap_missing }} 只缺市值，用中位数兜底）</template>
              </span>
            </div>
            <ul class="hint tiny" v-if="heatmap">
              <li v-for="n in heatmap.notes" :key="n">{{ n }}</li>
            </ul>
          </div>

          <div class="panel" v-else>
            <h6>热度网格（颜色越红越热，点击看行业内涨跌排名）</h6>
            <div class="heat-grid">
              <button v-for="r in heat.rows" :key="r.industry" class="heat-chip"
                      :class="{ active: r.industry === selected }"
                      :style="chipStyle(r.heat)" :title="`${r.industry}｜热度 ${r.heat}｜涨跌 ${pct(r.ret)}｜净流入 ${fmt(r.net_amount_yi, 1)} 亿`"
                      @click="selectIndustry(r.industry)">
                <span class="hc-name">{{ r.industry }}</span>
                <span class="hc-heat">{{ r.heat.toFixed(0) }}</span>
                <span class="hc-ret" :class="tone(r.ret)">{{ pct(r.ret) }}</span>
              </button>
            </div>
            <h6 class="mt">热度前 {{ Math.min(heat.rows.length, 20) }} 名</h6>
            <BarChart :bars="heatBars" :label-width="88" :max-height="380" />
          </div>
        </div>

        <!-- 右侧：选中行业的成分股列表。**自动列出**——进入页面就默认选中热度第 1 名，
             换行业时立即重取；点 ★ 加自选（与 K 线页侧栏、选股日历同一份存储）。 -->
        <div class="col-side">
          <div class="panel side-panel">
            <h6>
              <b>{{ selected || '—' }}</b> 成分股
              <span class="dim tiny">{{ stocks?.stats.n ?? 0 }} 只</span>
              <span class="tabs inline" v-if="selected">
                <button class="tab" :class="{ active: direction === 'up' }" @click="direction = 'up'">涨</button>
                <button class="tab" :class="{ active: direction === 'down' }" @click="direction = 'down'">跌</button>
              </span>
            </h6>
            <p class="hint tiny side-hint" v-if="selected">
              中位 {{ pct(stocks?.stats.ret) }} · 上涨占比
              {{ stocks ? (stocks.stats.up_ratio * 100).toFixed(0) : '—' }}% ·
              净流入 {{ fmt(stocks?.stats.net_amount_yi, 2) }} 亿
            </p>
            <StateBlock v-if="stocksError" state="error" title="取不到成分股" :detail="stocksError" />
            <div v-else-if="!stocks" class="loading-mini">加载中…</div>
            <ul v-else class="side-rows">
              <li v-for="s in stocks.rows.slice(0, 24)" :key="s.stock_code"
                  @click="gotoKline(s.stock_code)">
                <span class="sr-rank dim">{{ s.rank }}</span>
                <span class="sr-main">
                  <span class="sr-name">{{ s.name }}</span>
                  <span class="sr-code mono dim">{{ s.stock_code }}</span>
                </span>
                <span class="sr-ret mono" :class="tone(s.period_ret)">{{ pct(s.period_ret) }}</span>
                <span class="sr-tag">
                  <span v-if="s.boards >= 2" class="tag gold">{{ cn(s.boards) }}板</span>
                  <span v-else-if="s.is_limit_up" class="tag ok">首板</span>
                </span>
                <button class="star" :class="{ on: watchlist.has(s.stock_code) }"
                        :title="watchlist.has(s.stock_code) ? '从自选移除' : '加入自选'"
                        @click.stop="watchlist.toggle(s.stock_code, s.name)">
                  {{ watchlist.has(s.stock_code) ? '★' : '☆' }}
                </button>
              </li>
            </ul>
            <p class="hint tiny mt" v-if="stocks && stocks.rows.length > 24">
              只显示前 24 只，完整榜单见下方「行业内涨跌排名」。
            </p>
          </div>
        </div>
      </div>

      <!-- 行业内涨跌排名（完整列） -->
      <div class="panel" v-if="selected">
        <h6>
          <b>{{ selected }}</b> 行业内涨跌排名
          <span class="dim tiny" v-if="stocks">
            {{ stocks.period_label }} · {{ stocks.stats.n }} 只 · 中位 {{ pct(stocks.stats.ret) }} ·
            上涨占比 {{ (stocks.stats.up_ratio * 100).toFixed(0) }}% ·
            净流入 {{ fmt(stocks.stats.net_amount_yi, 2) }} 亿
          </span>
          <span class="tabs inline">
            <button class="tab" :class="{ active: direction === 'up' }" @click="direction = 'up'">涨幅榜</button>
            <button class="tab" :class="{ active: direction === 'down' }" @click="direction = 'down'">跌幅榜</button>
          </span>
          <button class="btn-ghost btn-sm close-btn" @click="selected = ''">收起</button>
        </h6>
        <StateBlock v-if="stocksError" state="error" title="取不到行业内个股" :detail="stocksError" />
        <StateBlock v-else-if="!stocks" state="loading" title="加载中…" />
        <div class="table-scroll" v-else>
          <table class="data-table">
            <thead>
              <tr>
                <th>#</th><th class="text-left">代码</th><th class="text-left">名称</th><th class="text-left">板块</th>
                <th>{{ stocks.period_label }}涨跌</th><th>当日</th><th>收盘</th><th>成交额</th>
                <th>净流入(亿)</th><th>连板</th><th>自选</th><th class="text-left">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="s in stocks.rows" :key="s.stock_code">
                <td>{{ s.rank }}</td>
                <td class="mono text-left link" @click="gotoKline(s.stock_code)">{{ s.stock_code }}</td>
                <td class="text-left">
                  <a class="link" @click="gotoKline(s.stock_code)">{{ s.name }}</a>
                  <span v-if="s.is_st" class="flag flag-bad">ST</span>
                </td>
                <td class="text-left tiny dim">{{ s.market || '—' }}</td>
                <td :class="tone(s.period_ret)"><b>{{ pct(s.period_ret) }}</b></td>
                <td :class="tone(s.pct_chg)">{{ pct(s.pct_chg) }}</td>
                <td>{{ fmt(s.close, 2) }}</td>
                <td class="dim">{{ money(s.amount) }}</td>
                <td :class="tone(s.net_amount_yi)">{{ fmt(s.net_amount_yi, 2) }}</td>
                <td>
                  <span v-if="s.boards >= 2" class="tag gold">{{ cn(s.boards) }}板</span>
                  <span v-else-if="s.is_limit_up" class="tag ok">首板</span>
                  <span v-else class="dim">—</span>
                </td>
                <td>
                  <button class="star" :class="{ on: watchlist.has(s.stock_code) }"
                          :title="watchlist.has(s.stock_code) ? '从自选移除' : '加入自选'"
                          @click="watchlist.toggle(s.stock_code, s.name)">
                    {{ watchlist.has(s.stock_code) ? '★' : '☆' }}
                  </button>
                </td>
                <td class="text-left"><a class="link tiny" @click="gotoIntraday(s.stock_code)">分时</a></td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- 全市场行业热度排名 -->
      <div class="panel">
        <h6>热度排名（{{ heat.period_label }}）</h6>
        <div class="table-scroll mt">
          <table class="data-table">
            <thead>
              <tr>
                <th>#</th><th class="text-left">行业</th><th>热度</th><th>Δ热度</th>
                <th>区间涨跌</th><th>上涨占比</th><th>净流入(亿)</th><th>流入强度</th>
                <th>涨停次数</th><th>连板</th><th>放量</th><th>成分</th><th class="text-left">领涨股</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in heat.rows" :key="r.industry" class="clickable"
                  :class="{ selected: r.industry === selected }" @click="selectIndustry(r.industry)">
                <td>{{ r.rank }}</td>
                <td class="text-left"><b>{{ r.industry }}</b></td>
                <td><span class="heat-badge" :style="chipStyle(r.heat)">{{ r.heat.toFixed(1) }}</span></td>
                <td :class="tone(r.heat_delta)">
                  {{ r.heat_delta === null ? '—' : (r.heat_delta > 0 ? '+' : '') + r.heat_delta.toFixed(1) }}
                </td>
                <td :class="tone(r.ret)"><b>{{ pct(r.ret) }}</b></td>
                <td>{{ (r.up_ratio * 100).toFixed(0) }}%</td>
                <td :class="tone(r.net_amount_yi)">{{ fmt(r.net_amount_yi, 2) }}</td>
                <td :class="tone(r.flow_intensity)">{{ (r.flow_intensity * 100).toFixed(2) }}%</td>
                <td>{{ r.zt_days }}</td>
                <td>{{ r.boards_ge2 || '—' }}</td>
                <td class="dim">{{ r.volume_boost.toFixed(2) }}×</td>
                <td class="dim">{{ r.n }}</td>
                <td class="text-left tiny dim">{{ r.leaders }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="hint tiny mt">
          Δ热度 = 与上一个同长度窗口的热度差（正=升温）；流入强度 = 净流入 ÷ 区间成交额；
          放量 = 本区间成交额 ÷ 上一同长度区间。涨停次数是"涨停股票日"累计（不是家数）。
        </p>
        <ul class="hint tiny">
          <li v-for="n in heat.notes" :key="n">{{ n }}</li>
        </ul>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { getIndustryHeat, getIndustryHeatmap, getIndustryOverview, getIndustryStocks } from '@/api/client'
import type {
  IndustryHeatResponse, IndustryHeatmapResponse, IndustryOverview, IndustryStocksResponse,
} from '@/types/api'
import { useWatchlist } from '@/composables/useWatchlist'
import PageHeader from '@/components/ui/PageHeader.vue'
import StateBlock from '@/components/ui/StateBlock.vue'
import BarChart, { type Bar } from '@/components/ui/BarChart.vue'
import IndustryHeatmap from '@/components/IndustryHeatmap.vue'

const MARKETS = ['主板', '创业板', '科创板', '北交所']

const router = useRouter()
const watchlist = useWatchlist()
const overview = ref<IndustryOverview | null>(null)
const heat = ref<IndustryHeatResponse | null>(null)
const stocks = ref<IndustryStocksResponse | null>(null)
const heatmap = ref<IndustryHeatmapResponse | null>(null)
const heatmapError = ref('')
const stocksError = ref('')
const error = ref('')
const loading = ref(false)
const period = ref<'daily' | 'weekly' | 'monthly'>('daily')
const minN = ref(0)
const view = ref<'grid' | 'map'>('grid')
const market = ref('')
const direction = ref<'up' | 'down'>('up')
const selected = ref('')

async function reload() {
  loading.value = true
  try {
    if (!overview.value) overview.value = await getIndustryOverview()
    heat.value = await getIndustryHeat(period.value, 120, minN.value)
    error.value = ''
    // 自动列出：进入页面 / 换周期 / 当前选中行业消失时，默认选热度第 1 名
    if (!selected.value || !heat.value.rows.some((r) => r.industry === selected.value)) {
      selected.value = heat.value.rows[0]?.industry || ''
      stocks.value = null
    }
    if (selected.value) await loadStocks()
    if (view.value === 'map') await loadHeatmap()
  } catch (e) {
    error.value = String(e)
  } finally {
    loading.value = false
  }
}

async function loadHeatmap() {
  heatmapError.value = ''
  try {
    heatmap.value = await getIndustryHeatmap(period.value, market.value)
  } catch (e) {
    heatmapError.value = String(e)
    heatmap.value = null
  }
}

async function loadStocks() {
  if (!selected.value) { stocks.value = null; return }
  stocksError.value = ''
  try {
    stocks.value = await getIndustryStocks(selected.value, period.value, direction.value, 50)
    watchlist.rememberMany(stocks.value.rows.map((r) => ({ code: r.stock_code, name: r.name })))
  } catch (e) {
    stocksError.value = String(e)
    stocks.value = null
  }
}

/** 选中行业（网格 / 热力图 / 排名表三处共用同一个入口） */
async function selectIndustry(name: string) {
  if (selected.value === name && stocks.value) return
  selected.value = name
  stocks.value = null
  await loadStocks()
}

onMounted(reload)
watch([period, minN], () => { void reload() })
watch(market, () => { if (view.value === 'map') void loadHeatmap() })
watch(view, (v) => { if (v === 'map' && !heatmap.value) void loadHeatmap() })
watch(direction, () => { void loadStocks() })

const heatBars = computed<Bar[]>(() =>
  (heat.value?.rows || []).slice(0, 20).map((r) => ({
    label: r.industry, value: r.heat,
    text: `${r.heat.toFixed(0)}｜${pct(r.ret)}`,
    tone: 'pos' as const,
  })),
)

/** 热度 0~100 → 红-绿渐变（A 股口径：红=热）。 */
function chipStyle(value: number) {
  const ratio = Math.max(0, Math.min(1, value / 100))
  const alpha = 0.10 + ratio * 0.55
  const color = value >= 50 ? `rgba(239, 71, 111, ${alpha})` : `rgba(0, 212, 170, ${alpha})`
  return { background: color, borderColor: value >= 50 ? 'rgba(239,71,111,0.55)' : 'rgba(0,212,170,0.45)' }
}

function gotoKline(code: string) { void router.push({ path: '/kline', query: { code } }) }
function gotoIntraday(code: string) { void router.push({ path: '/intraday', query: { code } }) }

const fmt = (v: number | null | undefined, nd = 2) =>
  v === null || v === undefined || Number.isNaN(Number(v)) ? '—' : Number(v).toFixed(nd)
const pct = (v: number | null | undefined) =>
  v === null || v === undefined || Number.isNaN(Number(v)) ? '—' : `${Number(v) >= 0 ? '+' : ''}${(Number(v) * 100).toFixed(2)}%`
const tone = (v: number | null | undefined) =>
  v === null || v === undefined || Number.isNaN(Number(v)) ? '' : Number(v) >= 0 ? 'up' : 'down'
const yi = (wan: number | null | undefined) => (wan === null || wan === undefined ? null : wan / 1e4)
function money(yuan: number | null | undefined) {
  if (yuan === null || yuan === undefined) return '—'
  if (yuan >= 1e8) return (yuan / 1e8).toFixed(1) + ' 亿'
  if (yuan >= 1e4) return (yuan / 1e4).toFixed(0) + ' 万'
  return yuan.toFixed(0)
}
const CN_NUM = ['零', '一', '两', '三', '四', '五', '六', '七', '八', '九', '十']
const cn = (n: number) => CN_NUM[n] || String(n)
</script>

<style scoped>
.picker { padding: 5px 10px; background: var(--bg-input); color: var(--text-primary);
  border: 1px solid var(--border-color); border-radius: var(--r-2); }
.seg { display: inline-flex; border: 1px solid var(--border-color); border-radius: var(--r-2); overflow: hidden; }
.seg-btn { padding: 5px 10px; background: var(--bg-input); color: var(--text-muted);
  border: 0; cursor: pointer; font-size: var(--fs-sm); }
.seg-btn.active { background: rgba(255, 209, 102, 0.12); color: var(--accent-gold); font-weight: 600; }
.mt { margin-top: var(--sp-3); }
.heat-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 6px; margin: var(--sp-2) 0 var(--sp-3); }
.heat-chip {
  display: flex; flex-direction: column; align-items: flex-start; gap: 1px;
  padding: 5px 8px; border-radius: var(--r-2); border: 1px solid var(--border-color);
  background: var(--bg-input); cursor: pointer; color: var(--text-primary);
  font-size: var(--fs-sm); text-align: left; transition: transform 0.1s;
}
.heat-chip:hover { transform: translateY(-1px); }
.heat-chip.active { outline: 2px solid var(--accent-gold); outline-offset: 1px; }
.hc-name { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%; }
.hc-heat { font-family: var(--font-mono); font-size: var(--fs-xs); color: var(--text-muted); }
.hc-ret { font-family: var(--font-mono); font-size: var(--fs-xs); }
.heat-badge { display: inline-block; min-width: 42px; padding: 1px 6px; border-radius: var(--r-pill);
  border: 1px solid var(--border-color); font-family: var(--font-mono); }
.tabs.inline { display: inline-flex; gap: 4px; margin-left: var(--sp-2); margin-bottom: 0; }
.close-btn { margin-left: auto; }
.link-btn { text-decoration: none; display: inline-flex; align-items: center; }
.panel h6 { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.loading-mini { padding: var(--sp-4); color: var(--text-muted); font-size: var(--fs-sm); }

/* 右侧成分股速览 */
.side-panel { position: sticky; top: var(--sp-3); }
.side-hint { margin-bottom: var(--sp-2); }
.side-rows { list-style: none; margin: 0; padding: 0; max-height: 62vh; overflow: auto; }
.side-rows li { display: flex; align-items: center; gap: 6px; padding: 3px 4px; border-radius: var(--r-2);
  cursor: pointer; font-size: var(--fs-sm); }
.side-rows li:hover { background: rgba(255, 255, 255, 0.04); }
.sr-rank { width: 20px; text-align: right; font-family: var(--font-mono); font-size: var(--fs-xs); }
.sr-main { flex: 1; min-width: 0; display: flex; flex-direction: column; line-height: 1.25; }
.sr-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sr-code { font-size: 10px; }
.sr-ret { font-size: var(--fs-sm); }
.sr-tag { width: 34px; text-align: right; }
.star { background: none; border: none; color: var(--text-dim); cursor: pointer; font-size: var(--fs-md); padding: 0 2px; }
.star.on { color: var(--accent-gold); }
.star:hover { color: var(--accent-gold); }
@media (max-width: 1100px) { .side-panel { position: static; } }
</style>
