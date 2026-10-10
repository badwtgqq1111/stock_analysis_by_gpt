<template>
  <div class="page">
    <PageHeader
      title="选股归因"
      description="逐股回答<strong>「为什么被选中」</strong>：LightGBM 的 SHAP 分解（哪几个因子在推）＋ 启动门禁的倾斜四分项（位置/高点/量/资金流）。产物在 <code>output/results_cn/attribution/&lt;date&gt;/</code>。"
    >
      <template #actions>
        <select v-model="date" class="picker">
          <option v-for="d in dates" :key="d" :value="d">{{ d }}</option>
        </select>
        <button class="btn-ghost" :disabled="loading" @click="reload">刷新</button>
      </template>
    </PageHeader>

    <StateBlock v-if="error" state="error" title="取不到归因数据" :detail="error" :retry="reload" />
    <StateBlock v-else-if="!data" state="loading" title="加载中…" />
    <StateBlock v-else-if="!dates.length" state="empty" title="还没有归因产物"
                detail="每日流程会自动生成；手动跑：uv run python scripts/explain_selection.py <date> && uv run python scripts/pick_attribution_report.py <date>" />

    <template v-else>
      <div class="panel">
        <div class="pill-row">
          <span class="pill">交易日 <b>{{ data.date }}</b></span>
          <span class="pill"><b>{{ data.n }}</b> 只</span>
          <span class="pill">族维度 <b>{{ data.family_columns.length }}</b> 类</span>
          <span class="pill dim">在最终名单 <b>{{ nSelected }}</b> · 在候选池 <b>{{ nPreselected }}</b></span>
        </div>

        <table class="data-table">
          <thead>
            <tr>
              <th class="text-left">代码</th><th class="text-left">策略</th><th class="text-left">通道</th>
              <th class="text-left">腿</th><th>rank</th><th>模型分</th>
              <th>门禁 tilt</th><th class="text-left">tilt 拆分（位置/高点/量/资金）</th>
              <th>在名单</th><th>权重</th><th>实现收益</th>
              <th class="text-left">正向贡献</th><th class="text-left">负向贡献</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in data.rows" :key="r.stock_code" :class="{ strong: truthy(r.in_selection) }">
              <td class="mono link text-left" @click="gotoStock(r.stock_code)">{{ r.stock_code }}</td>
              <td class="text-left tiny">{{ r.strategy || '—' }}</td>
              <td class="text-left tiny">{{ r.channel || '—' }}</td>
              <td class="text-left tiny">{{ r.sleeve || '—' }}</td>
              <td>{{ r.rank ?? '—' }}</td>
              <td>{{ num(r.raw_pred, 4) }}</td>
              <td :class="tone(r.tilt_points)">{{ num(r.tilt_points, 2) }}</td>
              <td class="text-left tiny dim">
                {{ num(r.tilt_from_dist, 1) }} / {{ num(r.tilt_from_high, 1) }} /
                {{ num(r.tilt_from_volume, 1) }} / {{ num(r.tilt_from_flow, 1) }}
              </td>
              <td>{{ truthy(r.in_selection) ? '✅' : '—' }}</td>
              <td>{{ r.target_weight ? (r.target_weight * 100).toFixed(2) + '%' : '—' }}</td>
              <td :class="tone(r['ret_close_to_last_%'])">{{ pct(r['ret_close_to_last_%']) }}</td>
              <td class="text-left tiny pos" :title="r.top_positive_factors">{{ short(r.top_positive_factors) }}</td>
              <td class="text-left tiny neg" :title="r.top_negative_factors">{{ short(r.top_negative_factors) }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="panel" v-if="selected">
        <h6>
          <span class="link" @click="gotoStock(selected.stock_code)">{{ selected.stock_code }}</span>
          族贡献（SHAP 按因子族求和）
        </h6>
        <BarChart :bars="familyBars" :label-width="150" :max-height="300" />
      </div>

      <div class="panel" v-if="data.winners_vs_losers && !data.winners_vs_losers.includes('nan')">
        <h6>赢家 vs 输家（按 T+1 开盘→最新收盘）</h6>
        <pre class="md">{{ data.winners_vs_losers }}</pre>
      </div>

      <div class="panel">
        <h6>报告原文（pick_attribution.md）</h6>
        <pre class="md">{{ data.markdown }}</pre>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { getAttributionDetail, getAttributionIndex } from '@/api/client'
import type { AttributionDetail } from '@/types/api'
import PageHeader from '@/components/ui/PageHeader.vue'
import StateBlock from '@/components/ui/StateBlock.vue'
import BarChart, { type Bar } from '@/components/ui/BarChart.vue'

const router = useRouter()
const dates = ref<string[]>([])
const date = ref('')
const data = ref<AttributionDetail | null>(null)
const error = ref('')
const loading = ref(false)

async function reload() {
  loading.value = true
  try {
    if (!dates.value.length) {
      const idx = await getAttributionIndex()
      dates.value = idx.dates
      date.value = dates.value[0] || ''
    }
    if (date.value) data.value = await getAttributionDetail(date.value)
    error.value = ''
  } catch (e) {
    error.value = String(e)
  } finally {
    loading.value = false
  }
}
onMounted(reload)
watch(date, () => void reload())

function gotoStock(code: string) { if (code) router.push({ path: '/kline', query: { code } }) }
const truthy = (v: unknown) => v === true || v === 1 || v === 'True' || v === 'true' || v === '1'
const num = (v: unknown, nd = 2) =>
  v === null || v === undefined || Number.isNaN(Number(v)) ? '—' : Number(v).toFixed(nd)
const pct = (v: unknown) =>
  v === null || v === undefined || Number.isNaN(Number(v)) ? '—' : `${(Number(v) * 100).toFixed(2)}%`
const tone = (v: unknown) =>
  v === null || v === undefined || Number.isNaN(Number(v)) ? '' : Number(v) >= 0 ? 'up' : 'down'
/** 因子列表很长，只显示前 3 个的"名字(+值)"，完整值放 title */
function short(text: unknown) {
  if (!text) return '—'
  const parts = String(text).split(';').slice(0, 3)
  return parts.join('; ') + (String(text).split(';').length > 3 ? ' …' : '')
}

const nSelected = computed(() => (data.value?.rows || []).filter((r) => truthy(r.in_selection)).length)
const nPreselected = computed(() => (data.value?.rows || []).filter((r) => truthy(r.in_preselected)).length)
const selected = computed(() => (data.value?.rows || []).find((r) => truthy(r.in_selection)) || data.value?.rows[0])
const familyBars = computed<Bar[]>(() =>
  ((selected.value?.families as { name: string; value: number }[]) || []).map((f) => ({
    label: f.name, value: f.value,
    text: `${f.value > 0 ? '+' : ''}${f.value.toFixed(4)}`,
    tone: f.value >= 0 ? 'pos' : 'neg',
  })),
)
</script>

<style scoped>
.picker { padding: 5px 10px; background: var(--bg-input); color: var(--text-primary);
  border: 1px solid var(--border-color); border-radius: var(--r-2); }
.data-table tr.strong td { box-shadow: inset 2px 0 0 var(--accent-up); }
.pos { color: var(--accent-up); }
.neg { color: var(--accent-down); }
.md { font-family: var(--font-mono); font-size: var(--fs-xs); color: var(--text-muted);
  white-space: pre-wrap; max-height: 420px; overflow: auto; margin: 0; line-height: 1.6; }
</style>
