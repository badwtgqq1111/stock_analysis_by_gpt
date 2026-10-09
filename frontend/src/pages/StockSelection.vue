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

    <!-- 数据源选择 + 出处指纹：这是本轮补上的最关键的一块 —— 以前页面只读港股那份 CSV，
         用户无从知道首页名单和命令行跑出来的名单根本不是一回事。 -->
    <div class="panel">
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
          </button>
        </div>
        <div v-if="data?.source" class="source-meta">
          <span class="pill">交易日 <b>{{ data.source.trade_date || '—' }}</b></span>
          <span class="pill">{{ data.source.row_count }} 行</span>
          <span v-for="f in data.source.files" :key="f.path" class="pill" :title="f.path">
            {{ f.role }} · {{ f.path.split('/').pop() }} · {{ f.mtime || '缺失' }}
          </span>
        </div>
      </div>
      <p v-if="data?.source?.note" class="hint source-note">{{ data.source.note }}</p>
    </div>

    <!-- 导出到同花顺：同花顺**不开放写接口**（自选股在账号云端），
         所以这里做到"格式对齐 + 一键复制/下载"，人工在同花顺里点一次导入或 Ctrl+V 粘贴。 -->
    <div class="panel" v-if="ths?.available">
      <h6>导出到同花顺（自选股）· {{ ths.trade_date }}</h6>
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
      <div class="two-col">
        <div class="col-main">
          <h6>名单（{{ pagedRows.length }} / {{ rows.length }}）</h6>
          <div class="table-scroll">
            <table class="data-table">
              <thead>
                <tr>
                  <th v-for="col in data.columns" :key="col.key"
                      :class="{ 'text-left': col.key === 'stock_code' || col.key === 'stock_name' || col.key === 'stage' }"
                      @click="sortBy(col.key)">
                    {{ col.title }} {{ sortKey === col.key ? (sortAsc ? '▲' : '▼') : '' }}
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="(row, i) in pagedRows" :key="row.stock_code + '-' + i"
                  class="clickable"
                  :class="{ selected: selectedCode === row.stock_code, strong: row.in_portfolio === true || row.stage === '最终入选（PK 后）' }"
                  @click="selectedCode = row.stock_code"
                >
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
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { getImportance, getSelection, getShap, getThsExportFiles, thsDownloadUrl } from '@/api/client'
import { useAsync } from '@/composables/useAsync'
import type { ImportanceResponse, SelectionRow, ShapFeature, ThsExportList } from '@/types/api'
import PageHeader from '@/components/ui/PageHeader.vue'
import StateBlock from '@/components/ui/StateBlock.vue'
import BarChart, { type Bar } from '@/components/ui/BarChart.vue'

const router = useRouter()
const currentSource = ref('')   // 留空 = 后端默认（CN 生产名单）
const page = ref(1)
const perPage = 20
const sortKey = ref('')
const sortAsc = ref(false)
const selectedCode = ref<string | null>(null)

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
  const list = [...(data.value?.rows ?? [])]
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
/* 页面专属：数据源条 + 主表/评分榜两列。其余全部走设计系统。 */
.source-bar { display: flex; align-items: flex-start; gap: var(--sp-4); flex-wrap: wrap; }
.source-bar .tabs { margin-bottom: 0; }
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
</style>
