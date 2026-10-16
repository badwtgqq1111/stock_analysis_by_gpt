<template>
  <section class="panel volatility-risk" aria-label="波动率研究风险预算">
    <h3>波动率影子 · 风险预算与前向验证</h3>
    <div class="controls">
      <label>研究变体 <select v-model="variant" @change="load(true)"><option v-for="v in data?.variants || [variant]" :key="v">{{ v }}</option></select></label>
      <label>历史决策日 <select v-model="date" @change="load()"><option v-for="d in data?.dates || []" :key="d">{{ d }}</option></select></label>
      <button :disabled="loading" @click="load()">{{ loading ? '加载中…' : '刷新风险' }}</button>
    </div>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-else-if="loading">读取独立研究快照…</p>
    <template v-else-if="data?.available">
      <p>{{ data.hint }}</p>
      <p v-if="!data.audit?.length">该变体在所选日期无决策；可切换日期查看。</p>
      <p v-for="a in data.audit" :key="a.status">
        敞口 {{ pct(a.exposure) }} · 现金 {{ pct(1 - a.exposure) }} · 预测年化风险 {{ pct(a.predicted_annual_vol) }}
        · 风险上限 {{ pct(data.target_vol) }} · 状态 {{ a.status }} {{ a.error || '' }}
      </p>
      <div class="scroll"><table v-if="data.contributions?.length"><thead><tr><th>股票</th><th>权重</th><th>边际风险</th><th>风险贡献</th><th>贡献份额</th><th>等风险参照</th><th>偏离</th></tr></thead>
        <tbody><tr v-for="r in data.contributions" :key="r.stock_code"><td>{{ r.stock_code }}</td><td>{{ pct(r.weight) }}</td><td>{{ pct(r.marginal_vol) }}</td><td>{{ pct(r.component_vol) }}</td><td>{{ pct(r.risk_share) }}</td><td>{{ pct(r.reference_budget_share) }}</td><td>{{ pct(r.budget_deviation) }}</td></tr></tbody></table></div>
      <h4>策略相关性 · 截至 {{ data.correlation?.asof }}</h4>
      <p>{{ data.correlation?.observations || 0 }} 个共同日收益<template v-if="data.correlation?.short_sample"> · 短样本，仅供研究</template></p>
      <div class="scroll"><table v-if="data.correlation?.rows.length"><thead><tr><th>变体</th><th v-for="n in data.correlation.names" :key="n">{{ n }}</th></tr></thead><tbody>
        <tr v-for="r in data.correlation.rows" :key="String(r.variant)"><td>{{ r.variant }}</td><td v-for="n in data.correlation.names" :key="n">{{ typeof r[n] === 'number' ? Number(r[n]).toFixed(3) : '—' }}</td></tr>
      </tbody></table></div>
      <h4>前向晋级门槛 · {{ data.forward?.status === 'accumulating' ? '积累中，未晋级' : '等待有效样本' }}</h4>
      <p>真实留档决策 {{ data.forward?.eligible_decisions || 0 }} 个 · 捕获 {{ data.forward?.captured_at || '待运行' }} · 不自动修改生产权重</p>
      <div v-for="(gate, name) in data.forward?.gates" :key="name"><b>{{ name }}</b>：{{ gate.status === 'review_eligible' ? '可供人工复核' : '积累中' }} · {{ gate.observations }} / 252 日收益，{{ gate.decisions }} / 63 决策
        <ul><li v-for="(ok, check) in gate.checks" :key="check">{{ labels[check] || check }}：{{ ok ? '通过' : '待满足' }}</li></ul>
      </div>
    </template>
    <p v-else>{{ data?.hint || '研究风险产物待生成' }}</p>
  </section>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { getVolatilityRisk } from '@/api/client'
import type { VolatilityRiskResponse } from '@/types/api'
const data = ref<VolatilityRiskResponse | null>(null)
const variant = ref('cn_ensemble_volcap'), date = ref(''), loading = ref(false), error = ref('')
const labels: Record<string, string> = { frozen_protocol_consistent: '冻结模型及执行协议一致', prospective_provenance_reviewed: '前向来源及价格版本审计', complete_daily_coverage: '逐日留档完整', returns_252: '至少252个日收益', decisions_63: '至少63个决策', stable_quarters: '至少3季度且2/3风险稳定改善', risk_not_higher: '波动不高于基线', return_shortfall_le_2pct: '累计收益落后不超过2个百分点', drawdown_not_worse: '回撤不劣于基线' }
const pct = (n: number | null | undefined) => n == null ? '—' : `${(n * 100).toFixed(2)}%`
let request = 0
async function load(latest = false) {
  const id = ++request; loading.value = true; error.value = ''
  try { const result = await getVolatilityRisk(latest ? undefined : date.value || undefined, variant.value); if (id === request) { data.value = result; date.value = result.date || '' } }
  catch (e) { if (id === request) error.value = e instanceof Error ? e.message : String(e) }
  finally { if (id === request) loading.value = false }
}
onMounted(() => load())
</script>
<style scoped>
.volatility-risk { margin: 16px 0; } .controls { display: flex; gap: 16px; flex-wrap: wrap; align-items: center; } .scroll { overflow-x: auto; } table { width: 100%; font-size: 12px; border-collapse: collapse; } th,td { text-align: right; padding: 6px; border-bottom: 1px solid var(--border, #ddd); } th:first-child,td:first-child { text-align: left; } p,li { font-size: 12px; } ul { display: flex; gap: 8px 20px; flex-wrap: wrap; }
</style>
