<template>
  <div class="page">
    <h4 class="page-title">CN 数据流水线 · preselect / PK</h4>
    <p class="hint">
      数据全部来自流水线自己的产物（<code>output/results_cn/*.csv</code>、<code>output/pipeline_reports/*.json</code>），
      前端只做展示，不重算 —— 与命令行跑出来的一致。
    </p>

    <div class="toolbar">
      <button class="btn-primary" @click="refreshAll">刷新</button>
      <span class="status" :class="loading ? 'running' : 'done'">{{ loading ? 'loading' : 'ready' }}</span>
      <span v-if="overview?.latest_report?.file" class="meta">
        最近一次流水线：{{ overview.latest_report.started_at }} ·
        <b :class="overview.latest_report.result === 'ok' ? 'ok' : 'bad'">{{ overview.latest_report.result }}</b>
        · {{ overview.latest_report.file }}
      </span>
    </div>

    <div class="panel">
      <h6>阶段概览（configured = 在 <code>{{ overview?.config }}</code> 的 stages 里）</h6>
      <div class="stage-grid">
        <div v-for="s in visibleStages" :key="s.stage" class="stage-card"
             :class="{ configured: s.configured, warn: s.overwrites_results }">
          <div class="stage-head">
            <span class="stage-name">{{ s.stage }}</span>
            <span v-if="s.configured" class="tag cfg">configured</span>
            <span v-if="s.overwrites_results" class="tag warn">覆盖 results_cn</span>
            <span v-if="s.last_status" class="tag" :class="s.last_status">{{ s.last_status }}</span>
          </div>
          <div v-if="!s.artifacts.length" class="empty tiny">（未登记产物）</div>
          <div v-for="a in s.artifacts" :key="a.path" class="artifact" :class="{ missing: !a.exists }">
            <span class="dot" :class="a.exists ? 'on' : 'off'"></span>
            <span class="apath" :title="a.path">{{ a.path }}</span>
            <span class="ameta">
              <template v-if="a.exists">
                {{ a.rows != null ? a.rows + ' 行' : (a.files != null ? a.files + ' 文件' : '') }}
                · {{ fmtBytes(a.bytes) }} · {{ a.mtime }}
              </template>
              <template v-else>缺失</template>
            </span>
          </div>
          <div v-if="s.last_detail" class="detail">{{ s.last_detail }}</div>
        </div>
      </div>
      <button class="btn-ghost tiny" @click="showAllStages = !showAllStages">
        {{ showAllStages ? '只看配置阶段' : '展开全部 ' + (overview?.stage_catalog.length || 0) + ' 个阶段' }}
      </button>
    </div>

    <div class="panel">
      <h6>preselect → PK 结果（{{ selection?.source_dir }}，交易日 {{ selection?.stats?.trade_date }}）</h6>
      <div class="pill-row">
        <span class="pill">候选池 <b>{{ selection?.stats?.preselected_count ?? '—' }}</b></span>
        <span class="pill">最终名单 <b>{{ selection?.stats?.selected_count ?? '—' }}</b></span>
        <span class="pill">进/出 <b class="ok">+{{ selection?.diff?.entered?.length ?? 0 }}</b> /
          <b class="bad">-{{ selection?.diff?.exited?.length ?? 0 }}</b></span>
        <span class="pill">渠道 {{ kv(selection?.stats?.by_channel) }}</span>
        <span class="pill">腿 {{ kv(selection?.stats?.by_sleeve) }}</span>
        <span class="pill">策略 {{ kv(selection?.stats?.by_strategy) }}</span>
        <span class="pill">约束 {{ kv(selection?.stats?.selected_by_constraint) }}</span>
        <span class="pill" v-if="selection?.stats?.model_score">
          model_score {{ selection.stats.model_score.min }} / {{ selection.stats.model_score.p50 }} / {{ selection.stats.model_score.max }}
        </span>
      </div>

      <div class="tabs">
        <button v-for="t in tabs" :key="t.key" class="tab" :class="{ active: tab === t.key }" @click="tab = t.key">
          {{ t.label }}
        </button>
      </div>

      <table v-if="tab === 'preselected'" class="data-table">
        <thead><tr><th>#</th><th>代码</th><th>名称</th><th>model_score</th><th>lightgbm</th><th>transformer</th>
          <th>渠道</th><th>腿</th><th>约束</th><th>目标权重</th><th>regime</th></tr></thead>
        <tbody>
          <tr v-for="(r, i) in selection?.preselected?.rows || []" :key="i">
            <td>{{ r.rank }}</td>
            <td class="mono link" @click="gotoStock(r.stock_code)">{{ r.stock_code }}</td>
            <td class="link" @click="gotoStock(r.stock_code)">{{ r.stock_name || '—' }}</td>
            <td>{{ num(r.model_score, 3) }}</td><td>{{ num(r.lightgbm_score, 2) }}</td><td>{{ num(r.transformer_score, 2) }}</td>
            <td>{{ r.selection_channel }}</td><td>{{ r.selection_sleeve }}</td><td>{{ r.constraint_status }}</td>
            <td>{{ num(r.target_weight, 3) }}</td><td>{{ r.regime }}</td>
          </tr>
        </tbody>
      </table>

      <table v-else-if="tab === 'selected'" class="data-table">
        <thead><tr><th>#</th><th>代码</th><th>名称</th><th>model_score</th><th>目标权重</th><th>市值(亿)</th>
          <th>成交额</th><th>约束</th><th>组合模式</th><th>行业</th></tr></thead>
        <tbody>
          <tr v-for="(r, i) in selection?.selected?.rows || []" :key="i">
            <td>{{ r.rank }}</td>
            <td class="mono link" @click="gotoStock(r.stock_code)">{{ r.stock_code }}</td>
            <td class="link" @click="gotoStock(r.stock_code)">{{ r.stock_name || '—' }}</td>
            <td>{{ num(r.model_score, 3) }}</td><td>{{ num(r.target_weight, 3) }}</td>
            <td>{{ r.market_cap != null ? (r.market_cap / 1e8).toFixed(2) : '—' }}</td>
            <td>{{ big(r.daily_turnover) }}</td><td>{{ r.constraint_status }}</td>
            <td>{{ r.portfolio_mode }}</td><td>{{ r.industry_l2 || r.industry_l1 || '—' }}</td>
          </tr>
        </tbody>
      </table>

      <div v-else-if="tab === 'arm'">
        <div class="pill-row">
          <span class="pill warn-pill">研究手臂 · 不替代 preselect/pk 生产名单</span>
          <span class="pill" v-if="selection?.arm?.meta?.snapshot_date">
            快照日 {{ selection.arm.meta.snapshot_date }}</span>
          <span class="pill" v-if="selection?.arm?.meta?.arm">手臂 {{ selection.arm.meta.arm }}</span>
          <span class="pill" v-if="selection?.arm?.meta?.domain_size">
            域内 {{ selection.arm.meta.domain_size }} 只</span>
          <span class="pill" v-if="selection?.arm?.meta?.weight_robustness?.top_overlap_pct != null">
            权重稳健性 top10 重合
            {{ (selection.arm.meta.weight_robustness.top_overlap_pct * 100).toFixed(0) }}%
            （全历史 vs 最近 {{ selection.arm.meta.weight_robustness.weights_months_recent }} 个月）</span>
        </div>
        <table class="data-table">
          <thead><tr><th>#</th><th>代码</th><th>名称</th><th>板块</th><th>行业</th>
            <th>快照日名称(PIT)</th><th>分数</th>
            <th>收盘</th><th>可下单</th><th>手数</th><th>金额</th><th>持仓</th></tr></thead>
          <tbody>
            <tr v-for="(r, i) in selection?.arm?.payload?.rows || []" :key="i">
              <td>{{ r.rank }}</td>
              <td class="mono link" @click="gotoStock(r.stock_code)">{{ r.stock_code }}</td>
              <td class="link" @click="gotoStock(r.stock_code)">{{ r.name || '—' }}</td>
              <td>{{ r.board }}</td><td>{{ r.industry }}</td>
              <td class="tiny">{{ r.pit_name || '—' }}</td>
              <td>{{ num(r.score, 4) }}</td><td>{{ num(r.last_close, 2) }}</td>
              <td :class="truthy(r.tradable) ? 'ok' : 'bad'">
                {{ truthy(r.tradable) ? (truthy(r.limit_up) ? '涨停禁买' : '可买') : '停牌' }}
              </td>
              <td>{{ r.lots_at_capital }}</td><td>{{ r.planned_amount }}</td>
              <td>{{ truthy(r.in_portfolio) ? '✅' : '候选' }}</td>
            </tr>
          </tbody>
        </table>
        <div class="empty tiny" v-if="selection?.arm?.meta?.st_pit_caveat">
          ST 过滤：{{ selection.arm.meta.st_pit_caveat }}
        </div>
        <div class="empty tiny" v-if="selection?.arm?.meta?.order_precheck">
          下单预检：{{ selection.arm.meta.order_precheck }}
        </div>
      </div>

      <div v-else class="diff-grid">
        <div class="diff-col">
          <h6 class="ok">新进 PK（{{ selection?.diff?.entered?.length || 0 }}）</h6>
          <div v-for="c in selection?.diff?.entered || []" :key="c" class="diff-row">
            <span class="mono">{{ c }}</span><span>{{ selection?.diff?.entered_names?.[c] || '' }}</span>
          </div>
          <div v-if="!selection?.diff?.entered?.length" class="empty tiny">无</div>
        </div>
        <div class="diff-col">
          <h6 class="bad">候选池落选（{{ selection?.diff?.exited?.length || 0 }}）</h6>
          <div v-for="c in selection?.diff?.exited || []" :key="c" class="diff-row">
            <span class="mono">{{ c }}</span><span>{{ selection?.diff?.exited_names?.[c] || '' }}</span>
          </div>
          <div v-if="!selection?.diff?.exited?.length" class="empty tiny">无</div>
        </div>
        <div class="diff-col">
          <h6>保留（{{ selection?.diff?.kept?.length || 0 }}）</h6>
          <div v-for="c in selection?.diff?.kept || []" :key="c" class="diff-row"><span class="mono">{{ c }}</span></div>
          <div v-if="!selection?.diff?.kept?.length" class="empty tiny">无</div>
        </div>
      </div>
    </div>

    <div class="panel">
      <h6>触发流水线阶段（子进程 <code>scripts/run_cn_pipeline.py --stage …</code>）</h6>
      <div class="run-form">
        <label><span>阶段</span>
          <select v-model="runStage">
            <option v-for="s in overview?.stage_catalog || []" :key="s.stage" :value="s.stage">
              {{ s.stage }}{{ s.configured ? '（configured）' : '' }}{{ s.overwrites_results ? ' ⚠覆盖' : '' }}
            </option>
          </select>
        </label>
        <label><span>额外参数（空格分隔）</span><input v-model="extraArgs" placeholder="--force-rebalance" /></label>
        <label class="chk">
          <input v-model="confirmOverwrite" type="checkbox" />
          我确认该阶段会覆盖 <code>output/results_cn/*</code>
        </label>
        <button class="btn-primary" :disabled="running || (needsConfirm && !confirmOverwrite)" @click="startRun">运行</button>
        <span v-if="needsConfirm" class="warn-text">该阶段会写当日名单，需勾选确认</span>
      </div>
      <div class="log-box">
        <div class="log-head">
          <span>运行日志 {{ activeRunId ? '· ' + activeRunId + ' · ' + runStatus : '' }}</span>
          <span class="empty tiny">同一时刻只允许一个阶段在跑</span>
        </div>
        <pre class="log">{{ runLog.length ? runLog.join('\n') : '（暂无）' }}</pre>
      </div>
      <table v-if="overview?.runs?.length" class="data-table">
        <thead><tr><th>run</th><th>阶段</th><th>状态</th><th>返回码</th><th>开始</th><th>结束</th><th>日志行</th></tr></thead>
        <tbody>
          <tr v-for="r in overview.runs" :key="r.run_id">
            <td class="mono">{{ r.run_id }}</td><td>{{ r.stage }}</td>
            <td>{{ r.status }}</td><td>{{ r.returncode ?? '—' }}</td>
            <td>{{ r.started_at || '—' }}</td><td>{{ r.finished_at || '—' }}</td><td>{{ r.log_lines }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h6>最近流水线报告（output/pipeline_reports）</h6>
      <table class="data-table">
        <thead><tr><th>报告</th><th>开始</th><th>结束</th><th>耗时(s)</th><th>结果</th><th>失败阶段</th><th>配置</th></tr></thead>
        <tbody>
          <tr v-for="r in overview?.recent_reports || []" :key="r.file">
            <td class="mono">{{ r.file }}</td><td>{{ r.started_at || '—' }}</td>
            <td>{{ r.finished_at || '—' }}</td>
            <td>{{ r.seconds != null ? r.seconds.toFixed(1) : '—' }}</td>
            <td :class="r.result === 'ok' ? 'ok' : 'bad'">{{ r.result || '—' }}</td>
            <td>{{ r.failed.join(', ') || '—' }}</td>
            <td class="mono">{{ r.config }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  getPipelineOverview, getPipelineRun, getPipelineSelection, startPipelineStage,
  type PipelineOverview, type PipelineSelection,
} from '@/api/client'

const router = useRouter()
const overview = ref<PipelineOverview | null>(null)
const selection = ref<PipelineSelection | null>(null)
const loading = ref(false)
const showAllStages = ref(false)
const tab = ref('preselected')
const tabs = [
  { key: 'preselected', label: '候选池（preselect）' },
  { key: 'selected', label: '最终名单（PK 后）' },
  { key: 'arm', label: '研究手臂（arm_selection）' },
  { key: 'diff', label: '进 / 出差异' },
]

const runStage = ref('sleeve_diagnostics')
const extraArgs = ref('')
const confirmOverwrite = ref(false)
const activeRunId = ref('')
const runStatus = ref('')
const runLog = ref<string[]>([])
let timer: ReturnType<typeof setInterval> | null = null

const visibleStages = computed(() => {
  const catalog = overview.value?.stage_catalog || []
  return showAllStages.value ? catalog
    : catalog.filter((s) => s.configured || s.overwrites_results || s.artifacts.length)
})
const needsConfirm = computed(() => {
  const item = overview.value?.stage_catalog.find((s) => s.stage === runStage.value)
  return Boolean(item?.overwrites_results)
})
const running = computed(() => runStatus.value === 'running' || runStatus.value === 'pending')

/** 后端把 bool 转成了 1/0/True/false 各种形态，这里统一判断（否则"可买"会显示成"停牌"）。 */
function truthy(v: any) {
  return v === true || v === 1 || String(v).toLowerCase() === 'true' || String(v) === '1' || String(v) === '1.0'
}
function num(v: any, d: number) {
  const n = Number(v)
  return Number.isFinite(n) ? n.toFixed(d) : '—'
}
function big(v: any) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return n >= 1e8 ? (n / 1e8).toFixed(2) + '亿' : (n / 1e4).toFixed(0) + '万'
}
function fmtBytes(v?: number) {
  if (v == null) return ''
  const u = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0, x = v
  while (x >= 1024 && i < u.length - 1) { x /= 1024; i++ }
  return x.toFixed(i > 2 ? 2 : 0) + u[i]
}
function kv(obj?: Record<string, any>) {
  if (!obj || !Object.keys(obj).length) return '—'
  return Object.entries(obj).map(([k, v]) => `${k}:${v}`).join(' ')
}

function gotoStock(code: string) {
  if (code) router.push({ path: '/kline', query: { code } })
}

async function refreshAll() {
  loading.value = true
  try {
    const [o, s] = await Promise.all([getPipelineOverview(), getPipelineSelection()])
    overview.value = o
    selection.value = s
  } finally {
    loading.value = false
  }
}

async function poll() {
  if (!activeRunId.value) return
  try {
    const d = await getPipelineRun(activeRunId.value)
    runStatus.value = d.status
    runLog.value = d.log
    if (d.status === 'done' || d.status === 'failed') {
      if (timer) clearInterval(timer)
      timer = null
      refreshAll()
    }
  } catch { /* 轮询抖动忽略 */ }
}

async function startRun() {
  runLog.value = []
  runStatus.value = 'pending'
  const extra = extraArgs.value.trim() ? extraArgs.value.trim().split(/\s+/) : []
  try {
    const res = await startPipelineStage({
      stage: runStage.value, confirm_overwrite: confirmOverwrite.value, extra_args: extra,
    })
    activeRunId.value = res.run_id
    runStatus.value = res.status
    timer = setInterval(poll, 2000)
    await poll()
  } catch (e: any) {
    runStatus.value = 'failed'
    runLog.value = [String(e?.response?.data?.detail || e?.message || e)]
  }
}

onMounted(refreshAll)
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<style scoped>
/* 页面专属：阶段卡片网格 + 触发区表单 + 差异三列 */
.stage-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(330px, 1fr)); gap: 10px; margin-bottom: 8px; }
.stage-card {
  background: var(--bg-card); border: 1px solid var(--border-color);
  border-left: 3px solid var(--border-color); border-radius: var(--r-3); padding: 8px 10px;
}
.stage-card.configured { border-left-color: var(--accent-blue); }
.stage-card.warn { border-left-color: var(--accent-orange); }
.stage-head { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; }
.stage-name { font-weight: 600; font-size: var(--fs-md); }
.tag.cfg { color: var(--accent-blue); border-color: var(--accent-blue); }
.artifact { display: flex; align-items: center; gap: 6px; font-size: var(--fs-xs); color: var(--text-muted); }
.artifact.missing { opacity: 0.6; }
.dot { width: 6px; height: 6px; border-radius: 50%; flex: 0 0 auto; }
.dot.on { background: var(--accent-down); }
.dot.off { background: var(--accent-up); }
.apath { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 200px; }
.ameta { margin-left: auto; white-space: nowrap; }
.detail { margin-top: 6px; font-size: var(--fs-xs); color: var(--text-muted); line-height: 1.4; }
.meta { font-size: var(--fs-sm); color: var(--text-muted); }
.warn-pill { color: var(--accent-orange); border-color: var(--accent-orange); }
.diff-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
.diff-col h6 { font-size: var(--fs-sm); margin-bottom: 6px; }
.diff-row { display: flex; gap: 8px; font-size: var(--fs-sm); padding: 2px 0; }
.run-form { display: flex; align-items: flex-end; gap: 12px; flex-wrap: wrap; margin-bottom: 10px; }
@media (max-width: 1100px) { .diff-grid { grid-template-columns: 1fr; } }
</style>
