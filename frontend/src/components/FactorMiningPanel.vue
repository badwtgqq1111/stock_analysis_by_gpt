<template>
  <div class="mining-panel">
    <div class="mp-toolbar">
      <span class="dim tiny">
        会话落盘 <code>output/factor_mining_sessions/</code>；跑完的因子可以「入池」成候选策略，
        <b>并且会自动生成一条轨迹</b>进「投研执行台」（<code>agent_run.py from-session</code>）。
      </span>
      <a class="btn-ghost link-btn" href="#/strategies">策略池 →</a>
      <button class="btn-ghost" :disabled="loadingHistory" @click="refreshHistory">刷新会话</button>
    </div>

    <div class="workbench">
      <!-- ① 左栏：新建 + 方向模板 + 会话 + 研究工具（P2.43 §2.1） -->
      <aside class="wb-col wb-left">
        <button class="btn-primary new-btn" @click="newSession">+ 新建挖掘会话</button>

        <div class="panel">
          <h6>方向模板 <span class="dim tiny">{{ (context?.templates || []).length }} 条 · 点一下填进中栏输入框，可再改</span></h6>
          <div class="tpl-chips">
            <button v-for="t in (context?.templates || [])" :key="t.label" class="chip"
                    :title="`来源：${sourceName(t.source)}`"
                    :class="{ on: direction.trim() === t.direction }" @click="useTemplate(t)">
              {{ t.label }}
              <span class="chip-src" v-if="t.source">{{ sourceShort(t.source) }}</span>
            </button>
          </div>
        </div>

        <!-- 灵感来源 / 知识库：只提供方向、不提供证据；每条都写清"怎么用"和"坑" -->
        <div class="panel">
          <h6>灵感来源 / 知识库
            <span class="dim tiny">{{ sources.length }} 个 · 点开看用法与坑</span>
          </h6>
          <div class="src-regions">
            <div v-for="(items, region) in byRegion" :key="region" class="src-region">
              <span class="region-tag">{{ region }}</span>
              <div class="src-chips">
                <button v-for="s in items" :key="s.id" class="chip"
                        :class="{ on: openSource === s.id }" @click="toggleSource(s.id)">
                  {{ s.name }}
                </button>
              </div>
            </div>
          </div>
          <div v-if="openSourceData" class="src-detail">
            <div class="sd-head">
              <b>{{ openSourceData.name }}</b>
              <span class="tiny dim">{{ openSourceData.kind }} · {{ openSourceData.cost }}</span>
            </div>
            <p class="tiny">{{ openSourceData.what }}</p>
            <p class="tiny"><b>怎么用：</b>{{ (openSourceData.use || []).join(' / ') }}</p>
            <p class="tiny warn-text"><b>坑：</b>{{ openSourceData.caution }}</p>
            <p class="tiny dim">入口：{{ openSourceData.entry }}</p>
            <div class="sd-actions">
              <a v-if="openSourceData.url" class="btn-ghost btn-xs link-btn" :href="openSourceData.url"
                 target="_blank" rel="noopener">打开 ↗</a>
              <a v-if="openSourceData.api" class="btn-ghost btn-xs link-btn" :href="openSourceData.api"
                 target="_blank" rel="noopener">API ↗</a>
              <button class="btn-ghost btn-xs" @click="copy(openSourceData.url || openSourceData.entry || '')">复制入口</button>
              <button v-if="templateOfSource(openSourceData.id)" class="btn-ghost btn-xs"
                      @click="useTemplate(templateOfSource(openSourceData.id)!)">用它的方向</button>
            </div>
          </div>
          <p class="tiny dim">{{ (sourceNote || [])[0] }}</p>
        </div>

        <div class="panel">
          <h6>会话 <span class="dim tiny">{{ history.length }}</span></h6>
          <ul class="session-list">
            <li v-for="h in history" :key="h.session_id"
                :class="{ active: h.session_id === sessionId }" @click="loadSession(h.session_id)">
              <span class="dot" :class="statusDot(h.status)" :title="h.status" />
              <span class="s-main">
                <span class="s-dir">{{ short(h.direction) }}</span>
                <span class="tiny dim">{{ (h.created_at || '').slice(5, 16) }} · {{ h.n_factors }} 因子</span>
              </span>
              <a class="trace-link" :href="`#/agent?tab=console&run=${h.session_id}-mining`"
                 title="在「投研执行台」看这次挖掘的轨迹（阶段 / 门槛 / 回滚）">轨迹 →</a>
            </li>
            <li v-if="!history.length" class="empty tiny">还没有会话</li>
          </ul>
        </div>

        <div class="panel">
          <h6>研究工具 <span class="dim tiny">这些就是我们的「工具行」</span></h6>
          <ul class="tool-list">
            <li v-for="tool in (context?.tools || [])" :key="tool.name">
              <a :href="tool.page">{{ tool.name }} →</a>
              <code class="tiny">{{ tool.command }}</code>
              <span class="tiny dim">{{ tool.artifact }}</span>
            </li>
          </ul>
        </div>
      </aside>

      <!-- ② 中栏：上下文 chips + 结构化对话流 + **常驻 composer**（P2.43 §2.2~2.4） -->
      <main class="wb-col wb-center">
        <div class="panel ctx-panel">
          <div class="ctx-chips">
            <span class="chip static" v-if="context?.panel?.available">
              面板 <b>{{ context.panel.version }}</b> · <b>{{ context.panel.stocks }}</b> 只 ·
              <b>{{ context.panel.features }}</b> 字段 <span class="dim">（{{ context.panel.asof }}）</span>
            </span>
            <span class="chip static" v-if="context?.last_session?.panel_rows">
              规模 <b>{{ context.last_session.panel_rows }}</b> 行 ·
              <b>{{ context.last_session.trading_days }}</b> 个交易日
            </span>
            <span class="chip static" v-if="context?.criteria">
              准入口径 <b>|IC| ≥ {{ context.criteria.min_abs_ic }}</b> ·
              <b>BH-FDR q={{ context.criteria.fdr_q }}</b> ·
              <b>|t| ≥ {{ context.criteria.hlz_t }}</b>
            </span>
            <span class="chip static" v-if="context?.last_session?.best">
              上次最佳 <b>{{ context.last_session.best.name }}</b>
              （ICIR {{ num(context.last_session.best.icir, 2) }}）
            </span>
            <span class="chip static" v-if="sessionId">会话 <b>{{ sessionId }}</b></span>
          </div>
          <p class="tiny dim" v-if="context?.panel?.source">
            口径取自 <code>{{ context.panel.source }}</code> 与因子库 —— 页面上显示的阈值和脚本用的是同一份。
          </p>
        </div>

        <div class="panel trace">
          <h6>
            执行轨迹
            <span class="dim tiny">{{ steps.length }} 步 · {{ factorSteps.length }} 张因子卡</span>
            <span class="dim tiny" v-if="outDir">产物 <code>{{ rel(outDir) }}</code></span>
            <span class="status" :class="statusTone">{{ status }}</span>
          </h6>
          <div ref="chatRef" class="chat">
            <div v-if="!steps.length" class="empty">
              在左边选/新建会话，或直接在下面输入方向后点「开始挖掘」。
            </div>
            <template v-for="(step, i) in steps" :key="i">
              <!-- 因子卡：结构化展示 + 三个量化动作（复制表达式 / 入池成候选 / 复制产物路径） -->
              <div v-if="step.kind === 'factor'" class="factor-card" :class="step.ok ? 'ok' : 'err'">
                <div class="fc-head">
                  <b>{{ step.name }}</b>
                  <span class="tag" :class="step.ok ? 'ok' : 'dim'">{{ step.ok ? 'OK' : 'ERR' }}</span>
                  <span class="fc-actions">
                    <button class="btn-ghost btn-xs" @click="copy(step.expression || '')">复制表达式</button>
                    <button class="btn-ghost btn-xs" :disabled="pooling" @click="poolStep(step)">入池成候选</button>
                    <button class="btn-ghost btn-xs" @click="copy(outDir || '')">复制产物路径</button>
                  </span>
                </div>
                <div class="fc-metrics">
                  <span>IC <b>{{ num(step.ic, 4) }}</b></span>
                  <span>ICIR <b>{{ num(step.icir, 3) }}</b></span>
                  <span>NW t <b>{{ num(step.nwt, 2) }}</b></span>
                  <span>maxρ <b>{{ num(step.maxcorr, 2) }}</b></span>
                </div>
                <code class="f-expr">{{ step.expression }}</code>
              </div>
              <div v-else class="step" :class="step.kind">
                <span class="step-role">{{ step.role }}</span>
                <pre class="step-text">{{ step.text }}</pre>
              </div>
            </template>
          </div>

          <!-- 常驻 composer：滚动轨迹时不离开视线（sticky bottom） -->
          <div class="composer-sticky">
            <label class="field">
              <span>目标方向（自然语言；点左栏模板可一键填入，可再改；支持多行）</span>
              <textarea v-model="direction" rows="5"
                        placeholder="用自然语言描述想要的因子方向与经济含义：想预测什么（未来几日收益/行业相对收益）、用什么字段与算子、要避开什么（与现有字段高相关 / 只在某一年有效 / 依赖集合竞价数据）"></textarea>
            </label>
            <div class="param-chips">
              <label class="param"><span>轮数</span>
                <input v-model.number="rounds" type="number" min="1" max="10" /></label>
              <label class="param"><span>每轮候选</span>
                <input v-model.number="proposals" type="number" min="1" max="20" /></label>
              <label class="param"><span>模型</span>
                <select v-model="model">
                  <option value="deepseek-flash">deepseek-flash</option>
                  <option value="deepseek-chat">deepseek-chat</option>
                  <option value="deepseek-reasoner">deepseek-reasoner</option>
                </select>
              </label>
              <label class="param check">
                <input v-model="dryRun" type="checkbox" />
                <span>自检（不调 LLM，秒级）</span>
              </label>
              <button class="btn-primary" :disabled="running" @click="start">
                {{ running ? '挖掘中…' : '开始挖掘' }}
              </button>
              <button class="btn-ghost" :disabled="!running" @click="stopPolling">停止轮询</button>
            </div>
            <p class="hint tiny" v-if="copyMsg">{{ copyMsg }}</p>
          </div>
        </div>
      </main>

      <!-- ③ 右栏：本轮因子（按 |ICIR| 排序 + 入池） -->
      <aside class="wb-col wb-right">
        <div class="panel">
          <h6>
            本轮因子
            <span class="dim tiny">{{ factors.length }}</span>
          </h6>
          <div class="pool-actions">
            <button class="btn-ghost btn-sm" :disabled="!factors.length || pooling"
                    @click="poolSession">整轮结果入池</button>
            <span class="tiny dim" v-if="poolMsg">{{ poolMsg }}</span>
          </div>
          <div v-if="!factors.length" class="empty tiny">
            会话跑完后，通过 BH-FDR 的因子会按 |ICIR| 排在这里（已按名字去重）。
          </div>
          <ul v-else class="factor-list">
            <li v-for="f in factorRows" :key="f.name" class="factor">
              <div class="f-head">
                <b class="f-name">{{ f.name }}</b>
                <span class="tag" :class="f.fdr ? 'ok' : 'dim'">{{ f.fdr ? 'FDR ✅' : '未过' }}</span>
                <button class="btn-ghost btn-xs" :disabled="pooling" @click="poolFactor(f)">入池</button>
              </div>
              <div class="f-metrics">
                <span>IC <b>{{ num(f.ic_mean, 4) }}</b></span>
                <span>ICIR <b>{{ num(f.icir, 3) }}</b></span>
                <span>NW t <b>{{ num(f.nw_t, 2) }}</b></span>
                <span>正号月 <b>{{ pct(f.pos_ratio) }}</b></span>
                <span>maxρ <b>{{ num(f.max_corr_with_fields, 2) }}</b></span>
              </div>
              <code class="f-expr">{{ f.expression }}</code>
              <div class="tiny dim" v-if="f.reason">{{ f.reason }}</div>
            </li>
          </ul>
          <ul class="hint tiny" v-if="outDir">
            <li>产物目录 <code>{{ rel(outDir) }}</code>（factors_best.csv / summary.json / round_*）</li>
            <li>入池的候选出现在「策略池」页的候选区，需人工升格才会进生产。</li>
          </ul>
        </div>
      </aside>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import {
  createMiningSession, getMiningContext, getMiningSession, getResearchSources, listMiningSessions,
  registerStrategyCandidate,
  type MiningContext, type MiningFactor, type MiningMessage, type MiningSessionSummary,
  type ResearchSource, type ResearchSourcesResponse,
} from '@/api/client'

const ROOT_MARK = 'stock_analysis_by_gpt/'

const direction = ref(
  'A 股小市值与量价/估值的交互因子：要求能在月度横截面上稳定排序，避免与 pe/pb/rps/rev 单因子高度重合',
)
const rounds = ref(2)
const proposals = ref(6)
const model = ref('deepseek-flash')
const dryRun = ref(false)

const sessionId = ref('')
const status = ref('idle')
const running = ref(false)
const messages = ref<MiningMessage[]>([])
const factors = ref<MiningFactor[]>([])
const outDir = ref('')
const chatRef = ref<HTMLElement | null>(null)
const history = ref<MiningSessionSummary[]>([])
const loadingHistory = ref(false)
const pooling = ref(false)
const poolMsg = ref('')
const copyMsg = ref('')
const context = ref<MiningContext | null>(null)
const research = ref<ResearchSourcesResponse | null>(null)
const openSource = ref('')

/** 灵感来源（来自 config/research_sources.json）：按地区分组，点开看用法与坑 */
const sources = computed<ResearchSource[]>(() => research.value?.sources || [])
const byRegion = computed<Record<string, ResearchSource[]>>(() => research.value?.by_region || {})
const sourceNote = computed(() => research.value?.note || [])
const openSourceData = computed(() => sources.value.find((s) => s.id === openSource.value) || null)

function toggleSource(id: string) {
  openSource.value = openSource.value === id ? '' : id
}
function sourceName(id?: string) {
  return sources.value.find((s) => s.id === id)?.name || id || '—'
}
function sourceShort(id?: string) {
  const name = sourceName(id)
  return String(name).split(/[（(]/)[0]
}
/** 某个来源对应的方向模板（点「用它的方向」时用；没有就返回 null，按钮不显示） */
function templateOfSource(id: string) {
  return (context.value?.templates || []).find((t) => t.source === id) || null
}
let timer: ReturnType<typeof setInterval> | null = null

/** 因子行：统一 fdr_pass 的多种形态（true / "True" / ""），并按 |ICIR| 排序 */
const factorRows = computed(() => factors.value
  .map((f) => ({
    ...f,
    fdr: String(f.fdr_pass).toLowerCase() === 'true',
    _icir: Math.abs(Number(f.icir) || 0),
  }))
  .sort((a, b) => b._icir - a._icir))

/**
 * 对话流**结构化**：把 `[llm-factor] …` 的日志行分成 env / round / factor / summary / artifact 五类。
 * 纯前端解析、不改后端日志格式（与 `scripts/agent_run.py` 只解析日志的原则一致）；
 * `OK/ERR` 行渲染成**因子卡**（带指标与表达式），其它行按原文渲染。
 */
interface Step {
  kind: 'env' | 'round' | 'factor' | 'summary' | 'artifact' | 'raw' | 'error'
  role: string
  text: string
  name?: string
  ok?: boolean
  ic?: number
  icir?: number
  nwt?: number
  maxcorr?: number
  expression?: string
}

const FACTOR_RE = /^\s*\[llm-factor\]\s+(OK|ERR)\s+([A-Za-z0-9_\-]+):\s*IC=(-?[\d.]+)\s+ICIR=(-?[\d.]+)\s+NWt=(-?[\d.]+)\s*\|\s*maxρ=(-?[\d.eE+]+)\s*\|\s*(.+)$/

const steps = computed<Step[]>(() => {
  const out: Step[] = []
  for (const m of messages.value) {
    const text = String(m.text || '')
    const one = text.trim()
    if (m.role === 'assistant' && !one.startsWith('[llm-factor]')) {
      out.push({ kind: 'summary', role: 'AGENT', text })
      continue
    }
    if (/失败|错误|Traceback|ERROR/.test(text)) {
      out.push({ kind: 'error', role: 'ERROR', text })
      continue
    }
    const factor = one.match(FACTOR_RE)
    if (factor) {
      out.push({
        kind: 'factor', role: 'FACTOR', text: one,
        ok: factor[1] === 'OK', name: factor[2],
        ic: Number(factor[3]), icir: Number(factor[4]), nwt: Number(factor[5]),
        maxcorr: Number(factor[6]), expression: factor[7].trim(),
      })
      continue
    }
    if (/面板 .* 行/.test(one)) out.push({ kind: 'env', role: 'ENV', text })
    else if (/第 \d+ 轮/.test(one)) out.push({ kind: 'round', role: 'ROUND', text })
    else if (/通过 BH-FDR/.test(one)) out.push({ kind: 'summary', role: 'SUMMARY', text })
    else if (/^\s*\[llm-factor\]\s*输出/.test(one)) out.push({ kind: 'artifact', role: 'OUT', text })
    else out.push({ kind: 'raw', role: 'LOG', text })
  }
  return out
})
const factorSteps = computed(() => steps.value.filter((s) => s.kind === 'factor'))

function useTemplate(t: { label: string; direction: string }) {
  direction.value = t.direction
  void nextTick(() => {
    const box = document.querySelector('.composer-sticky textarea') as HTMLTextAreaElement | null
    box?.focus()
  })
}

async function copy(text: string) {
  if (!text) {
    copyMsg.value = '没有可复制的内容'
    return
  }
  try {
    await navigator.clipboard.writeText(text)
    copyMsg.value = `已复制：${text.length > 60 ? text.slice(0, 60) + '…' : text}`
  } catch {
    copyMsg.value = `剪贴板不可用，内容：${text.slice(0, 80)}`
  }
  setTimeout(() => { copyMsg.value = '' }, 4000)
}

/** 因子卡上的「入池成候选」：与右栏同一套登记逻辑（同一份 metrics 口径） */
async function poolStep(step: Step) {
  await poolFactor({
    name: String(step.name || ''), ic_mean: step.ic, icir: step.icir, nw_t: step.nwt,
    pos_ratio: undefined, max_corr_with_fields: step.maxcorr,
    fdr_pass: step.ok, expression: String(step.expression || ''), reason: '来自因子卡（结构化轨迹）',
  } as MiningFactor)
}

async function refreshHistory() {
  loadingHistory.value = true
  try {
    history.value = await listMiningSessions()
  } catch {
    /* 历史列表拉取失败不影响挖掘 */
  } finally {
    loadingHistory.value = false
  }
}

async function loadContext() {
  try {
    context.value = await getMiningContext()
  } catch {
    /* 上下文 chips 拿不到不影响挖掘（页面上会少几个 chip） */
  }
  try {
    research.value = await getResearchSources()
  } catch {
    /* 灵感来源拉不到只影响左栏那块面板 */
  }
}

async function loadSession(id: string) {
  if (running.value) {
    status.value = 'running（先等当前会话结束或停止轮询）'
    return
  }
  try {
    const data = await getMiningSession(id)
    sessionId.value = data.session_id
    status.value = data.status
    messages.value = data.messages || []
    factors.value = data.factors || []
    outDir.value = data.out_dir || ''
    poolMsg.value = ''
    if (data.request?.direction) direction.value = data.request.direction
    if (data.request?.rounds) rounds.value = Number(data.request.rounds)
    if (data.request?.proposals) proposals.value = Number(data.request.proposals)
    if (data.request?.model) model.value = String(data.request.model)
    await scrollToEnd()
  } catch (e: any) {
    status.value = 'load-failed：' + (e?.message || e)
  }
}

function newSession() {
  if (running.value) { status.value = 'running（先等当前会话结束）'; return }
  sessionId.value = ''
  messages.value = []
  factors.value = []
  outDir.value = ''
  poolMsg.value = ''
  status.value = 'idle'
}

const statusTone = computed(() => {
  if (/^failed|load-failed/.test(status.value)) return 'bad'
  if (running.value) return 'busy'
  if (status.value === 'done') return 'ok'
  return ''
})

function statusDot(value: string) {
  if (value === 'done') return 'ok'
  if (value === 'running') return 'busy'
  if (value === 'failed' || value === 'interrupted') return 'bad'
  return 'idle'
}

function short(text?: string) {
  const value = (text || '').trim()
  return value.length > 22 ? `${value.slice(0, 22)}…` : value || '(无方向)'
}

/** 绝对路径 → 仓库相对路径（脚本打印的是绝对路径，页面上不该出现一长串 /Users/...） */
function rel(path: string) {
  if (!path) return ''
  const idx = path.indexOf(ROOT_MARK)
  return idx >= 0 ? path.slice(idx + ROOT_MARK.length) : path
}

function num(v: any, d: number) {
  const n = Number(v)
  return Number.isFinite(n) ? n.toFixed(d) : '—'
}

function pct(v: any) {
  const n = Number(v)
  return Number.isFinite(n) ? (n * 100).toFixed(1) + '%' : '—'
}

async function scrollToEnd() {
  await nextTick()
  if (chatRef.value) chatRef.value.scrollTop = chatRef.value.scrollHeight
}

async function poll() {
  if (!sessionId.value) return
  try {
    const data = await getMiningSession(sessionId.value)
    messages.value = data.messages || []
    factors.value = data.factors || []
    status.value = data.status
    outDir.value = data.out_dir || ''
    await scrollToEnd()
    if (data.status === 'done' || data.status === 'failed') {
      finish()
      if (data.error) messages.value = [...messages.value, { role: 'system', text: data.error }]
      void refreshHistory()
      void loadContext()
    }
  } catch {
    /* 轮询抖动忽略 */
  }
}

function finish() {
  if (timer) clearInterval(timer)
  timer = null
  running.value = false
}

function stopPolling() {
  finish()
  status.value = 'stopped（服务端子进程仍在跑）'
}

async function start() {
  if (!direction.value.trim()) {
    status.value = 'direction-empty'
    return
  }
  running.value = true
  messages.value = []
  factors.value = []
  outDir.value = ''
  poolMsg.value = ''
  status.value = 'submitting'
  try {
    const res = await createMiningSession({
      direction: direction.value.trim(),
      rounds: rounds.value,
      proposals: proposals.value,
      model: model.value,
      dry_run: dryRun.value,
    })
    sessionId.value = res.session_id
    status.value = res.status
    timer = setInterval(poll, 2000)
    await poll()
    void refreshHistory()
  } catch (e: any) {
    running.value = false
    status.value = 'failed：' + (e?.message || e)
  }
}

// ── 入池：把挖掘结果登记成「候选策略」───────────────────────────────────────
async function poolFactor(f: MiningFactor) {
  pooling.value = true
  poolMsg.value = ''
  try {
    const res = await registerStrategyCandidate({
      id: `llm-factor-${f.name}`,
      name: `LLM 因子：${f.name}`,
      kind: 'llm_factor',
      origin: 'llm_factor_mining',
      owner: model.value,
      stage: 'candidate',
      summary: `${f.reason || ''} 表达式：${f.expression || ''}`.trim(),
      entry: `uv run python scripts/llm_factor_mining.py --direction "..." --rounds ${rounds.value} --proposals ${proposals.value}`,
      metrics: {
        ic_mean: Number(f.ic_mean), icir: Number(f.icir), nw_t: Number(f.nw_t),
        pos_ratio: Number(f.pos_ratio), max_corr: Number(f.max_corr_with_fields),
        fdr_pass: String(f.fdr_pass),
      },
      artifacts: outDir.value ? [`${rel(outDir.value)}/factors_best.csv`] : [],
      evidence: outDir.value ? [`${rel(outDir.value)}/summary.json`] : [],
    })
    poolMsg.value = `已入池：${res.id}（去策略池升格）`
  } catch (e: any) {
    poolMsg.value = `入池失败：${e?.message || e}`
  } finally {
    pooling.value = false
  }
}

async function poolSession() {
  const best = factorRows.value[0]
  if (!best) return
  await poolFactor(best)
  poolMsg.value = `已把本轮最佳因子入池：${poolMsg.value}`
}

const route = useRoute()

onMounted(async () => {
  await Promise.all([refreshHistory(), loadContext()])
  // 深链 `?session=<id>`（执行台点「去会话 →」跳过来）：自动选中那条会话
  const wanted = String(route.query.session || '')
  if (wanted) await loadSession(wanted)
})
</script>

<style scoped>
/* 三栏 agent 工作台：左（新建/模板/会话/工具）｜中（上下文 + 对话流 + 常驻 composer）｜右（本轮因子） */
.mining-panel { display: block; }
.mp-toolbar {
  display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap;
  margin-bottom: var(--sp-3);
}
.mp-toolbar .dim { margin-right: auto; }
.workbench { display: grid; grid-template-columns: 250px minmax(0, 1fr) 360px; gap: var(--sp-3); align-items: start; }
.wb-col { min-width: 0; display: flex; flex-direction: column; gap: var(--sp-2); }
.wb-left .panel, .wb-right .panel { margin: 0; }
.panel h6 { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.new-btn { width: 100%; }
.tpl-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip {
  background: var(--bg-input); border: 1px solid var(--border-color); color: inherit;
  border-radius: 999px; padding: 2px 9px; font-size: var(--fs-xs); cursor: pointer;
}
.chip:hover { border-color: var(--text-dim); }
.chip.on { border-color: var(--accent-gold); color: var(--accent-gold); }
.chip.static { cursor: default; }
.chip-src { font-size: 9px; opacity: 0.65; margin-left: 4px; }
.src-regions { display: flex; flex-direction: column; gap: 5px; max-height: 30vh; overflow: auto; }
.src-region { display: flex; gap: 6px; align-items: flex-start; }
.region-tag { flex-shrink: 0; font-size: 10px; color: var(--text-dim); padding-top: 3px; width: 52px; }
.src-chips { display: flex; flex-wrap: wrap; gap: 4px; }
.src-detail { margin-top: 6px; border: 1px solid var(--border-color); border-radius: var(--r-2);
  padding: 6px 8px; background: var(--bg-input); display: flex; flex-direction: column; gap: 3px; }
.sd-head { display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap; }
.sd-actions { display: flex; gap: 4px; flex-wrap: wrap; margin-top: 2px; }
.ctx-panel { margin: 0; }
.ctx-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.session-list { list-style: none; margin: 0; padding: 0; max-height: 34vh; overflow: auto; }
.session-list li { display: flex; gap: 6px; align-items: flex-start; padding: 5px 6px;
  border-radius: var(--r-2); cursor: pointer; }
.session-list li:hover { background: rgba(255, 255, 255, 0.04); }
.session-list li.active { background: rgba(255, 209, 102, 0.1); }
.s-main { display: flex; flex-direction: column; min-width: 0; line-height: 1.35; }
.s-dir { font-size: var(--fs-sm); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.trace-link { margin-left: auto; flex-shrink: 0; font-size: var(--fs-xs); color: var(--accent-gold); }
.trace-link:hover { text-decoration: underline; }
.dot { width: 7px; height: 7px; border-radius: 50%; margin-top: 6px; flex-shrink: 0; background: var(--text-dim); }
.dot.ok { background: var(--accent-down); }
.dot.busy { background: var(--accent-gold); }
.dot.bad { background: var(--accent-up); }
.tool-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 5px; }
.tool-list li { display: flex; flex-direction: column; gap: 1px; }
.tool-list a { font-size: var(--fs-sm); color: var(--accent-gold); text-decoration: none; }
.tool-list a:hover { text-decoration: underline; }
.tool-list code { color: var(--text-dim); overflow-wrap: anywhere; }

.trace { margin: 0; }
.trace h6 { justify-content: flex-start; }
.chat { max-height: 46vh; overflow: auto; display: flex; flex-direction: column; gap: 4px;
  font-size: var(--fs-sm); }
.step { display: flex; gap: 6px; align-items: flex-start; }
.step-role { flex-shrink: 0; width: 58px; font-family: var(--font-mono); font-size: 10px;
  color: var(--text-dim); padding-top: 2px; }
.step-text { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; font-family: var(--font-mono);
  font-size: var(--fs-xs); line-height: 1.5; color: var(--text-muted); }
.step.summary .step-role { color: var(--accent-gold); }
.step.summary .step-text { color: var(--text-primary); }
.step.error .step-role, .step.error .step-text { color: var(--accent-up); }

/* 因子卡：结构化 + 三操作 */
.factor-card { border: 1px solid var(--border-color); border-left-width: 3px;
  border-radius: var(--r-2); padding: 6px 8px; background: var(--bg-input);
  display: flex; flex-direction: column; gap: 4px; }
.factor-card.ok { border-left-color: var(--accent-down); }
.factor-card.err { border-left-color: var(--accent-up); }
.fc-head { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.fc-head b { font-family: var(--font-mono); font-size: var(--fs-sm); }
.fc-actions { margin-left: auto; display: flex; gap: 4px; }
.fc-metrics { display: flex; gap: var(--sp-2); flex-wrap: wrap; font-size: 10px; color: var(--text-dim); }
.fc-metrics b { font-family: var(--font-mono); color: var(--text-muted); }

/* 常驻 composer：贴在中栏底部，滚动轨迹时不离开视线 */
.composer-sticky {
  position: sticky; bottom: 0; margin-top: var(--sp-2); padding-top: var(--sp-2);
  background: var(--bg-panel, rgba(18, 21, 28, 0.96)); border-top: 1px solid var(--border-color);
  backdrop-filter: blur(2px);
}
.field { display: flex; flex-direction: column; gap: 3px; }
.field > span { font-size: var(--fs-xs); color: var(--text-muted); }
.field textarea { background: var(--bg-input); border: 1px solid var(--border-color);
  border-radius: var(--r-2); color: var(--text-primary); padding: 8px 10px; resize: vertical;
  width: 100%; min-height: 120px; line-height: 1.55; font-size: var(--fs-sm); }
.param-chips { display: flex; gap: var(--sp-2); flex-wrap: wrap; align-items: flex-end; margin-top: 6px; }
.param { display: flex; flex-direction: column; gap: 2px; font-size: var(--fs-xs); color: var(--text-muted); }
.param input, .param select { background: var(--bg-input); border: 1px solid var(--border-color);
  border-radius: var(--r-2); color: var(--text-primary); padding: 3px 6px; width: 88px; }
.param.check { flex-direction: row; align-items: center; gap: 5px; padding-bottom: 4px; }
.status { font-family: var(--font-mono); font-size: var(--fs-xs); color: var(--text-muted); margin-left: auto; }
.status.ok { color: var(--accent-down); }
.status.bad { color: var(--accent-up); }
.status.busy { color: var(--accent-gold); }
.link-btn { text-decoration: none; display: inline-flex; align-items: center; }

.pool-actions { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; margin-bottom: var(--sp-2); }
.btn-xs { padding: 1px 6px; font-size: 10px; }
.factor-list { list-style: none; margin: 0; padding: 0; max-height: 46vh; overflow: auto;
  display: flex; flex-direction: column; gap: 6px; }
.factor { border: 1px solid var(--border-color); border-radius: var(--r-2); padding: 5px 7px;
  background: var(--bg-input); display: flex; flex-direction: column; gap: 3px; }
.f-head { display: flex; align-items: center; gap: 6px; }
.f-name { font-size: var(--fs-sm); flex: 1; min-width: 0; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.f-metrics { display: flex; gap: var(--sp-2); flex-wrap: wrap; font-size: 10px; color: var(--text-dim); }
.f-metrics b { font-family: var(--font-mono); color: var(--text-muted); }
.f-expr { font-size: 10px; color: var(--text-muted); overflow-wrap: anywhere; white-space: pre-wrap; }
.empty { color: var(--text-muted); font-size: var(--fs-sm); }
@media (max-width: 1300px) {
  .workbench { grid-template-columns: 1fr; }
  .session-list, .factor-list, .chat { max-height: 34vh; }
}
</style>
