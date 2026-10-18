<template>
  <div class="page">
    <PageHeader
      title="选股结果"
      description="名单直接来自流水线产物（<code>output/results_cn/*.csv</code>），前端只做展示、不重算；下方「数据源」写明了这份名单取自哪个文件、什么时候生成的。"
    >
      <template #actions>
        <span v-if="loading" class="status running">加载中…</span>
        <button class="btn-ghost" :disabled="loading" @click="reload">刷新</button>
      </template>
    </PageHeader>

    <!-- 每日名单刷新契约：这份名单属于哪条策略、今天更新到哪个交易日，在这里点名 -->
    <StrategyListsStatusBar />

    <!-- ① 来源 + 出处指纹（页面第一屏）：以前页面只读港股那份 CSV，
         用户无从知道首页名单和命令行跑出来的名单根本不是一回事。 -->
    <div class="panel source-panel">
      <div class="source-bar">
        <div class="tabs">
          <button
            v-for="opt in data?.available || []"
            :key="opt.key"
            class="tab"
            :class="{ active: opt.key === currentSource }"
            :disabled="!opt.available"
            @click="switchSource(opt.key)"
          >
            {{ opt.label }}<span v-if="!opt.available" class="dim">（缺）</span>
            <span v-if="verdicts[opt.key]" class="tab-verdict" :class="verdicts[opt.key].tone"
                  :title="verdicts[opt.key].detail">{{ verdicts[opt.key].label }}</span>
          </button>
        </div>
      </div>
      <div v-if="data?.source" class="source-meta">
        <span class="pill">交易日 <b>{{ data.source.trade_date || '—' }}</b></span>
        <span class="pill">{{ rows.length }} 只（原始 {{ data.source.row_count }} 行）</span>
        <span class="pill" v-if="filterActive">筛选命中 {{ rows.length }}</span>
        <span v-for="f in data.source.files" :key="f.path" class="pill" :title="f.path">
          {{ f.role }} · {{ f.path.split('/').pop() }} · {{ f.mtime || '缺失' }}
        </span>
      </div>
      <p v-if="data?.source?.note" class="hint source-note">{{ data.source.note }}</p>
    </div>

    <StateBlock
      v-if="error"
      state="error"
      title="取数失败"
      :detail="error"
      :retry="reload"
    />
    <StateBlock v-else-if="loading && !data" state="loading" title="加载选股名单…" />
    <StateBlock
      v-else-if="data?.empty"
      state="empty"
      title="这个数据源还没有产物"
      detail="先跑一次流水线（`uv run python scripts/run_cn_pipeline.py --stage selection`），或切换到其它数据源。"
    />

    <template v-else-if="data">
      <!-- ② 名单（主区）+ 评分榜（侧栏）：名单是页面的主体，放在第一屏，导出与归因往后放 -->
      <div class="two-col">
        <div class="col-main">
          <h6>
            名单（{{ pagedRows.length }} / {{ rows.length }}）
            <span class="dim tiny">点代码/名称进 K 线，点 ★ 加自选（与 K 线页侧栏、选股日历同一份）</span>
          </h6>
          <div class="toolbar-row">
            <input v-model="keyword" class="search" placeholder="搜索 代码 / 名称 / 行业 / 阶段" />
            <button class="btn-ghost btn-sm" :class="{ 'wl-on': onlyWatchlist }"
                    @click="onlyWatchlist = !onlyWatchlist">
              ★ 只看自选 <span class="dim">{{ onlyWatchlist ? '(' + watchHits + ')' : watchlist.size.value }}</span>
            </button>
            <button class="btn-ghost btn-sm" v-if="filterActive" @click="clearFilters">清空</button>
          </div>
          <div class="table-scroll">
            <table class="data-table">
              <thead>
                <tr>
                  <th class="star-col">★</th>
                  <th v-for="col in data.columns" :key="col.key"
                      :class="{ 'text-left': col.key === 'stock_code' || col.key === 'stock_name' || col.key === 'stage' }"
                      @click="sortBy(col.key)">
                    {{ col.title }} {{ sortKey === col.key ? (sortAsc ? '▲' : '▼') : '' }}
                  </th>
                  <th class="ls-col">多空</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="(row, i) in pagedRows" :key="row.stock_code + '-' + i"
                  class="clickable"
                  :class="{ selected: selectedCode === row.stock_code, strong: row.in_portfolio === true || row.stage === '最终入选（PK 后）',
                            hl: highlightCode === row.stock_code }"
                  :ref="(el: any) => registerRow(row.stock_code, el)"
                  @click="selectedCode = row.stock_code"
                >
                  <td class="star-col">
                    <button class="star" :class="{ on: watchlist.has(row.stock_code) }"
                            :title="watchlist.has(row.stock_code) ? '从自选移除' : '加入自选'"
                            @click.stop="watchlist.toggle(row.stock_code, row.stock_name || '')">{{
                      watchlist.has(row.stock_code) ? '★' : '☆' }}</button>
                  </td>
                  <td v-for="col in data.columns" :key="col.key"
                      :class="{ 'text-left': col.key === 'stock_code' || col.key === 'stock_name' || col.key === 'stage' }">
                    <template v-if="col.key === 'stock_code'">
                      <span class="mono link" @click.stop="gotoStock(row.stock_code)">{{ row.stock_code }}</span>
                    </template>
                    <template v-else-if="col.key === 'stock_name'">
                      <span class="link" @click.stop="gotoStock(row.stock_code)">{{ row.stock_name || '—' }}</span>
                    </template>
                    <template v-else-if="col.key === 'stage'">
                      <span class="tag" :class="row.stage === '最终入选（PK 后）' ? 'gold' : ''">{{ row.stage }}</span>
                    </template>
                    <template v-else>{{ fmtVal(row[col.key]) }}</template>
                  </td>
                  <td class="ls-col">
                    <button class="btn-ghost btn-sm" title="看这只票的 AI 结论 / 研报证据"
                            @click.stop="longShortCode = row.stock_code">多空</button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <div class="pagination">
            <button :disabled="page <= 1" @click="page--">上一页</button>
            <span>{{ page }} / {{ totalPages }}</span>
            <button :disabled="page >= totalPages" @click="page++">下一页</button>
          </div>
        </div>

        <div class="col-side">
          <h6>{{ scoreKey ? '评分榜（' + scoreLabel + '）' : '评分榜' }}</h6>
          <BarChart v-if="scoreBars.length" :bars="scoreBars" :label-width="72" :max-height="420" />
          <div v-else class="empty">该数据源没有可用的评分列</div>
        </div>
      </div>

      <!-- ③ 导出到同花顺：同花顺**不开放写接口**（自选股在账号云端），
           所以这里做到"格式对齐 + 一键复制/下载"，人工在同花顺里点一次导入或 Ctrl+V 粘贴。
           默认折叠：它是"看完名单之后的动作"，不该占着第一屏。 -->
      <details class="panel collapse" v-if="ths?.available">
        <summary>导出到同花顺（自选股）· {{ ths.trade_date }}</summary>
        <p class="hint tiny">
          同花顺不开放写入接口，无法全自动同步。最快是<strong>点「复制」→ 在同花顺自选股里 Ctrl+V</strong>；
          或下载 txt 后走「自选股 → 右键 → 导入自选股」（乱码就换 GBK 版本）。
        </p>
        <div class="ths-grid">
          <div v-for="src in ths.sources" :key="src.key" class="ths-row">
            <div class="ths-head">
              <b>{{ src.label }}</b>
              <span class="tag" :class="src.key === 'all' ? 'gold' : ''">{{ src.n }} 只</span>
            </div>
            <div class="ths-codes mono tiny" :title="src.codes.join(' ')">
              {{ src.codes.join(' ') }}
            </div>
            <div class="ths-actions">
              <button class="btn btn-sm" @click="copyCodes(src)">
                {{ copiedKey === src.key ? '✓ 已复制' : '复制' }}
              </button>
              <a class="btn btn-sm" :href="thsDownloadUrl(`${ths.trade_date}_${src.key}_code.txt`)"
                 download>下载 txt</a>
              <a class="btn btn-sm" :href="thsDownloadUrl(`${ths.trade_date}_${src.key}_code_gbk.txt`)"
                 download>GBK</a>
              <a class="btn btn-sm" :href="thsDownloadUrl(`${ths.trade_date}_${src.key}_code_name.txt`)"
                 download>带名称</a>
              <a class="btn btn-sm" :href="thsDownloadUrl(`${ths.trade_date}_${src.key}.csv`)"
                 download>CSV</a>
            </div>
          </div>
        </div>
        <p class="tiny dim" v-for="(step, i) in ths.how_to_import || []" :key="i">· {{ step }}</p>
      </details>

      <!-- ④ 归因 / 风险：SHAP 与全局因子重要性（只港股链路有 factor_explanation） -->
      <hr>

      <!-- SHAP / 重要性只存在于港股链路的 factor_explanation 列；CN 名单没有这个字段。
           以前这里无条件渲染，切到 CN 后就是一片空白且没有任何解释。 -->
      <div v-if="!data.source?.supports_shap" class="state-block">
        <div class="state-title">本数据源没有 SHAP / 因子重要性</div>
        <div class="state-detail">这两个分析读的是港股链路的 <code>factor_explanation</code> 字段。
          切到「港股 LightGBM Top10」即可查看。</div>
        <div class="state-actions">
          <button class="btn btn-sm" @click="switchSource('hk_lightgbm')">切到港股源</button>
        </div>
      </div>

      <template v-else>
        <div v-if="importanceData.importances.length > 0">
          <h6>全局因子重要性（Top 20，共 {{ importanceData.feature_count }} 个因子）</h6>
          <BarChart :bars="importanceBars" :label-width="110" :max-height="480" />
        </div>
        <hr>
        <div>
          <h6>SHAP 因子贡献（单股）</h6>
          <select v-model="shapCode" class="stock-select" @change="loadShap">
            <option v-for="r in rows" :key="r.stock_code" :value="r.stock_code">
              {{ r.stock_code }} {{ r.stock_name }}
            </option>
          </select>
          <BarChart v-if="shapBars.length" :bars="shapBars" :label-width="140" />
          <div v-else class="empty">{{ shapCode ? '该股票没有 SHAP 明细' : '先选一只股票' }}</div>
        </div>
      </template>
    </template>
  </div>
    <div v-if="longShortCode" class="ls-mask" @click.self="longShortCode = null">
      <aside class="ls-drawer">
        <header class="ls-head">
          <b class="mono">{{ longShortCode }}</b>
          <span class="dim tiny">AI 结论 + 研报证据（两个口径）</span>
          <button class="btn-ghost btn-sm" @click="longShortCode = null">关闭</button>
        </header>
        <StockLongShortPanel :code="longShortCode" />
      </aside>
    </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { usePoolVerdicts } from '@/composables/usePoolVerdicts'
import { getImportance, getSelection, getShap, getThsExportFiles, thsDownloadUrl } from '@/api/client'
import StockLongShortPanel from '@/components/StockLongShortPanel.vue'
import { useAsync } from '@/composables/useAsync'
import { useWatchlist } from '@/composables/useWatchlist'
import type { ImportanceResponse, SelectionRow, ShapFeature, ThsExportList } from '@/types/api'
import PageHeader from '@/components/ui/PageHeader.vue'
import StateBlock from '@/components/ui/StateBlock.vue'
import BarChart, { type Bar } from '@/components/ui/BarChart.vue'
import StrategyListsStatusBar from '@/components/StrategyListsStatusBar.vue'

const router = useRouter()
const route = useRoute()
const watchlist = useWatchlist()
// 留空 = 后端默认（CN 生产名单）。支持 `/#/?source=cn_arm_concept` 深链：
// 其它页面（如行业热度页的"概念/板块手臂名单 →"）可以直接把人送到这个来源上，
// 不用再让用户自己去来源 tab 里找。
const currentSource = ref(String(route.query.source || ''))
// ?code=：从 K 线自选 / 日历跳进来时高亮并滚动到那一行（2026-10-15：把"自选 → 审查"这条单行道补上）
const highlightCode = ref(String(route.query.code || ''))
const rowEls = new Map<string, HTMLElement>()
function registerRow(code: string, el: any) {
  const node = el?.$el instanceof HTMLElement ? el.$el as HTMLElement : (el instanceof HTMLElement ? el : null)
  if (node) rowEls.set(code, node)
}
// ⚠️ 必须挂载后再注册：`pagedRows` 在这个位置还没定义（TDZ）⇒ 直接 watch 会在 setup 阶段抛错，
// 整页渲染不出来（实测：/selection?source=...&code=... 30 秒等不到表格）。
onMounted(() => {
  watch([highlightCode, () => pagedRows.value.length], () => {
    const el = rowEls.get(highlightCode.value)
    if (el) el.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }, { immediate: false })
  watch(() => route.query.code, (v) => { highlightCode.value = String(v || '') })
})
// 来源 tab 的方向徽章（正期望/负期望/样本不足）：判定来自策略池，同一套规则
const { verdicts, load: loadVerdicts } = usePoolVerdicts()
onMounted(() => { void loadVerdicts() })

const page = ref(1)
const perPage = 20
const sortKey = ref('')
const sortAsc = ref(false)
const selectedCode = ref<string | null>(null)
const longShortCode = ref<string | null>(null)
/** 名单筛选：关键字（代码/名称/行业/阶段）+ 只看自选（与 K 线页侧栏同一份自选） */
const keyword = ref('')
const onlyWatchlist = ref(false)

const { data, error, loading, reload } = useAsync(() => getSelection(currentSource.value))

// 同花顺导出：每个源一行，可一键复制代码（粘贴进同花顺自选股即建分组）
const ths = ref<ThsExportList | null>(null)
const copiedKey = ref('')
async function copyCodes(src: { key: string; codes: string[] }) {
  const text = src.codes.join('\n')
  try {
    await navigator.clipboard.writeText(text)
    copiedKey.value = src.key
    setTimeout(() => { if (copiedKey.value === src.key) copiedKey.value = '' }, 1600)
  } catch {
    // http 明文下 clipboard API 可能被拒 —— 退回到"选中文本让用户自己复制"
    window.prompt('自动复制被浏览器拒绝，请手动复制下面这段：', text)
  }
}
void (async () => {
  try { ths.value = await getThsExportFiles() } catch { /* 没有导出产物就不显示这一块 */ }
})()

function switchSource(key: string) {
  if (key === currentSource.value) return
  currentSource.value = key
  // 同步到 URL：刷新/分享后仍停在同一来源
  void router.replace({ query: key ? { ...route.query, source: key } : { ...route.query, source: undefined } })
  page.value = 1
  sortKey.value = ''
  shapCode.value = ''
  shapFeatures.value = []
  importanceData.value = { factor_set: '', importances: [], feature_count: 0, train_rows: 0 }
  void reload()
}

function gotoStock(code: string) {
  if (code) router.push({ path: '/kline', query: { code } })
}

const rows = computed<SelectionRow[]>(() => {
  let list = [...(data.value?.rows ?? [])]
  const kw = keyword.value.trim().toLowerCase()
  if (kw) {
    list = list.filter((row) => Object.values(row).some(
      (v) => v !== null && v !== undefined && String(v).toLowerCase().includes(kw)))
  }
  if (onlyWatchlist.value) list = list.filter((row) => watchlist.has(String(row.stock_code)))
  if (!sortKey.value) return list
  const key = sortKey.value
  return list.sort((a, b) => {
    const va = a[key], vb = b[key]
    if (va == null) return 1
    if (vb == null) return -1
    const cmp = typeof va === 'number' && typeof vb === 'number' ? va - vb : String(va).localeCompare(String(vb))
    return sortAsc.value ? cmp : -cmp
  })
})

const filterActive = computed(() => keyword.value.trim() !== '' || onlyWatchlist.value)
const watchHits = computed(() => (data.value?.rows ?? [])
  .filter((row) => watchlist.has(String(row.stock_code))).length)

function clearFilters() {
  keyword.value = ''
  onlyWatchlist.value = false
}

// 名单里出现过的名称顺手回填自选缓存（日历/行业页只有代码时也能显示名字）
watch(rows, (list) => {
  if (list.length) {
    watchlist.rememberMany(list.map((row) => ({ code: String(row.stock_code), name: String(row.stock_name || '') })))
  }
}, { immediate: false })

const totalPages = computed(() => Math.max(1, Math.ceil(rows.value.length / perPage)))
const pagedRows = computed(() => rows.value.slice((page.value - 1) * perPage, page.value * perPage))

function sortBy(key: string) {
  if (sortKey.value === key) sortAsc.value = !sortAsc.value
  else { sortKey.value = key; sortAsc.value = false }
  page.value = 1
}

// 评分列随数据源变化：CN 用 model_score，手臂用 score，港股用 ranking_score
const SCORE_KEYS = ['model_score', 'ensemble_score', 'score', 'ranking_score']
const scoreKey = computed(() => SCORE_KEYS.find((k) => (data.value?.rows ?? []).some((r) => r[k] != null)) ?? '')
const scoreLabel = computed(() => data.value?.columns.find((c) => c.key === scoreKey.value)?.title ?? '')

const scoreBars = computed<Bar[]>(() => {
  const key = scoreKey.value
  if (!key) return []
  const picks = (data.value?.rows ?? []).filter((r) => typeof r[key] === 'number')
  if (!picks.length) return []
  const sorted = [...picks].sort((a, b) => (b[key] as number) - (a[key] as number)).slice(0, 20)
  const max = Math.max(...sorted.map((r) => Math.abs(r[key] as number)), 1e-9)
  return sorted.map((r) => {
    const v = r[key] as number
    return {
      key: r.stock_code,
      label: `${r.stock_name || r.stock_code}`.slice(0, 6),
      value: v,
      text: v.toFixed(v > 10 ? 2 : 4),
      tone: (r.in_portfolio === true || r.stage === '最终入选（PK 后）') ? 'pos' : 'neutral',
      offset: 0,
    }
  })
})

// ── SHAP / 重要性（仅港股源） ──
const shapCode = ref('')
const shapFeatures = ref<(ShapFeature & { pct: number; offset: number })[]>([])
const importanceData = ref<ImportanceResponse>({ factor_set: '', importances: [], feature_count: 0, train_rows: 0 })

async function loadShap() {
  if (!shapCode.value) return
  try {
    const res = await getShap(shapCode.value)
    const maxAbs = Math.max(...res.features.map((f) => Math.abs(f.value)), 1e-9)
    shapFeatures.value = res.features.map((f) => {
      const pct = (Math.abs(f.value) / maxAbs) * 80
      return { ...f, pct, offset: f.direction === 'negative' ? 95 - pct : 5 }
    })
  } catch {
    shapFeatures.value = []
  }
}

const shapBars = computed<Bar[]>(() =>
  shapFeatures.value.map((f) => ({
    label: f.name,
    value: f.value,
    text: `${f.value > 0 ? '+' : ''}${f.value.toFixed(4)}`,
    tone: f.direction === 'positive' ? 'pos' : 'neg',
    offset: f.offset,
  })),
)

const importanceBars = computed<Bar[]>(() =>
  importanceData.value.importances.slice(0, 20).map((f) => ({
    label: f.factor,
    value: f.importance,
    text: f.importance.toFixed(4),
    tone: 'info',
  })),
)

// 只在支持 SHAP 的数据源上取重要性（CN 名单没有 factor_explanation，取了必然为空）
watch(
  () => data.value?.source?.supports_shap,
  async (supported) => {
    if (!supported || importanceData.value.importances.length) return
    try {
      importanceData.value = await getImportance()
    } catch {
      /* 取不到就留空；页面已按 supports_shap 分支处理 */
    }
  },
  { immediate: true },
)

function fmtVal(v: unknown): string {
  if (v === null || v === undefined || v === '') return '—'
  if (typeof v === 'boolean') return v ? '是' : '否'
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(4)
  return String(v)
}
</script>

<style scoped>
/* ?code= 深链进来的那一行（自选 → 选股结果） */
tr.hl { background: rgba(250, 204, 21, 0.12); outline: 1px solid rgba(250, 204, 21, 0.45); }
.go-to-selection { opacity: 0.75; }

/* 页面专属：数据源条 + 主表/评分榜两列。其余全部走设计系统。 */
.source-bar { display: flex; align-items: flex-start; gap: var(--sp-4); flex-wrap: wrap; }
.source-bar .tabs { margin-bottom: 0; }
.tab-verdict { margin-left: 5px; font-size: 10px; padding: 0 5px; border-radius: var(--r-pill);
  border: 1px solid var(--border-color); }
.tab-verdict.ok { color: var(--state-ok); border-color: var(--state-ok); }
.tab-verdict.warn { color: var(--state-warn); border-color: var(--state-warn); }
.tab-verdict.bad { color: var(--state-bad); border-color: var(--state-bad); }
.tab-verdict.dim { color: var(--text-dim); }
.source-meta { display: flex; flex-wrap: wrap; gap: 6px; margin-left: auto; }
.source-note { margin: var(--sp-2) 0 0; }
.stock-select {
  margin-bottom: var(--sp-3); padding: 6px 10px; background: var(--bg-input);
  color: var(--text-primary); border: 1px solid var(--border-color);
  border-radius: var(--r-2); max-width: 320px;
}
.data-table tr.strong td { box-shadow: inset 2px 0 0 var(--accent-up); }
.ths-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: var(--sp-2); }
.ths-row { background: var(--bg-input); border: 1px solid var(--border-color);
  border-radius: var(--r-3); padding: var(--sp-2) var(--sp-3); }
.ths-head { display: flex; align-items: center; gap: var(--sp-2); margin-bottom: 4px; }
.ths-codes { color: var(--text-muted); line-height: 1.6; margin-bottom: 6px;
  max-height: 3.2em; overflow: hidden; word-break: break-all; }
.ths-actions { display: flex; gap: 6px; flex-wrap: wrap; }
.ths-actions .btn { text-decoration: none; display: inline-flex; align-items: center; }

/* ── 排布优化（2026-10-10）───────────────────────────────────────────
   ① 来源与出处合并成一块"第一屏"信息条（原来来源 tab 与出处 pill 分散在两行）；
   ② 名单加工具条（搜索 / 只看自选 / 清空）+ ★ 列，与 K 线页自选共用一份状态；
   ③ 同花顺导出改成折叠块 —— 它是"看完名单之后的动作"，不该占第一屏。 */
.source-panel .source-meta { margin-left: 0; margin-top: var(--sp-2); }
.toolbar-row { display: flex; gap: var(--sp-2); align-items: center; flex-wrap: wrap;
  margin-bottom: var(--sp-2); }
.search { flex: 1; min-width: 180px; max-width: 320px; padding: 5px 8px;
  background: var(--bg-input); border: 1px solid var(--border-color);
  border-radius: var(--r-2); color: var(--text-primary); }
.btn-ghost.wl-on { color: var(--accent-gold); border-color: var(--accent-gold);
  background: rgba(255, 209, 102, 0.08); }
.star-col { width: 30px; text-align: center; }
.star { background: none; border: none; cursor: pointer; font-size: 13px; line-height: 1;
  color: var(--text-dim); padding: 0 2px; }
.star.on, .star:hover { color: var(--accent-gold); }
.collapse summary { cursor: pointer; font-size: var(--fs-md); color: var(--text-muted);
  padding: 2px 0; }
.collapse summary:hover { color: var(--text-primary); }
.collapse[open] summary { margin-bottom: var(--sp-2); }
.ls-col { width: 56px; text-align: center; }
.ls-mask { position: fixed; inset: 0; z-index: 80; display: flex; justify-content: flex-end;
  background: rgba(0, 0, 0, 0.45); }
.ls-drawer { width: min(820px, 94vw); height: 100%; overflow-y: auto; padding: var(--sp-3);
  background: var(--bg-panel, #141414); border-left: 1px solid var(--border-color); }
.ls-head { display: flex; align-items: center; gap: var(--sp-2); margin-bottom: var(--sp-2); }
.ls-head .btn-ghost { margin-left: auto; }
</style>
