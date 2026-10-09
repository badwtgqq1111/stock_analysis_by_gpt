<template>
  <div class="page">
    <h4 class="page-title">LLM 因子挖掘 · 对话面板</h4>
    <p class="hint">
      后端直连 <code>scripts/llm_factor_mining.py</code>（DeepSeek）。
      评估口径：月频横截面秩 IC / ICIR / Newey-West t / BH-FDR(q=0.05)。
      参数面板加载约占 2GB 内存，服务端串行执行，一次只跑一个会话。
    </p>

    <div class="form-grid">
      <label class="full">
        <span>挖掘方向</span>
        <textarea v-model="direction" rows="3" placeholder="用自然语言描述想要的因子方向与经济含义"></textarea>
      </label>
      <label>
        <span>轮数</span>
        <input v-model.number="rounds" type="number" min="1" max="10" />
      </label>
      <label>
        <span>每轮候选数</span>
        <input v-model.number="proposals" type="number" min="1" max="20" />
      </label>
      <label>
        <span>模型</span>
        <select v-model="model">
          <option value="deepseek-flash">deepseek-flash</option>
          <option value="deepseek-chat">deepseek-chat</option>
          <option value="deepseek-reasoner">deepseek-reasoner</option>
        </select>
      </label>
      <label>
        <span>会话 ID</span>
        <input :value="sessionId || '（未开始）'" readonly />
      </label>
    </div>

    <div class="controls">
      <button class="btn-primary" :disabled="running" @click="start">开始挖掘</button>
      <button class="btn-ghost" :disabled="!running" @click="stopPolling">停止轮询</button>
      <label class="inline-check">
        <input v-model="dryRun" type="checkbox" /> 自检模式（不调 LLM，秒级）
      </label>
      <span class="status" :class="status">{{ status }}</span>
    </div>

    <div class="panel">
      <h6>
        历史会话（落盘持久化：<code>output/factor_mining_sessions/</code>，重启不丢）
        <button class="btn-ghost tiny" @click="refreshHistory">刷新</button>
      </h6>
      <div v-if="!history.length" class="empty">暂无历史会话。</div>
      <table v-else class="data-table history">
        <thead>
          <tr><th>创建时间</th><th>状态</th><th>方向</th><th>消息</th><th>因子</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="h in history" :key="h.session_id" :class="{ current: h.session_id === sessionId }">
            <td>{{ h.created_at || '—' }}</td>
            <td>{{ h.status }}</td>
            <td class="dir">{{ h.direction }}</td>
            <td>{{ h.n_messages }}</td>
            <td>{{ h.n_factors }}</td>
            <td><button class="btn-ghost tiny" @click="loadSession(h.session_id)">载入</button></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h6>对话 / 流式日志</h6>
      <div ref="chatRef" class="chat">
        <div v-if="!messages.length" class="empty">输入方向后点「开始挖掘」。</div>
        <div v-for="(m, i) in messages" :key="i" class="msg" :class="roleClass(m)">
          <div class="bubble">{{ m.text }}</div>
        </div>
      </div>
    </div>

    <div class="panel">
      <h6>本轮入档因子（按 |ICIR| 排序，仅列通过 BH-FDR 的）</h6>
      <div v-if="!factors.length" class="empty">暂无。</div>
      <table v-else class="data-table">
        <thead>
          <tr>
            <th>因子</th><th>IC</th><th>ICIR</th><th>NW t</th>
            <th>正号月</th><th>maxρ</th><th>FDR</th><th>表达式</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="f in factors" :key="f.name">
            <td>{{ f.name }}</td>
            <td>{{ num(f.ic_mean, 4) }}</td>
            <td>{{ num(f.icir, 3) }}</td>
            <td>{{ num(f.nw_t, 2) }}</td>
            <td>{{ pct(f.pos_ratio) }}</td>
            <td>{{ num(f.max_corr_with_fields, 2) }}</td>
            <td>{{ String(f.fdr_pass).toLowerCase() === 'true' ? '✅' : '—' }}</td>
            <td class="expr">{{ f.expression }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onMounted, ref } from 'vue'
import {
  createMiningSession, getMiningSession, listMiningSessions,
  type MiningMessage, type MiningFactor, type MiningSessionSummary,
} from '@/api/client'

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
const chatRef = ref<HTMLElement | null>(null)
const history = ref<MiningSessionSummary[]>([])
let timer: ReturnType<typeof setInterval> | null = null

async function refreshHistory() {
  try {
    history.value = await listMiningSessions()
  } catch {
    /* 历史列表拉取失败不影响挖掘 */
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
    if (data.request?.direction) direction.value = data.request.direction
    await nextTick()
    if (chatRef.value) chatRef.value.scrollTop = chatRef.value.scrollHeight
  } catch (e: any) {
    status.value = 'load-failed：' + (e?.message || e)
  }
}

function roleClass(m: MiningMessage) {
  if (m.role === 'assistant') return 'assistant'
  if (/失败|错误|Traceback|ERROR/.test(m.text)) return 'error'
  return 'system'
}

function num(v: any, d: number) {
  const n = Number(v)
  return Number.isFinite(n) ? n.toFixed(d) : '—'
}

function pct(v: any) {
  const n = Number(v)
  return Number.isFinite(n) ? (n * 100).toFixed(1) + '%' : '—'
}

async function poll() {
  if (!sessionId.value) return
  try {
    const data = await getMiningSession(sessionId.value)
    messages.value = data.messages || []
    factors.value = data.factors || []
    status.value = data.status
    await nextTick()
    if (chatRef.value) chatRef.value.scrollTop = chatRef.value.scrollHeight
    if (data.status === 'done' || data.status === 'failed') {
      finish()
      if (data.error) messages.value = [...messages.value, { role: 'system', text: data.error }]
      refreshHistory()
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
    refreshHistory()
  } catch (e: any) {
    running.value = false
    status.value = 'failed：' + (e?.message || e)
  }
}

onMounted(refreshHistory)
</script>

<style scoped>
/* 页面专属：会话列表 + 主区两列 */
.mining-layout { display: grid; grid-template-columns: 260px 1fr; gap: var(--sp-3); }
.session-list { max-height: 620px; overflow: auto; }
.session-item { padding: 6px 8px; border-radius: var(--r-2); cursor: pointer; font-size: var(--fs-sm); }
.session-item:hover { background: rgba(255, 255, 255, 0.04); }
.session-item.active { background: rgba(255, 209, 102, 0.1); color: var(--accent-gold); }
.data-table.history td:nth-child(4), .data-table.history td:nth-child(5),
.data-table th:nth-child(4), .data-table th:nth-child(5) { width: 60px; }
button.tiny { padding: 2px 10px; font-size: var(--fs-xs); margin-left: var(--sp-2); }
@media (max-width: 1100px) { .mining-layout { grid-template-columns: 1fr; } }
</style>
