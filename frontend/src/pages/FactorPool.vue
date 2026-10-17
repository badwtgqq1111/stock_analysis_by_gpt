<template>
  <div class="page factor-pool">
    <h2>因子池 · P2.71</h2>
    <p class="hint">独立于旧 LightGBM / preselect / PK 链路。编辑只追加版本，策略固定引用版本；保存或“优化”不代表收益已改善。</p>
    <p role="alert" v-if="error" class="error">{{ error }}</p>
    <p role="status" v-if="message">{{ message }}</p>
    <p v-if="loading">加载中…</p>
    <section class="panel" v-if="aqcmPerf?.available">
      <h3>AlphaQCM 冻结组合 · 中证500 对比</h3>
      <SvgCurve :series="aqcmSeries" :labels="aqcmPerf.dates" :height="170" />
      <p class="tiny dim">{{ aqcmPerf.start }} — {{ aqcmPerf.end }} · {{ aqcmPerf.observations }} 个收益观测 · {{ aqcmPerf.factor_count }} 个冻结表达式 · {{ aqcmPerf.reason }}</p>
      <table class="data-table tiny"><thead><tr><th>指标</th><th>AlphaQCM组合</th><th>中证500</th></tr></thead><tbody>
        <tr v-for="m in perfMetricDefs" :key="m.key"><td>{{ m.label }}</td>
          <td class="mono">{{ fmtPerf(aqcmPerf.metrics?.[m.key], m.pct) }}</td>
          <td class="mono">{{ fmtPerf(aqcmPerf.benchmark_metrics?.[m.key], m.pct) }}</td></tr>
      </tbody></table>
      <p class="tiny">相对中证500累计超额：<b class="mono">{{ fmtPerf(aqcmPerf.excess_return, true) }}</b></p>
      <button type="button" class="btn-ghost btn-sm" :disabled="busy" @click="importAlphaQCM">把该 run 的候选导入因子池</button>
    </section>
    <p v-else-if="aqcmPerf && !aqcmPerf.available" class="hint tiny">{{ aqcmPerf.reason }}</p>
    <div class="factor-layout" v-if="factors.length">
      <aside class="panel">
        <h3>公式库</h3>
        <button v-for="f in factors" :key="f.factor_id" class="factor-item" :class="{ active: selected?.factor_id === f.factor_id }" @click="choose(f)">
          {{ f.name }} <small>{{ f.factor_id }} · v{{ f.version }}</small>
        </button>
      </aside>
      <section v-if="selected" class="panel editor">
        <header><h3>{{ selected.name }} · v{{ selected.version }}</h3><span class="tag">{{ selected.evaluation_status }} · {{ languageLabel(selected.expression_language) }}</span></header>
        <form @submit.prevent="save">
          <label>名称<input v-model="draft.name" required maxlength="120" /></label>
          <label>{{ languageLabel(draft.expression_language) }}<textarea v-model="draft.formula" required rows="3" maxlength="4000" spellcheck="false" /></label>
          <label>金融解释（解释每个常量、参数与意图）<textarea v-model="draft.description" required rows="4" /></label>
          <h4>出处 / 文章来源</h4>
          <fieldset v-for="(source, i) in draft.sources" :key="i">
            <label>标题<input v-model="source.title" required /></label>
            <label>URL<input v-model="source.url" type="url" placeholder="https://…（项目自研可留空）" /></label>
            <label>作者<input v-model="source.authors" /></label>
            <label>发布日期<input v-model="source.date" /></label>
            <label>页码 / 公式号 / 代码位置 / commit<input v-model="source.locator" required /></label>
            <button type="button" v-if="draft.sources.length > 1" @click="draft.sources.splice(i, 1)">移除此出处</button>
          </fieldset>
          <button type="button" @click="draft.sources.push({ title: '', url: '', locator: '', authors: '', date: '' })">增加出处</button>
          <label>版本操作<select v-model="action"><option value="edit">编辑并保存新版本</option><option value="replace">替换提案（不替换策略引用）</option><option value="optimize">优化提案（待评估）</option><option value="restore">恢复历史内容为新版本</option><option value="fork">派生新因子</option></select></label>
          <label v-if="action === 'fork'">新因子 ID<input v-model="forkId" required pattern="[a-z][a-z0-9_]{1,79}" /></label>
          <label>修改原因 / 参数说明<input v-model="note" required /></label>
          <div class="actions"><button type="button" :disabled="busy" @click="validate">校验并查看变量解释</button><button :disabled="busy">保存新版本</button></div>
        </form>
        <h4>当前草稿与所选版本差异</h4>
        <pre>{{ diffText }}</pre>
        <template v-if="semantics">
          <h4>变量字典 · {{ semantics.dsl_version }} · {{ semantics.result_type }}</h4>
          <div class="table-wrap"><table><thead><tr><th>变量</th><th>解释 / 定义</th><th>单位</th><th>窗口 / 可得时点</th></tr></thead><tbody>
            <tr v-for="v in semantics.variables" :key="v.name"><td><code>{{ v.name }}</code></td><td>{{ v.label }}<br />{{ v.definition }}</td><td>{{ v.unit }}</td><td>{{ v.window }}<br />{{ v.availability }}</td></tr>
          </tbody></table></div>
          <h4>算子解释</h4><p v-for="o in semantics.operators" :key="o.name"><code>{{ o.name }}</code>：{{ o.description }}</p>
          <h4>参数解释</h4><p v-for="(p, i) in semantics.parameters" :key="i">{{ p.value }}：{{ p.role }}；具体金融含义见上方解释。</p>
        </template>
        <h4>历史版本（点击载入草稿；保存时追加，不覆盖历史）</h4>
        <button v-for="v in history" :key="v.version" class="history-item" @click="loadHistory(v)">v{{ v.version }} · {{ v.action }} · {{ v.change_note }} · {{ v.created_at }}</button>
        <p class="tiny">所选版本 SHA256：{{ selected.sha256 }}；父版本：{{ selected.parent_factor_id || '—' }} / {{ selected.parent_version || '—' }}</p>
      </section>
    </div>
    <section class="panel" v-for="family in families" :key="family.id">
      <h3>策略家族：{{ family.name }}</h3>
      <p>主要挑战基准：{{ family.primary_benchmark }}；市场基准：中证500 {{ family.market_benchmark }}。同区间、同成本、同成交约束比较年化/回撤/换手。</p>
      <div class="table-wrap"><table><thead><tr><th>实现</th><th>决策类型 / 版本</th><th>状态（不是收益结论）</th><th>固定因子版本</th></tr></thead><tbody>
        <tr v-for="v in family.variants" :key="v.id"><td>{{ v.name }}<br /><code>{{ v.id }}</code></td><td>{{ v.decision_type }} / {{ v.implementation_version }}</td><td>{{ v.status }}</td><td><span v-for="r in v.factor_refs" :key="r.factor_id">{{ r.factor_id }}@{{ r.version }}<br /></span></td></tr>
      </tbody></table></div>
      <p v-for="v in family.variants.filter(x => x.gates)" :key="v.id"><b>{{ v.name }}全部硬门槛 AND：</b>{{ v.gates?.map(g => `${g.factor_id} ${g.op} ${g.other_factor_id ?? g.value}`).join(' AND ') }}；通过后按资金流强度降序，同分按股票代码。线性加权允许相互补偿，不等于硬门槛。</p>
      <p>等价版已完成公共因果回放的逐笔/NAV一致性审计（回顾窗口2024-08-27至2026-10-15），已作为研究策略入池展示中证500对照，不代表生产或盲测业绩；PIT、真实成交约束、样本外与优化版仍待验收。</p>
    </section>
  </div>
</template>
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { factorAPI, type AlphaQCMPerformance, type Factor, type Family, type Semantics, type Source } from '@/api/factorPool'
import SvgCurve from '@/components/ui/SvgCurve.vue'
const factors = ref<Factor[]>([]), families = ref<Family[]>([]), selected = ref<Factor | null>(null), history = ref<Factor[]>([])
const aqcmPerf = ref<AlphaQCMPerformance | null>(null)
const draft = ref<{ name: string; formula: string; description: string; sources: Source[]; expression_language: string }>({ name: '', formula: '', description: '', sources: [], expression_language: 'p2_71_dsl' })
const semantics = ref<Semantics | null>(null), error = ref(''), message = ref(''), note = ref(''), action = ref('edit'), forkId = ref('')
const busy = ref(false), loading = ref(true)
function err(e: unknown) { const x = e as { response?: { data?: { detail?: unknown } }; message?: string }; error.value = String(x.response?.data?.detail ?? x.message ?? e) }
function languageLabel(language?: string) { return language === 'alphagen_v1' ? 'AlphaGen 原生表达式（正窗口）' : '可编辑公式（受控DSL）' }
const aqcmSeries = computed(() => aqcmPerf.value?.nav?.length ? [
  { name: 'AlphaQCM组合', color: 'var(--accent-gold)', points: aqcmPerf.value.nav },
  { name: '中证500', color: 'var(--accent-blue)', points: aqcmPerf.value.benchmark_nav || [] },
] : [])
const perfMetricDefs = [
  { key: 'total_return', label: '累计收益', pct: true },
  { key: 'annual_return_252', label: '年化收益（252日）', pct: true },
  { key: 'max_drawdown', label: '最大回撤', pct: true },
  { key: 'sharpe', label: 'Sharpe（rf=0）', pct: false },
  { key: 'calmar', label: 'Calmar', pct: false },
]
function fmtPerf(v: number | null | undefined, pct: boolean): string {
  if (v == null || !Number.isFinite(v)) return '—'
  return pct ? `${(v * 100).toFixed(2)}%` : v.toFixed(3)
}
async function choose(f: Factor) {
  selected.value = f; draft.value = JSON.parse(JSON.stringify({ name: f.name, formula: f.formula, description: f.description, sources: f.sources, expression_language: f.expression_language || 'p2_71_dsl' }))
  semantics.value = f.semantics; note.value = ''; action.value = 'edit'; error.value = ''
  try { const versions = await factorAPI.history(f.factor_id); if (selected.value?.factor_id === f.factor_id) history.value = versions } catch (e) { err(e) }
}
function loadHistory(v: Factor) {
  draft.value = JSON.parse(JSON.stringify({ name: v.name, formula: v.formula, description: v.description, sources: v.sources, expression_language: v.expression_language || 'p2_71_dsl' }))
  semantics.value = v.semantics; note.value = `恢复v${v.version}内容`; action.value = 'restore'
}
const diffText = computed(() => {
  if (!selected.value) return ''
  const old = selected.value
  return [old.formula !== draft.value.formula ? `- ${old.formula}\n+ ${draft.value.formula}` : '公式未变', old.description !== draft.value.description ? '- ' + old.description + '\n+ ' + draft.value.description : '解释未变', JSON.stringify(old.sources) !== JSON.stringify(draft.value.sources) ? '出处已修改' : '出处未变'].join('\n')
})
watch(() => draft.value.formula, () => { semantics.value = null }, { flush: 'sync' })
async function validate() {
  busy.value = true; error.value = ''; message.value = ''
  try { semantics.value = await factorAPI.validate(draft.value.formula, draft.value.expression_language); message.value = '公式校验通过；此操作不是收益评测' } catch (e) { semantics.value = null; err(e) } finally { busy.value = false }
}
async function save() {
  if (!selected.value) return
  busy.value = true; error.value = ''; message.value = ''
  try {
    const fork = action.value === 'fork'
    const saved = await factorAPI.save(fork ? forkId.value : selected.value.factor_id, { ...draft.value, expected_version: fork ? 0 : selected.value.version, action: fork ? 'create' : action.value, change_note: note.value, parent: fork ? { factor_id: selected.value.factor_id, version: selected.value.version } : null })
    const data = await factorAPI.list(); factors.value = data.factors; await choose(saved)
    message.value = `已持久化 ${saved.factor_id}@${saved.version}；既有策略的固定引用保持不变；新版本未评估`
  } catch (e) { err(e) } finally { busy.value = false }
}
async function importAlphaQCM() {
  if (!aqcmPerf.value?.run_dir) return
  busy.value = true; error.value = ''; message.value = ''
  try {
    const result = await factorAPI.importAlphaQCM(aqcmPerf.value.run_dir)
    const data = await factorAPI.list()
    factors.value = data.factors
    message.value = `AlphaQCM候选已导入：${JSON.stringify(result).slice(0, 160)}`
  } catch (e) { err(e) } finally { busy.value = false }
}
onMounted(async () => {
  try {
    const data = await factorAPI.list(); factors.value = data.factors; families.value = data.families
    if (data.factors[0]) await choose(data.factors[0])
    aqcmPerf.value = await factorAPI.alphaqcmPerformance()
  } catch (e) { err(e) } finally { loading.value = false }
})
</script>
<style scoped>
.factor-layout { display: grid; grid-template-columns: 245px minmax(0,1fr); gap: 16px; margin: 16px 0; }
.factor-item,.history-item { display: block; width: 100%; text-align: left; padding: 12px; margin: 5px 0; white-space: normal; }
.factor-item small { display: block; opacity: .65; }.active { border-color: var(--accent-gold); }
label { display: block; margin: 12px 0; }input,textarea,select { display: block; width: 100%; box-sizing: border-box; margin-top: 5px; padding: 8px; background: var(--bg-panel); color: var(--text-primary); border: 1px solid var(--border-color); border-radius: 4px; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; background: rgba(0,0,0,.15); padding: 12px; }fieldset { border: 1px solid var(--border-color); margin-bottom: 10px; }.actions { display: flex; gap: 10px; }.error { color: var(--state-bad); }.table-wrap { overflow-x: auto; }th,td { padding: 10px; text-align: left; vertical-align: top; }code { overflow-wrap: anywhere; }.tiny { overflow-wrap: anywhere; }
@media(max-width: 900px) { .factor-layout { grid-template-columns: 1fr; } }
</style>
