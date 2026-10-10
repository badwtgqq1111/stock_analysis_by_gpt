<template>
  <div class="page">
    <PageHeader
      title="策略池"
      description="所有选股策略的登记处：<b>生产中</b>的策略由每日自动化跑并按交易日判新鲜度，<b>候选</b>来自 LLM 因子挖掘 / agent 研究，验证通过后再升格。声明写在 <code>config/strategies.json</code>，运行时状态由 <code>scripts/sync_strategy_pool.py</code> 扫产物生成。"
    >
      <template #actions>
        <span class="tiny dim">基准交易日 <b>{{ pool?.reference_trade_date || '—' }}</b></span>
        <button class="btn-ghost" :disabled="loading" @click="reload">刷新</button>
      </template>
    </PageHeader>

    <StateBlock v-if="error" state="error" title="取不到策略池" :detail="error" :retry="reload" />
    <StateBlock v-else-if="!pool" state="loading" title="加载策略池…" />

    <template v-else>
      <div class="panel">
        <div class="pill-row">
          <span class="pill">生产策略 <b>{{ pool.totals.n_production }}</b></span>
          <span class="pill">候选 <b>{{ pool.totals.n_candidates }}</b></span>
          <span class="pill">研究证据 <b>{{ pool.totals.n_evidence }}</b></span>
          <span class="pill" :class="pool.totals.fresh ? '' : 'bad-pill'">
            新鲜度 <b>{{ pool.totals.fresh ? '全部达标 ✅' : '不达标 ' + pool.totals.stale_production.length + ' 项 ❌' }}</b>
          </span>
          <span class="pill dim tiny">配置更新 {{ pool.config_updated_at || '—' }} · 快照 {{ pool.generated_at }}</span>
        </div>
        <p class="hint tiny" v-if="pool.totals.stale_production.length">
          落后的策略：{{ pool.totals.stale_production.join('、') }} —— 每日自动化末尾会跑
          <code>sync_strategy_pool.py --strict</code>，不达标的会写进运行日志并告警。
        </p>
        <p class="hint tiny">统一对比基准：中证500（000905.SH） · 绩效快照 {{ pool.performance_generated_at || '待生成' }}。
          同窗比较：{{ pool.common_window?.status === 'ok' ? `${pool.common_window.start_date} — ${pool.common_window.end_date}` : '共同历史不足，暂不跨策略排名' }}。
          名单跟踪毛收益与独立研究回测分开展示，非真实账户净收益。</p>
      </div>

      <!-- 每日名单策略卡片：包括仍处于 candidate 阶段的多因子 -->
      <div class="strategy-grid">
        <div v-for="s in pool.strategies" :key="s.id" class="panel strategy-card">
          <div class="sc-head">
            <span class="sc-name">{{ s.name }}</span>
            <span class="tag" :class="s.tag_class">{{ s.group }}</span>
            <span class="stage-badge" :class="'st-' + s.stage">{{ s.stage_label }}</span>
          </div>
          <p class="sc-summary">{{ s.summary }}</p>

          <div class="sc-stats">
            <span class="sc-stat">
              <label>最新交易日</label>
              <b class="mono" :class="s.fresh ? 'ok' : 'bad'">{{ s.latest_date || '—' }}</b>
              <span class="tiny dim" v-if="s.lag_days">{{ s.lag_days }} 天前</span>
            </span>
            <span class="sc-stat">
              <label>最新名单</label>
              <b class="mono">{{ s.n_picks ?? '—' }} 只</b>
            </span>
            <span class="sc-stat">
              <label>历史快照</label>
              <b class="mono">{{ s.n_snapshots }}</b>
            </span>
          </div>

          <div class="sc-row">
            <label>入口命令</label>
            <code class="sc-code">{{ s.entry || '—' }}</code>
          </div>
          <div class="sc-row">
            <label>调度</label>
            <span class="tiny">{{ s.schedule || '—' }}</span>
          </div>
          <!-- 回放指标直接摊在卡上：策略"到底行不行"不该点进别的页面才知道 -->
          <div class="sc-row" v-if="s.metrics && s.metrics.length">
            <label>月度回放（同窗口，基准=全市场等权）</label>
            <span class="metric-row">
              <span v-for="m in s.metrics" :key="m.key" class="metric"
                    :class="{ neg: m.key === 'excess_mean' && m.value < 0 }">
                <label>{{ m.label }}</label>
                <b class="mono">{{ m.value !== null ? fmtMetric(m, s.metrics_pct) : '—' }}</b>
              </span>
            </span>
          </div>
          <div class="sc-row">
            <label>台账来源</label>
            <span>
              <span v-for="src in s.ledger_sources" :key="src" class="tag dim tiny">{{ src }}</span>
              <span v-if="!s.ledger_sources.length" class="dim tiny">—</span>
            </span>
          </div>
          <div class="sc-row">
            <label>产物</label>
            <span class="artifact-list">
              <span v-for="a in s.artifacts" :key="a.path" class="artifact"
                    :class="{ missing: !a.exists }" :title="a.path + (a.detail ? '｜' + a.detail : '')">
                {{ a.path.split('/').pop() }}
                <span class="dim tiny">{{ a.latest_date || (a.exists ? '存在' : '缺失') }}</span>
              </span>
            </span>
          </div>

          <div class="sc-actions">
            <a class="btn-ghost btn-sm" :href="selectionLink(s)">看最新名单 →</a>
            <a class="btn-ghost btn-sm" href="#/calendar">选股日历 →</a>
          </div>
          <StrategyPerformance :data="s.performance" />
        </div>
      </div>

      <!-- 候选池 -->
      <div class="panel">
        <h6>
          候选策略（agent / LLM 挖掘产出，需人工升格才会进生产）
          <span class="dim tiny">{{ pool.candidates.length }} 个</span>
        </h6>
        <div v-if="!pool.candidates.length" class="empty">
          还没有候选。跑一次「因子挖掘」页的挖掘，或在研究里产出结论后用
          <code>uv run python scripts/sync_strategy_pool.py --register cand.json</code> 登记。
        </div>
        <table v-else class="data-table">
          <thead>
            <tr><th class="left">候选</th><th class="left">来源</th><th class="left">阶段</th>
              <th class="left">说明</th><th class="left">指标</th><th class="left">更新时间</th>
              <th class="left">操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="c in pool.candidates" :key="c.id">
              <td class="left">
                <a v-if="c.selection_source" class="btn-ghost btn-sm" :href="`#/?source=${c.selection_source}`">看每日名单 →</a>
                <b>{{ c.name }}</b>
                <div class="tiny dim mono">{{ c.id }}</div>
              </td>
              <td class="left tiny">{{ c.origin || '—' }}</td>
              <td class="left"><span class="stage-badge" :class="'st-' + c.stage">{{ c.stage_label }}</span></td>
              <td class="left multiline">{{ c.summary || '—' }}</td>
              <td class="left tiny mono">
                <div v-for="(v, k) in c.metrics" :key="k">{{ k }} = {{ v }}</div>
                <span v-if="!Object.keys(c.metrics || {}).length" class="dim">—</span>
              </td>
              <td class="left tiny dim">{{ c.updated_at || '—' }}</td>
              <td class="left">
                <button class="btn-ghost btn-sm" :disabled="busy === c.id"
                        @click="changeStage(c.id, 'research')">升为研究中</button>
                <button class="btn-ghost btn-sm" :disabled="busy === c.id"
                        @click="changeStage(c.id, 'retired')">下线</button>
              </td>
            </tr>
          </tbody>
        </table>
        <p class="hint tiny mt" v-if="stageHint">{{ stageHint }}</p>
      </div>

      <!-- 研究证据 -->
      <div class="panel">
        <h6>
          研究证据（<code>output/research/*/REPORT.md</code>）
          <span class="dim tiny">策略的来源出处；不是策略本身（不产生每日名单）</span>
        </h6>
        <ul class="evidence">
          <li v-for="e in pool.evidence.slice(0, 24)" :key="e.key">
            <span class="ev-title">{{ e.title }}</span>
            <span class="ev-path mono tiny dim">{{ e.path }}</span>
            <span class="ev-time tiny dim">{{ e.mtime }}</span>
          </li>
        </ul>
        <p class="hint tiny mt" v-if="pool.evidence.length > 24">
          只列最近 24 份，共 {{ pool.evidence.length }} 份。
        </p>
      </div>

      <div class="panel">
        <h6>口径说明</h6>
        <ul class="hint tiny">
          <li v-for="n in pool.notes || []" :key="n">{{ n }}</li>
          <li>「最新交易日」取自产物里的日期列 / 历史快照文件名，**不看文件 mtime**（否则重跑一次旧产物也会显示成今天）。</li>
        </ul>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { getStrategyPool, setStrategyStage } from '@/api/client'
import type { StrategyPoolEntry, StrategyPoolResponse } from '@/types/api'
import PageHeader from '@/components/ui/PageHeader.vue'
import StateBlock from '@/components/ui/StateBlock.vue'
import StrategyPerformance from '@/components/StrategyPerformance.vue'

const pool = ref<StrategyPoolResponse | null>(null)
const error = ref('')
const loading = ref(false)
const busy = ref('')
const stageHint = ref('')

async function reload() {
  loading.value = true
  try {
    pool.value = await getStrategyPool(true)
    error.value = ''
  } catch (e) {
    error.value = String(e)
  } finally {
    loading.value = false
  }
}

/** 该策略的名单在「选股结果」页对应的来源 tab（台账 source key 的第一个） */
function selectionLink(item: StrategyPoolEntry): string {
  const source = item.selection_source || ''
  return source ? `#/?source=${encodeURIComponent(source)}` : '#/'
}

/** 回放指标：`metrics_pct` 里的字段按百分数显示（超额/胜率/回撤…） */
function fmtMetric(metric: { key: string; value: number }, pctKeys: string[]): string {
  if ((pctKeys || []).includes(metric.key)) {
    const sign = metric.value > 0 ? '+' : ''
    return `${sign}${(metric.value * 100).toFixed(2)}%`
  }
  return metric.value.toFixed(3)
}

async function changeStage(id: string, stage: string) {
  busy.value = id
  stageHint.value = ''
  try {
    const res = await setStrategyStage(id, stage)
    stageHint.value = res.hint || `已把 ${id} 切到 ${res.stage}`
    await reload()
  } catch (e) {
    stageHint.value = `切换失败：${e}`
  } finally {
    busy.value = ''
  }
}

onMounted(reload)
</script>

<style scoped>
.strategy-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
  gap: var(--sp-3); margin-bottom: var(--sp-3); }
.strategy-card { display: flex; flex-direction: column; gap: var(--sp-2); }
.sc-head { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.sc-name { font-weight: 700; font-size: var(--fs-lg); }
.stage-badge { padding: 1px 8px; border-radius: var(--r-pill); font-size: var(--fs-xs);
  border: 1px solid var(--border-color); color: var(--text-muted); }
.st-production { color: var(--accent-gold); border-color: rgba(255, 209, 102, 0.5); }
.st-research { color: var(--accent-blue); border-color: rgba(17, 138, 178, 0.5); }
.st-candidate { color: var(--accent-orange); border-color: rgba(255, 159, 28, 0.5); }
.st-retired { color: var(--text-dim); }
.sc-summary { font-size: var(--fs-sm); color: var(--text-muted); line-height: 1.6; }
.sc-stats { display: flex; gap: var(--sp-4); flex-wrap: wrap; }
.metric-row { display: flex; gap: var(--sp-3); flex-wrap: wrap; }
.metric { display: flex; flex-direction: column; gap: 0; }
.metric label { font-size: 10px; color: var(--text-dim); }
.metric b { font-size: var(--fs-sm); color: var(--text-primary); }
.metric.neg b { color: var(--accent-down); }
.sc-stat { display: flex; flex-direction: column; gap: 1px; }
.sc-stat label, .sc-row label { font-size: 10px; color: var(--text-dim); text-transform: uppercase;
  letter-spacing: 0.04em; }
.sc-row { display: flex; flex-direction: column; gap: 2px; }
.sc-code { display: block; font-size: var(--fs-xs); background: var(--bg-input);
  border: 1px solid var(--border-color); border-radius: var(--r-2); padding: 3px 6px;
  overflow-x: auto; white-space: nowrap; }
.artifact-list { display: flex; flex-wrap: wrap; gap: 4px; }
.artifact { font-size: var(--fs-xs); font-family: var(--font-mono); background: var(--bg-input);
  border: 1px solid var(--border-color); border-radius: var(--r-2); padding: 1px 6px; }
.artifact.missing { border-color: rgba(239, 71, 111, 0.6); color: var(--accent-up); }
.sc-actions { display: flex; gap: var(--sp-2); margin-top: auto; padding-top: var(--sp-2); }
/* 状态用语义色（--state-ok 绿），涨跌用 --accent-up/down（红涨绿跌）——
   两套令牌混用会让人把"状态正常"读成"跌"，这里刻意分开。 */
.ok { color: var(--state-ok); }
.bad { color: var(--state-bad); }
.bad-pill { border-color: rgba(239, 71, 111, 0.6); color: var(--accent-up); }
.multiline { max-width: 360px; white-space: normal; line-height: 1.5; }
.left { text-align: left; }
.data-table th.left { text-align: left; }
.evidence { list-style: none; margin: 0; padding: 0; display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 4px; }
.evidence li { display: flex; flex-direction: column; padding: 4px 6px; border-radius: var(--r-2);
  background: var(--bg-input); border: 1px solid var(--border-color); }
.ev-title { font-size: var(--fs-sm); }
.ev-path { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mt { margin-top: var(--sp-2); }
.empty { color: var(--text-muted); font-size: var(--fs-sm); }
</style>
