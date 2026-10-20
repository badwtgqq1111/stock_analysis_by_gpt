<template>
  <div class="page">
    <PageHeader
      title="自动化进度"
      description="每日生产（launchd 定时）当前的阶段、耗时与预计剩余时间。数据来自流水线自己写的 <code>run.log</code> 与各任务的台账，前端只做展示、不重算。"
    >
      <template #actions>
        <span class="dim tiny">每 {{ pollSeconds }}s 自动刷新</span>
        <span class="dim tiny">{{ status?.generated_at || '—' }}</span>
        <button class="btn-ghost" :disabled="loading" @click="reload">立即刷新</button>
      </template>
    </PageHeader>

    <div class="panel trigger-panel">
      <div class="trigger-row">
        <div>
          <h6>手动触发全链路</h6>
          <p class="hint tiny">
            使用与 launchd 相同的 <code>run_daily_production.sh</code>；日期默认取最新行情交易日，
            可改成漏跑日期补跑。已有运行/锁时会拒绝，不会重复启动。
          </p>
        </div>
        <div class="trigger-controls">
          <input v-model="triggerDate" type="date" :disabled="triggering || isRunning" />
          <button class="btn-primary" :disabled="triggering || isRunning" @click="triggerFull">
            {{ triggering ? '启动中…' : '手动触发全链路' }}
          </button>
        </div>
      </div>
      <p v-if="triggerMessage" class="hint tiny ok-text">{{ triggerMessage }}</p>
      <p v-if="triggerError" class="hint tiny warn-text">{{ triggerError }}</p>
    </div>

    <StateBlock v-if="error && !status" state="error" title="取不到自动化状态" :detail="error" :retry="reload" />
    <StateBlock v-else-if="!status" state="loading" title="加载中…" />

    <template v-else>
      <!-- ── Web 页面新鲜度：自动化跑完后每个页面是否都是最新交易日 ── -->
      <div class="panel" v-if="freshness?.available">
        <h6>
          Web 页面新鲜度
          <span class="pill" :class="freshness.ok ? 'ok' : 'bad'">
            {{ freshness.ok ? '全部达标' : `不达标 ${freshness.stale?.length || 0} 项` }}
          </span>
          <span class="pill dim tiny">基准交易日 {{ freshness.latest_trade_date || '—' }}</span>
          <span class="dim tiny" v-if="freshness.generated_at">体检于 {{ freshness.generated_at }}</span>
        </h6>
        <div class="table-scroll">
          <table class="data-table">
            <thead><tr><th class="text-left">页面</th><th>口径</th><th>数据日期</th><th>状态</th>
              <th class="text-left">读的产物</th></tr></thead>
            <tbody>
              <tr v-for="row in freshness.rows || []" :key="row.page + row.artifact">
                <td class="text-left">{{ row.page }}</td>
                <td class="tiny dim">{{ row.kind }}</td>
                <td class="mono">{{ row.date || '—' }}</td>
                <td>
                  <span v-if="row.ok === true" class="tag ok">✅</span>
                  <span v-else-if="row.ok === false" class="tag bad">❌</span>
                  <span v-else class="tag dim">—</span>
                </td>
                <td class="text-left tiny dim" :title="row.artifact">{{ row.note }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="hint tiny">
          `eq` 口径要求"数据日期 == 最新交易日"，`ge` 用于按运行日命名的产物（流水线报告）；
          `event/live` 只报告不判死。不达标会同时写进 <code>output/data_quality/web_freshness.md</code> 并触发通知。
        </p>
      </div>

      <!-- ── 总览 ───────────────────────────────────────────── -->
      <div class="panel">
        <div class="overview">
          <div class="ov-main">
            <div class="ov-line">
              <span class="state-dot" :class="stateTone" />
              <b class="ov-state">{{ stateText }}</b>
              <span class="dim">交易日 {{ status.trade_date || '—' }}</span>
              <span v-if="status.daily.started_at" class="dim">起于 {{ hhmmss(status.daily.started_at) }}</span>
              <span v-if="status.daily.elapsed_seconds != null" class="dim">
                已跑 {{ human(status.daily.elapsed_seconds) }}
              </span>
              <span v-if="status.daily.lock_pid" class="pill">锁 pid={{ status.daily.lock_pid }}</span>
              <span v-else-if="status.daily.lock_stale_pid" class="pill dim"
                    title="进程已退出但锁目录还在（trap 未执行）；不影响下次运行，脚本会按「持有者是否存活」接管">
                陈旧锁 pid={{ status.daily.lock_stale_pid }}
              </span>
              <span v-if="status.daily.finished_at" class="dim">结束于 {{ hhmmss(status.daily.finished_at) }}</span>
            </div>
            <div class="progress-track">
              <div class="progress-fill" :class="stateTone" :style="{ width: (status.daily.progress * 100).toFixed(1) + '%' }" />
            </div>
            <div class="ov-sub dim tiny">
              {{ status.daily.done_stages }} / {{ status.daily.total_stages }} 个阶段完成
              <span v-if="runningStage">· 当前：<b>{{ runningStage.name }}</b>
                <span v-if="runningStage.progress"> {{ runningStage.progress.percent }}%（{{ runningStage.progress.current }}/{{ runningStage.progress.total }}）</span>
              </span>
              <span v-else-if="status.daily.current" class="dim">
                · 当前：<b>{{ status.daily.current.file }}</b>
                <template v-if="status.daily.current.percent != null"> {{ status.daily.current.percent }}%</template>
              </span>
              <span v-if="status.daily.eta_seconds != null">· 预计剩余 <b>{{ human(status.daily.eta_seconds) }}</b>
                （约 {{ etaClock }} 完成）</span>
            </div>
            <div v-if="status.today && !status.today_started && !isRunning" class="dim tiny">
              今天（{{ status.today }}）还没跑进主阶段<template v-if="status.today_attempts">：
              已触发 {{ status.today_attempts }} 次<template v-if="status.today_last_error">，最近一次失败于
              <b>{{ status.today_last_error }}</b></template></template>；
              定时任务默认 18:30 / 20:00 / 21:30（当日成功后写 production.done，后续触发自动跳过）。
              下面展示的是最近一次已完成的运行。
            </div>
            <div v-if="status.daily.failed_stage" class="warn-text tiny">
              最近一次失败在此阶段：{{ status.daily.failed_stage }}（后续触发会自动重试）
            </div>
            <div v-if="status.last_attempt?.outcome === 'preflight_failed' && !isRunning" class="warn-text tiny">
              最近一次手动触发（{{ hhmmss(status.last_attempt.started_at) }}，交易日 {{ status.last_attempt.trade_date }}，
              pid={{ status.last_attempt.pid }}）停在预检：<b>{{ status.last_attempt.error }}</b>，
              主链路未启动，进度条不会变化。原因见同目录 <code>preflight.log</code>。
            </div>
          </div>
        </div>
      </div>

      <!-- ── 阶段明细 ───────────────────────────────────────── -->
      <div class="panel">
        <h6>阶段明细</h6>
        <div v-for="g in groups" :key="g.key" class="group">
          <div class="group-head">
            <span class="tag" :class="g.tone">{{ g.label }}</span>
            <span class="dim tiny">{{ g.summary }}</span>
          </div>
          <table class="data-table">
            <thead>
              <tr>
                <th class="text-left">阶段</th><th class="text-left">状态</th>
                <th>耗时</th><th class="text-left">进度</th><th>中位耗时</th><th class="text-left">完成于</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="s in g.stages" :key="s.name" :class="{ 'cur-stage': s.status === 'running' }">
                <td class="text-left">
                  {{ s.label }}
                  <span v-if="s.label !== s.name" class="dim tiny mono">{{ s.name }}</span>
                </td>
                <td class="text-left">
                  <span class="tag" :class="statusTone(s.status)">{{ statusText(s.status) }}</span>
                  <span v-if="s.note" class="dim tiny">（{{ s.note }}）</span>
                </td>
                <td>{{ s.seconds != null ? human(s.seconds) : '—' }}</td>
                <td class="text-left">
                  <template v-if="s.progress">
                    <div class="mini-track"><div class="mini-fill" :style="{ width: s.progress.percent + '%' }" /></div>
                    <span class="tiny dim">{{ s.progress.percent }}% · 剩 {{ human(s.progress.eta_s) }}</span>
                  </template>
                  <span v-else class="dim">—</span>
                </td>
                <td class="dim">{{ s.median_seconds != null ? human(s.median_seconds) : '—' }}</td>
                <td class="text-left dim tiny">{{ hhmmss(s.finished_at) || '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- ── 数据补齐 / 体检 / 调度器 ───────────────────────── -->
      <div class="cards">
        <div class="panel">
          <h6>数据补齐进度（只看产物，不看任务退出码）</h6>
          <div class="stat-grid">
            <StatTile label="分时 5min 最后一轮" :value="intradayRows[0].value" :sub="intradayRows[0].sub" tone="gold" />
            <StatTile label="本轮会话已处理" :value="intradayRows[1].value" :sub="intradayRows[1].sub" />
            <StatTile label="实测速率" :value="intradayRows[2].value" :sub="intradayRows[2].sub" />
            <StatTile label="财务 / 财报回补" :value="finRows[0].value" :sub="finRows[0].sub" />
          </div>
          <p class="hint tiny">
            分时"已补齐到最新交易日的只数"要读 4,500+ 个 parquet（约 15 秒），不放进轮询路径；
            它以 <b>数据完整性体检</b> 的快照为准（见下）。
          </p>
        </div>

        <div class="panel">
          <h6>数据完整性体检
            <span v-if="status.completeness?.age_seconds != null" class="dim tiny">
              （快照 {{ human(status.completeness.age_seconds) }} 前）
            </span>
          </h6>
          <template v-if="status.completeness?.checks?.length">
            <table class="data-table">
              <thead><tr><th class="text-left">数据</th><th>频率</th><th class="text-left">最新</th><th class="text-left">状态</th></tr></thead>
              <tbody>
                <tr v-for="c in status.completeness.checks" :key="c.key">
                  <td class="text-left">{{ c.name }}</td>
                  <td class="dim tiny">{{ cadenceText(c.cadence) }}</td>
                  <td class="text-left mono tiny">{{ c.latest || '—' }}</td>
                  <td class="text-left">
                    <span class="tag" :class="c.ok ? 'ok' : 'bad'">{{ c.ok ? '达标' : '未达标' }}</span>
                    <div class="dim tiny detail">{{ c.detail }}</div>
                  </td>
                </tr>
              </tbody>
            </table>
          </template>
          <div v-else class="empty">还没有体检结果，跑一次 <code>scripts/data_completeness_report.py</code></div>
        </div>

        <div class="panel">
          <h6>调度器（launchd）</h6>
          <table class="data-table">
            <thead><tr><th class="text-left">任务</th><th>PID</th><th class="text-left">状态</th><th class="text-left">上次退出码</th></tr></thead>
            <tbody>
              <tr v-for="a in status.agents" :key="a.label">
                <td class="text-left mono tiny">{{ a.label }}</td>
                <td>{{ a.pid ?? '—' }}</td>
                <td class="text-left">
                  <span class="tag" :class="a.running ? 'ok' : ''">{{ a.running ? '运行中' : '已加载（空闲）' }}</span>
                </td>
                <td class="text-left dim">{{ a.last_exit }}</td>
              </tr>
            </tbody>
          </table>
          <p class="hint tiny">只列出已加载的 <code>com.quant.*</code> 任务；没有出现在这里 = 未加载（不会自动跑）。</p>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { getAutomationStatus, getWebFreshness, triggerAutomation } from '@/api/client'
import type { AutomationStage, AutomationStatus } from '@/types/api'
import PageHeader from '@/components/ui/PageHeader.vue'
import StateBlock from '@/components/ui/StateBlock.vue'
import StatTile from '@/components/ui/StatTile.vue'

const status = ref<AutomationStatus | null>(null)
// Web 页面新鲜度体检（每日流水线最后一步的产物）
const freshness = ref<any>(null)
const error = ref('')
const loading = ref(false)
const triggering = ref(false)
const triggerDate = ref('')
const triggerMessage = ref('')
const triggerError = ref('')
const pollSeconds = 5
let timer: ReturnType<typeof setInterval> | null = null

async function reload() {
  loading.value = true
  try {
    status.value = await getAutomationStatus()
    if (!triggerDate.value) {
      triggerDate.value = status.value.latest_market_date || status.value.trade_date || status.value.today || ''
    }
    // 体检产物独立于运行状态：加载失败不影响主面板
    try { freshness.value = await getWebFreshness() } catch { /* 忽略 */ }
    error.value = ''
  } catch (e) {
    error.value = String(e)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void reload()
  timer = setInterval(() => { if (!document.hidden) void reload() }, pollSeconds * 1000)
})
onUnmounted(() => { if (timer) clearInterval(timer) })

const isRunning = computed(() => status.value?.daily.state === 'running')

async function triggerFull() {
  if (!triggerDate.value) {
    triggerError.value = '请选择要触发的交易日'
    return
  }
  triggering.value = true
  triggerMessage.value = ''
  triggerError.value = ''
  try {
    const result = await triggerAutomation({ trade_date: triggerDate.value, force: true })
    triggerMessage.value = result.mode === 'historical'
      ? `已触发 ${result.trade_date} 历史补跑（数据→特征→选股→台账），pid=${result.pid}，日志：${result.log}`
      : `已触发 ${result.trade_date} 全链路，pid=${result.pid}，日志：${result.log}`
    window.setTimeout(() => { void reload() }, 1500)
  } catch (e) {
    triggerError.value = String(e)
  } finally {
    triggering.value = false
  }
}

const runningStage = computed<AutomationStage | undefined>(
  () => status.value?.daily.stages.find((s) => s.status === 'running'),
)

const stateText = computed(() => ({
  running: '正在跑', finished: '已完成', failed: '有阶段失败', idle: '空闲（未在跑）',
}[status.value?.daily.state || 'idle']))
const stateTone = computed(() => ({
  running: 'warn', finished: 'ok', failed: 'bad', idle: '',
}[status.value?.daily.state || 'idle']))

const etaClock = computed(() => {
  const eta = status.value?.daily.eta_seconds
  if (eta == null) return '—'
  const t = new Date(Date.now() + eta * 1000)
  return `${String(t.getHours()).padStart(2, '0')}:${String(t.getMinutes()).padStart(2, '0')}`
})

const GROUPS = [
  { key: 'data', label: '数据补齐（选股前）', tone: 'info' },
  { key: 'main', label: '主阶段', tone: 'gold' },
  { key: 'tail', label: '收尾', tone: '' },
] as const

const groups = computed(() =>
  GROUPS.map((g) => {
    const stages = (status.value?.daily.stages || []).filter((s) => s.group === g.key)
    // "本次未包含"（not_in_run）不能计入分母 —— 否则会显示成"0/3 完成"，
    // 让人以为这三步卡住了，实际是本轮脚本里根本没有它们（旧脚本 / 阶段被关掉）。
    const counted = stages.filter((s) => s.status !== 'not_in_run')
    const done = counted.filter((s) => s.status === 'ok').length
    const summary = counted.length
      ? `${done}/${counted.length} 完成${stages.length > counted.length ? `（另 ${stages.length - counted.length} 项本次未包含）` : ''}`
      : '本次未包含'
    return { ...g, stages, done, total: counted.length, summary }
  }).filter((g) => g.stages.length > 0),
)

function statusText(s: AutomationStage['status']) {
  return { ok: '完成', running: '进行中', pending: '待执行', failed: '失败', not_in_run: '本次未包含' }[s]
}
function statusTone(s: AutomationStage['status']) {
  return { ok: 'ok', running: 'gold', pending: '', failed: 'bad', not_in_run: '' }[s]
}
function cadenceText(c: string) {
  return { daily: '每日', report: '按披露截止日', monthly: '每月', event: '事件驱动' }[c] || c
}

/** 秒 → "1h23m" / "45m" / "12s" */
function human(sec: number | null | undefined): string {
  if (sec == null) return '—'
  const s = Math.max(0, Math.round(sec))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m${String(s % 60).padStart(2, '0')}s`
  return `${Math.floor(m / 60)}h${String(m % 60).padStart(2, '0')}m`
}
/** "2026-10-08 19:30:11" → "19:30:11" */
function hhmmss(text?: string | null) {
  if (!text) return ''
  const m = String(text).match(/(\d{2}:\d{2}:\d{2})/)
  return m ? m[1] : String(text)
}

const intradayRows = computed(() => {
  const i = status.value?.intraday
  if (!i?.available) return [{ value: '—', sub: '无台账' }, { value: '—', sub: '' }, { value: '—', sub: '' }]
  const updated = i.updated_at ? hhmmss(i.updated_at.replace('T', ' ')) : '—'
  return [
    { value: updated, sub: `台账 ${i.done_total}/${i.target_total}${i.pending_total != null ? `，待补 ${i.pending_total}` : ''}` },
    { value: String(i.session_processed ?? '—'), sub: `累计隔离 ${i.quarantined ?? 0} 只` },
    { value: i.rate_per_sec ? `${i.rate_per_sec.toFixed(3)} 只/秒` : '—',
      sub: i.eta_hours != null ? `预计 ${i.eta_hours.toFixed(2)} 小时补完` : '本会话实测' },
  ]
})

const finRows = computed(() => {
  const f = status.value?.financials
  if (!f?.available) return [{ value: '—', sub: '无台账' }]
  return [{ value: `${f.done_total}/${f.target_total}`, sub: `隔离 ${f.quarantined ?? 0} 只 · ${hhmmss(f.updated_at?.replace('T', ' '))}` }]
})
</script>

<style scoped>
.overview { display: flex; gap: var(--sp-4); }
.trigger-panel { margin-bottom: var(--sp-3); }
.trigger-row { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--sp-3); flex-wrap: wrap; }
.trigger-controls { display: flex; align-items: center; gap: var(--sp-2); }
.trigger-controls input { width: 150px; }
.ok-text { color: var(--state-ok); }
.ov-main { flex: 1; min-width: 0; }
.ov-line { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; margin-bottom: var(--sp-2); }
.ov-state { font-size: var(--fs-lg); }
.state-dot { width: 9px; height: 9px; border-radius: 50%; background: var(--text-dim); }
.state-dot.ok { background: var(--state-ok); }
.state-dot.warn { background: var(--state-warn); }
.state-dot.bad { background: var(--state-bad); }

.progress-track { height: 10px; border-radius: var(--r-pill); background: var(--bg-input); overflow: hidden; }
.progress-fill { height: 100%; background: var(--accent-blue); transition: width 0.4s; }
.progress-fill.ok { background: var(--state-ok); }
.progress-fill.warn { background: var(--state-warn); }
.progress-fill.bad { background: var(--state-bad); }
.ov-sub { margin-top: 6px; line-height: 1.7; }

.group { margin-bottom: var(--sp-4); }
.group-head { display: flex; align-items: center; gap: var(--sp-2); margin-bottom: 6px; }
.data-table tr.cur-stage { background: rgba(255, 209, 102, 0.07); }
.mini-track { height: 5px; width: 120px; border-radius: var(--r-pill); background: var(--bg-input); overflow: hidden; display: inline-block; vertical-align: middle; margin-right: 6px; }
.mini-fill { height: 100%; background: var(--accent-gold); }

.detail { white-space: normal; line-height: 1.5; max-width: 260px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: var(--sp-3); }
@media (max-width: 720px) { .cards { grid-template-columns: minmax(0, 1fr); } .detail { max-width: none; } }
</style>
