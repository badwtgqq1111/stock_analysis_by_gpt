<template>
  <div class="page">
    <PageHeader
      title="投研执行台"
      description="一个入口管三件事：<b>跑得怎么样</b>（两族状态 + 运行轨迹 + 门槛）、<b>挖得怎么样</b>（挖掘会话 + 因子库）、<b>它怎么说话</b>（提示词/人设）。数据全部来自产物，前端只展示、不重算。"
    >
      <template #actions>
        <span class="pill gold tiny" title="在「提示词」标签页里可改">角色：{{ overview?.profile?.role || '—' }}</span>
        <span class="dim tiny" v-if="overview">生成于 {{ overview.generated_at }}</span>
        <button class="btn-ghost" :disabled="loading" @click="reload">刷新</button>
      </template>
    </PageHeader>

    <nav class="tabs">
      <button
        v-for="item in tabs"
        :key="item.key"
        class="tab"
        :class="{ active: tab === item.key }"
        @click="setTab(item.key)"
      >
        {{ item.label }}
        <span class="pill dim tiny">{{ item.hint }}</span>
      </button>
    </nav>

    <StateBlock v-if="error && !overview" state="error" title="取不到投研执行台数据" :detail="error" :retry="reload" />
    <StateBlock v-else-if="!overview" state="loading" title="加载中…" />

    <template v-else>
      <!-- ─────────────── ① 执行台：两族状态 + 运行轨迹 + 门槛 ─────────────── -->
      <template v-if="tab === 'console'">
        <div class="layer-strip">
          <div v-for="layer in overview.layers" :key="layer.key" class="layer-tile">
            <div class="lt-head">
              <span class="dot" :class="tone(layer.status)" />
              <b>{{ layer.label }}</b>
              <span class="pill dim tiny">{{ layer.families.join(' / ') }}</span>
            </div>
            <p class="lt-detail tiny">{{ layer.detail }}</p>
          </div>
        </div>

        <div class="console-grid">
          <section class="panel">
            <h6>
              两族
              <span class="dim tiny">点一条看入口 / 产物</span>
            </h6>
            <div v-for="family in overview.families" :key="family.key" class="family">
              <div class="family-head">
                <span class="tag" :class="family.key === 'A' ? 'gold' : 'info'">{{ family.key }} 族</span>
                <b>{{ family.label }}</b>
              </div>
              <p class="hint tiny">{{ family.hint }}</p>
              <div v-for="branch in family.branches" :key="branch.key" class="branch">
                <div class="branch-label">
                  {{ branch.label }}
                  <span v-if="branch.items.length" class="dim tiny">· {{ branch.items.length }} 条</span>
                  <span v-else class="dim tiny">· 研究线（无在册策略）</span>
                </div>
                <button
                  v-for="item in branch.items"
                  :key="item.id"
                  class="item"
                  :class="{ active: item.id === selectedId }"
                  @click="selectedId = item.id"
                >
                  <span class="dot" :class="itemTone(item)" />
                  <span class="item-name">{{ item.name }}</span>
                  <span class="pill dim tiny">{{ item.stage_label || item.stage }}</span>
                  <span class="dim tiny mono">{{ item.latest_date || '—' }}</span>
                </button>
                <p v-if="branch.note" class="hint tiny">{{ branch.note }}</p>
                <p v-for="path in branch.evidence" :key="path" class="hint tiny mono">· {{ path }}</p>
              </div>
            </div>

            <div v-if="selectedItem" class="selected">
              <div class="sel-head">
                选中 <b>{{ selectedItem.name }}</b>
                <span class="pill dim tiny">{{ selectedItem.stage_label || selectedItem.stage }}</span>
              </div>
              <dl class="kv tiny">
                <dt>入口</dt><dd class="mono">{{ selectedItem.entry || '—' }}</dd>
                <dt>来源</dt><dd class="mono">{{ selectedItem.selection_source || '—' }}</dd>
                <dt>最新</dt>
                <dd class="mono">{{ selectedItem.latest_date || '—' }} · {{ selectedItem.n_picks ?? '—' }} 只</dd>
              </dl>
              <p class="hint tiny">{{ selectedItem.summary }}</p>
            </div>
          </section>

          <div class="right-col">
            <section class="panel">
              <h6>
                运行轨迹
                <span class="pill dim tiny">{{ runs.length }} 条</span>
                <span class="dim tiny">scripts/agent_run.py → output/agent_runs/</span>
              </h6>
              <p class="hint tiny">
                <b>与「因子挖掘」的关系</b>：挖掘会话跑完会自动生成一条轨迹（<code>agent_run.py from-session</code>）落在同一条时间线上
                ——「因子挖掘」是发起与复看会话的地方，「执行台」是看这条轨迹过了哪一档门槛、以及怎么回滚的地方。
              </p>
              <div class="runs">
                <button
                  v-for="run in runs"
                  :key="run.run_id"
                  class="run"
                  :class="{ active: run.run_id === current?.run_id }"
                  @click="openRun(run.run_id)"
                >
                  <span class="dot" :class="runTone(run.status)" />
                  <span class="run-id mono">{{ run.run_id }}</span>
                  <span class="pill tiny" :class="run.kind === 'mining' ? 'gold' : 'dim'">{{ run.kind }}</span>
                  <span class="dim tiny">{{ run.started_at }} · {{ fmtDuration(run.duration_seconds) }}</span>
                  <span class="pill tiny" :class="(run.totals?.n_failed || 0) > 0 ? 'bad' : 'ok'">
                    {{ run.totals?.n_ok || 0 }}/{{ run.totals?.n_stages || 0 }} 阶段
                  </span>
                  <span v-if="run.session_id" class="go-session" title="打开这次挖掘的会话"
                        @click.stop="openMiningSession(run.session_id)">去会话 →</span>
                </button>
                <p v-if="!runs.length" class="hint tiny">
                  还没有轨迹 —— 跑一次
                  <code>uv run python scripts/agent_run.py run --kind smoke -- &lt;你的命令&gt;</code>
                  或 <code>summarize --log output/pipeline_reports/daily/&lt;date&gt;/run.log</code>。
                </p>
              </div>

              <template v-if="current">
                <div class="tiles">
                  <StatTile label="状态" :value="current.status" :tone="runTone(current.status)" :sub="`退出码 ${current.exit_status ?? '—'}`" />
                  <StatTile label="墙钟用时" :value="fmtDuration(current.duration_seconds)" :sub="budgetText" />
                  <StatTile
                    label="阶段"
                    :value="`${current.totals.n_ok}/${current.totals.n_stages}`"
                    :sub="`failed ${current.totals.n_failed} · warn ${current.totals.n_warn}`"
                  />
                  <StatTile label="产物" :value="String(current.artifacts.length)" sub="轨迹登记到的路径" />
                </div>

                <div class="table-scroll">
                  <table class="data-table">
                    <thead>
                      <tr>
                        <th class="text-left">#</th>
                        <th class="text-left">阶段</th>
                        <th>状态</th>
                        <th>用时</th>
                        <th class="text-left">产物 / 说明</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-for="(stage, index) in current.stages" :key="stage.name + index">
                        <td class="dim">{{ index + 1 }}</td>
                        <td class="text-left mono">{{ stage.name }}</td>
                        <td><span class="tag" :class="tone(stage.status)">{{ stage.status }}</span></td>
                        <td class="mono">{{ stage.duration_seconds != null ? stage.duration_seconds + 's' : '—' }}</td>
                        <td class="text-left tiny dim">{{ stage.artifact || stage.reason || stage.evidence }}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>

                <dl v-if="current.session_id || current.kind === 'mining'" class="kv tiny source-link">
                  <dt>来源会话</dt>
                  <dd>
                    <code>{{ current.session_id || '—' }}</code>
                    <button v-if="current.session_id" class="btn-ghost btn-sm" @click="openMiningSession(current.session_id)">
                      打开挖掘会话 →
                    </button>
                  </dd>
                </dl>
                <div v-if="current.factors?.length" class="factor-chips">
                  <span class="dim tiny">本次入档因子</span>
                  <span v-for="f in current.factors" :key="f.name" class="pill dim tiny" :title="f.expression">
                    {{ f.name }} · IC {{ f.ic_mean?.toFixed(4) ?? '—' }}
                  </span>
                </div>

                <details class="rollback">
                  <summary>怎么回滚这次运行</summary>
                  <dl class="kv tiny">
                    <dt>动作</dt><dd>{{ current.rollback.action }}</dd>
                    <dt>命令</dt><dd class="mono">{{ current.rollback.cmd }}</dd>
                  </dl>
                  <p class="hint tiny">{{ current.rollback.note }}</p>
                  <p v-for="path in current.rollback.targets || []" :key="path" class="hint tiny mono">· {{ path }}</p>
                </details>
              </template>
            </section>

            <section class="panel">
              <h6>
                当前阶段清单（过了哪一档门槛）
                <span class="pill dim tiny">{{ gateRows.length }} 档</span>
                <span class="dim tiny">来自 {{ current?.run_id || '最近一次运行' }} 的门槛表</span>
              </h6>
              <div class="gate-list">
                <div v-for="gate in gateRows" :key="gate.name" class="gate">
                  <span class="tag" :class="tone(gate.status)">{{ gate.status }}</span>
                  <span class="gate-name mono">{{ gate.name }}</span>
                  <span class="dim tiny">{{ gate.label }}</span>
                  <p class="hint tiny" :title="gate.detail">{{ shorten(gate.detail, 150) }}</p>
                  <p v-if="gate.artifact" class="hint tiny mono" :title="gate.artifact">
                    · {{ shortPath(gate.artifact) }}
                  </p>
                </div>
                <p v-if="!gateRows.length" class="hint tiny">
                  还没有轨迹 ⇒ 没有门槛记录。每日自动化跑完会在末尾自动生成一条（<code>DAILY_AGENT_RUN=0</code> 可关）；
                  挖掘会话跑完也会自动生成一条（门槛是 BH-FDR 筛选 + 因子入库）。
                </p>
              </div>
            </section>
          </div>
        </div>
      </template>

      <!-- ─────────────── ② 因子挖掘：会话工作台 + 因子库 ─────────────── -->
      <template v-else-if="tab === 'mining'">
        <FactorMiningPanel />
        <FactorLibraryPanel />
      </template>

      <!-- ─────────────── ③ 提示词 / 人设 ─────────────── -->
      <template v-else>
        <AgentPromptPanel />
      </template>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getAgentOverview, getAgentRun, getAgentRuns } from '@/api/client'
import type { AgentFamilyItem, AgentOverview, AgentRunDetail, AgentRunSummary } from '@/types/api'
import PageHeader from '@/components/ui/PageHeader.vue'
import StateBlock from '@/components/ui/StateBlock.vue'
import StatTile from '@/components/ui/StatTile.vue'
import AgentPromptPanel from '@/components/AgentPromptPanel.vue'
import FactorMiningPanel from '@/components/FactorMiningPanel.vue'
import FactorLibraryPanel from '@/components/FactorLibraryPanel.vue'

const route = useRoute()
const router = useRouter()

// 三个标签页：执行台（跑得怎么样）/ 因子挖掘（挖得怎么样）/ 提示词（它怎么说话）
// 合并理由：这三个问题以前分散在两个页面（`/agent` + `/factor-mining`），
// 而"挖出来的因子"与"跑出来的轨迹"本来就是同一件事的两端（因子挖掘也是一种 agent run）。
const tabs = computed(() => [
  { key: 'console', label: '执行台', hint: `${overview.value?.runs.count ?? 0} 条轨迹` },
  { key: 'mining', label: '因子挖掘', hint: `${overview.value?.factor_library?.totals?.n ?? 0} 条因子/条件` },
  { key: 'prompt', label: '提示词 / 人设', hint: overview.value?.profile?.role || '—' },
])
const tab = ref<'console' | 'mining' | 'prompt'>('console')

function setTab(key: string) {
  tab.value = key as typeof tab.value
  void router.replace({ query: { ...route.query, tab: key } })
}

/** 执行台 → 因子挖掘：带着会话 id 跳过去（挖掘面板会自己把它选中） */
function openMiningSession(sessionId: string | null | undefined) {
  if (!sessionId) return
  tab.value = 'mining'
  void router.replace({ query: { tab: 'mining', session: sessionId } })
}

const overview = ref<AgentOverview | null>(null)
const runs = ref<AgentRunSummary[]>([])
const current = ref<AgentRunDetail | null>(null)
const error = ref('')
const loading = ref(false)
const selectedId = ref('')

async function reload() {
  loading.value = true
  try {
    const [ov, runList] = await Promise.all([getAgentOverview(), getAgentRuns(20)])
    overview.value = ov
    runs.value = runList.runs
    if (!selectedId.value && ov.families.length) {
      const first = ov.families[0]?.branches?.[0]?.items?.[0]
      if (first) selectedId.value = first.id
    }
    // 深链 `?run=<run_id>`（因子挖掘页的「轨迹 →」就是靠它跳过来的）
    const wanted = String(route.query.run || '')
    if (wanted) {
      await openRun(wanted)
    } else if (!current.value && runList.runs.length) {
      await openRun(runList.runs[0].run_id)
    }
    error.value = ''
  } catch (err) {
    error.value = String(err)
  } finally {
    loading.value = false
  }
}

async function openRun(runId: string) {
  try {
    current.value = await getAgentRun(runId)
  } catch (err) {
    error.value = `取轨迹失败：${err}`
  }
}

// 门槛表跟随**当前选中的那条轨迹**（点到挖掘轨迹时看到的应该是挖掘的门槛，
// 而不是每日那条的七个 —— 否则两个标签页看的是两套东西，关联感就断了）
const gateRows = computed(() => (current.value?.gates?.length ? current.value.gates : overview.value?.gates || []))

const selectedItem = computed<AgentFamilyItem | null>(() => {
  for (const family of overview.value?.families || []) {
    for (const branch of family.branches) {
      const hit = branch.items.find((item) => item.id === selectedId.value)
      if (hit) return hit
    }
  }
  return null
})

const budgetText = computed(() => {
  const budget = current.value?.budget
  if (!budget || budget.seconds == null) return '预算未设置'
  return `预算 ${Math.round(Number(budget.seconds))}s${budget.over ? '（超了）' : ''}`
})

function fmtDuration(seconds: number | null | undefined) {
  if (seconds == null) return '—'
  const total = Math.round(Number(seconds))
  if (total < 60) return `${total}s`
  if (total < 3600) return `${Math.floor(total / 60)}m${total % 60}s`
  return `${Math.floor(total / 3600)}h${Math.floor((total % 3600) / 60)}m`
}

function shorten(text: string | null | undefined, limit: number) {
  const value = String(text || '')
  return value.length > limit ? `${value.slice(0, limit)}…` : value
}

/** 产物路径只留最后三段，完整路径进 title */
function shortPath(path: string) {
  const parts = String(path || '').replace(/\/+$/, '').split('/')
  return parts.length > 3 ? `…/${parts.slice(-3).join('/')}` : path
}

/** 语义色：ok 绿 / warn 橙 / bad 红（与「涨跌红绿」无关） */
function tone(status: string | null | undefined) {
  const map: Record<string, string> = {
    ok: 'ok', pass: 'ok', warn: 'warn', failed: 'bad', fail: 'bad', dry_run: 'warn',
  }
  return map[String(status)] || ''
}

function runTone(status: string | null | undefined) {
  if (status === 'ok') return 'ok'
  if (status === 'failed') return 'bad'
  return ''
}

function itemTone(item: AgentFamilyItem) {
  if (item.stage === 'retired') return ''
  if (item.fresh === true) return 'ok'
  if (item.fresh === false) return 'bad'
  return ''
}

// 深链：`#/agent?tab=mining`（旧书签 `#/factor-mining` 由路由重定向到这里）
// `?run=` 变化也要跟着切轨迹（同一标签页内跳转不会重新 mount）
watch(
  () => route.query.run,
  async (value) => {
    const runId = String(value || '')
    if (runId && runId !== current.value?.run_id) await openRun(runId)
  },
)

watch(
  () => route.query.tab,
  (value) => {
    const next = String(value || 'console') as typeof tab.value
    if (['console', 'mining', 'prompt'].includes(next) && next !== tab.value) tab.value = next
  },
  { immediate: true },
)

onMounted(reload)
</script>

<style scoped>
.tabs {
  display: flex; gap: var(--sp-2); margin-bottom: var(--sp-3);
  border-bottom: 1px solid var(--border-color); padding-bottom: var(--sp-2);
}
.tab {
  display: flex; align-items: center; gap: 6px; cursor: pointer;
  background: transparent; border: 1px solid transparent; border-bottom: 2px solid transparent;
  color: var(--text-muted); font-size: var(--fs-md); padding: 5px 10px; border-radius: var(--r-2) var(--r-2) 0 0;
}
.tab:hover { color: var(--text-primary); background: rgba(255, 255, 255, 0.04); }
.tab.active { color: var(--accent-gold); border-bottom-color: var(--accent-gold); }

.layer-strip {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--sp-3);
  margin-bottom: var(--sp-3);
}
.layer-tile {
  background: var(--bg-card);
  border: 1px solid var(--border-color);
  border-radius: var(--r-3);
  padding: var(--sp-3);
  min-width: 0;
}
.lt-head { display: flex; align-items: center; gap: var(--sp-2); margin-bottom: 4px; }
.lt-detail { color: var(--text-muted); margin: 0; }

.console-grid {
  display: grid;
  grid-template-columns: 300px minmax(0, 1fr);
  gap: var(--sp-3);
  align-items: start;
}
.right-col { min-width: 0; }

.dot { width: 9px; height: 9px; border-radius: 50%; background: var(--text-dim); flex-shrink: 0; }
.dot.ok { background: var(--state-ok); }
.dot.warn { background: var(--state-warn); }
.dot.bad { background: var(--state-bad); }

.family { border-top: 1px dashed var(--border-soft); padding-top: var(--sp-2); margin-top: var(--sp-2); }
.family:first-of-type { border-top: none; margin-top: 0; padding-top: 0; }
.family-head { display: flex; align-items: center; gap: var(--sp-2); }
.branch { margin: var(--sp-2) 0 0 var(--sp-2); }
.branch-label { font-size: var(--fs-xs); color: var(--text-muted); margin-bottom: var(--sp-1); }
.item {
  display: flex; align-items: center; gap: 6px; width: 100%;
  background: transparent; border: 1px solid transparent; border-radius: var(--r-2);
  padding: 5px 6px; color: var(--text-primary); cursor: pointer; text-align: left;
}
.item:hover { background: rgba(255, 255, 255, 0.04); }
.item.active { border-color: var(--accent-gold); background: rgba(255, 209, 102, 0.08); }
.item-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.selected { margin-top: var(--sp-3); border-top: 1px solid var(--border-color); padding-top: var(--sp-2); }
.selected dd { word-break: break-all; }
.sel-head { margin-bottom: var(--sp-2); }

.runs { display: flex; flex-direction: column; gap: 4px; margin-bottom: var(--sp-3); }
.run {
  display: flex; align-items: center; gap: var(--sp-2); width: 100%;
  background: var(--bg-elevated); border: 1px solid var(--border-color);
  border-radius: var(--r-2); padding: 6px 8px; cursor: pointer; color: var(--text-primary); text-align: left;
}
.run:hover { border-color: var(--accent-gold); }
.run.active { border-color: var(--accent-gold); background: rgba(255, 209, 102, 0.08); }
.run-id { flex-shrink: 0; }
.go-session { margin-left: auto; font-size: var(--fs-xs); color: var(--accent-gold); cursor: pointer; white-space: nowrap; }
.go-session:hover { text-decoration: underline; }
.source-link dd { display: flex; align-items: center; gap: var(--sp-2); }
.factor-chips { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: var(--sp-2) 0; }
.tiles { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--sp-2); margin-bottom: var(--sp-3); }
.rollback { margin-top: var(--sp-3); border-top: 1px solid var(--border-color); padding-top: var(--sp-2); }
.rollback summary { cursor: pointer; color: var(--text-muted); font-size: var(--fs-sm); }
.rollback dd { word-break: break-all; }

.gate-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--sp-2); }
.gate {
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  border: 1px solid var(--border-color); border-radius: var(--r-2); padding: var(--sp-2);
}
.gate-name { font-size: var(--fs-sm); }
.gate p { flex-basis: 100%; margin: 0; }

@media (max-width: 1180px) {
  .layer-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .console-grid { grid-template-columns: minmax(0, 1fr); }
  .tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .gate-list { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 720px) {
  .layer-strip { grid-template-columns: minmax(0, 1fr); }
}
</style>
