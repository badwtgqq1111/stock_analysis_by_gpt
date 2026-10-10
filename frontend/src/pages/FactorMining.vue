<template>
  <div class="page">
    <PageHeader
      title="因子挖掘工作台"
      description="左=会话，中=任务与执行轨迹，右=产物与指标。<b>跑完的因子可以直接入池</b>：登记成「候选策略」进 <code>策略池</code>，验证通过再升格进生产，而不是散在 output/ 里没人知道。评估口径：月频横截面秩 IC / ICIR / Newey-West t / BH-FDR(q=0.05)；参数面板约 2GB，服务端串行执行。"
    >
      <template #actions>
        <a class="btn-ghost link-btn" href="#/strategies">策略池 →</a>
        <button class="btn-ghost" :disabled="loadingHistory" @click="refreshHistory">刷新会话</button>
      </template>
    </PageHeader>

    <div class="workbench">
      <!-- ① 会话列表（历史会话落盘持久化：output/factor_mining_sessions/） -->
      <aside class="panel wb-col wb-sessions">
        <h6>会话 <span class="dim tiny">{{ history.length }}</span></h6>
        <button class="btn-ghost btn-sm new-btn" @click="newSession">+ 新建会话</button>
        <ul class="session-list">
          <li v-for="h in history" :key="h.session_id"
              :class="{ active: h.session_id === sessionId }" @click="loadSession(h.session_id)">
            <span class="dot" :class="statusDot(h.status)" :title="h.status" />
            <span class="s-main">
              <span class="s-dir">{{ short(h.direction) }}</span>
              <span class="tiny dim">{{ (h.created_at || '').slice(5, 16) }} · {{ h.n_factors }} 因子</span>
            </span>
          </li>
          <li v-if="!history.length" class="empty tiny">还没有会话</li>
        </ul>
      </aside>

      <!-- ② 任务卡 + 执行轨迹 -->
      <main class="wb-col wb-main">
        <div class="panel composer">
          <h6>
            挖掘任务
            <span class="dim tiny" v-if="sessionId">会话 <code>{{ sessionId }}</code></span>
          </h6>
          <label class="field">
            <span>目标方向（自然语言，越具体越省 token）</span>
            <textarea v-model="direction" rows="3"
                      placeholder="用自然语言描述想要的因子方向与经济含义"></textarea>
          </label>
          <div class="param-row">
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
              <span>自检模式（不调 LLM，秒级）</span>
            </label>
          </div>
          <div class="run-actions">
            <button class="btn-primary" :disabled="running" @click="start">
              {{ running ? '挖掘中…' : '开始挖掘' }}
            </button>
            <button class="btn-ghost" :disabled="!running" @click="stopPolling">停止轮询</button>
            <span class="status" :class="statusTone">{{ status }}</span>
          </div>
          <!-- 验收标准摊开写：agent 的产出要能被人一眼判断"算不算成功" -->
          <p class="hint tiny">
            验收标准：因子与现有字段的 <code>max|corr|</code> 不超过阈值、BH-FDR 通过、
            IC 方向与经济学解释一致；不达标的因子照样入档（供复核），但不会被推荐入池。
          </p>
        </div>

        <div class="panel trace">
          <h6>
            执行轨迹
            <span class="dim tiny">{{ messages.length }} 步</span>
            <span class="dim tiny" v-if="outDir">产物 <code>{{ rel(outDir) }}</code></span>
          </h6>
          <div ref="chatRef" class="chat">
            <div v-if="!messages.length" class="empty">
              在左边新建/选择一个会话，或直接点「开始挖掘」。
            </div>
            <div v-for="(m, i) in messages" :key="i" class="step" :class="roleClass(m)">
              <span class="step-role">{{ roleLabel(m) }}</span>
              <pre class="step-text">{{ m.text }}</pre>
            </div>
          </div>
        </div>
      </main>

      <!-- ③ 产物与指标（入池入口） -->
      <aside class="panel wb-col wb-artifacts">
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
      </aside>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import {
  createMiningSession, getMiningSession, listMiningSessions, registerStrategyCandidate,
  type MiningFactor, type MiningMessage, type MiningSessionSummary,
} from '@/api/client'
import PageHeader from '@/components/ui/PageHeader.vue'

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
let timer: ReturnType<typeof setInterval> | null = null

/** 因子行：统一 fdr_pass 的多种形态（true / "True" / ""），并按 |ICIR| 排序 */
const factorRows = computed(() => factors.value
  .map((f) => ({
    ...f,
    fdr: String(f.fdr_pass).toLowerCase() === 'true',
    _icir: Math.abs(Number(f.icir) || 0),
  }))
  .sort((a, b) => b._icir - a._icir))

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

function roleClass(m: MiningMessage) {
  if (m.role === 'assistant') return 'assistant'
  if (/失败|错误|Traceback|ERROR/.test(m.text)) return 'error'
  return 'system'
}

function roleLabel(m: MiningMessage) {
  if (m.role === 'assistant') return 'AGENT'
  if (/失败|错误|Traceback|ERROR/.test(m.text)) return 'ERROR'
  return 'ENV'
}

function short(text?: string) {
  const value = (text || '').trim()
  return value.length > 26 ? `${value.slice(0, 26)}…` : value || '(无方向)'
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
async function poolFactor(f: MiningFactor & { fdr?: boolean }) {
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
      summary: `${f.reason || ''} 表达式：${f.expression || ''}`.trim(),
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

onMounted(refreshHistory)
</script>

<style scoped>
/* 三栏 agent 工作台：会话 | 任务+轨迹 | 产物 */
.workbench { display: grid; grid-template-columns: 240px minmax(0, 1fr) 380px; gap: var(--sp-3); align-items: start; }
.wb-col { min-width: 0; }
.wb-sessions, .wb-artifacts { position: sticky; top: var(--sp-3); }
.panel h6 { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.new-btn { width: 100%; margin-bottom: var(--sp-2); }
.session-list { list-style: none; margin: 0; padding: 0; max-height: 64vh; overflow: auto; }
.session-list li { display: flex; gap: 6px; align-items: flex-start; padding: 5px 6px;
  border-radius: var(--r-2); cursor: pointer; }
.session-list li:hover { background: rgba(255, 255, 255, 0.04); }
.session-list li.active { background: rgba(255, 209, 102, 0.1); }
.s-main { display: flex; flex-direction: column; min-width: 0; line-height: 1.35; }
.s-dir { font-size: var(--fs-sm); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dot { width: 7px; height: 7px; border-radius: 50%; margin-top: 6px; flex-shrink: 0; background: var(--text-dim); }
.dot.ok { background: var(--accent-down); }
.dot.busy { background: var(--accent-gold); }
.dot.bad { background: var(--accent-up); }

.field { display: flex; flex-direction: column; gap: 3px; }
.field > span { font-size: var(--fs-xs); color: var(--text-muted); }
.field textarea { background: var(--bg-input); border: 1px solid var(--border-color);
  border-radius: var(--r-2); color: var(--text-primary); padding: 6px 8px; resize: vertical; }
.param-row { display: flex; gap: var(--sp-3); flex-wrap: wrap; margin-top: var(--sp-2); }
.param { display: flex; flex-direction: column; gap: 2px; font-size: var(--fs-xs); color: var(--text-muted); }
.param input, .param select { background: var(--bg-input); border: 1px solid var(--border-color);
  border-radius: var(--r-2); color: var(--text-primary); padding: 4px 6px; width: 100px; }
.param.check { flex-direction: row; align-items: center; gap: 5px; align-self: flex-end; padding-bottom: 4px; }
.run-actions { display: flex; align-items: center; gap: var(--sp-2); margin-top: var(--sp-3); flex-wrap: wrap; }
.status { font-family: var(--font-mono); font-size: var(--fs-xs); color: var(--text-muted); }
.status.ok { color: var(--accent-down); }
.status.bad { color: var(--accent-up); }
.status.busy { color: var(--accent-gold); }
.link-btn { text-decoration: none; display: inline-flex; align-items: center; }

.trace { margin-top: var(--sp-3); }
.chat { max-height: 56vh; overflow: auto; display: flex; flex-direction: column; gap: 4px;
  font-size: var(--fs-sm); }
.step { display: flex; gap: 6px; align-items: flex-start; }
.step-role { flex-shrink: 0; width: 48px; font-family: var(--font-mono); font-size: 10px;
  color: var(--text-dim); padding-top: 2px; }
.step-text { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; font-family: var(--font-mono);
  font-size: var(--fs-xs); line-height: 1.5; color: var(--text-muted); }
.step.assistant .step-role { color: var(--accent-gold); }
.step.assistant .step-text { color: var(--text-primary); }
.step.error .step-role, .step.error .step-text { color: var(--accent-up); }

.pool-actions { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; margin-bottom: var(--sp-2); }
.btn-xs { padding: 1px 6px; font-size: 10px; }
.factor-list { list-style: none; margin: 0; padding: 0; max-height: 52vh; overflow: auto;
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
@media (max-width: 1200px) {
  .workbench { grid-template-columns: 1fr; }
  .wb-sessions, .wb-artifacts { position: static; }
  .session-list, .factor-list, .chat { max-height: 40vh; }
}
</style>
