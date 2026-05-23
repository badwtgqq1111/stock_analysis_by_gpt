<template>
  <div class="page">
    <h4 class="page-title">LightGBM 选股结果</h4>

    <div v-if="data?.empty" class="empty-state">暂无选股数据，请先运行选股模型生成 output/ 目录下的 CSV 文件</div>

    <template v-else-if="data">
      <div class="two-col">
        <div class="col-main">
          <h6>入选股票列表</h6>
          <table class="data-table">
            <thead>
              <tr>
                <th v-for="col in data.columns" :key="col.key" @click="sortBy(col.key)">
                  {{ col.title }} {{ sortKey === col.key ? (sortAsc ? '▲' : '▼') : '' }}
                </th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="row in pagedRows" :key="row.stock_code"
                :class="{ selected: selectedCode === row.stock_code, strong: row.signal_tier === 'strong' }"
                @click="selectedCode = row.stock_code"
              >
                <td v-for="col in data.columns" :key="col.key">
                  <template v-if="col.key === 'stock_code'">
                    {{ row.stock_code }} {{ row.stock_name }}
                  </template>
                  <template v-else>
                    {{ fmtVal(row[col.key]) }}
                  </template>
                </td>
              </tr>
            </tbody>
          </table>
          <div class="pagination">
            <button :disabled="page <= 1" @click="page--">上一页</button>
            <span>{{ page }} / {{ totalPages }}</span>
            <button :disabled="page >= totalPages" @click="page++">下一页</button>
          </div>
        </div>

        <div class="col-side">
          <h6>Top 20 综合评分</h6>
          <div class="bar-chart">
            <div v-for="r in scoreBars" :key="r.code" class="bar-row">
              <span class="bar-label">{{ r.code }}</span>
              <div class="bar-track">
                <div class="bar-fill" :class="r.tier === 'strong' ? 'bar-strong' : 'bar-normal'"
                  :style="{ width: r.pct + '%' }"></div>
              </div>
              <span class="bar-val">{{ r.score.toFixed(1) }}</span>
            </div>
          </div>
        </div>
      </div>

      <hr>
      <div>
        <h6>SHAP 因子贡献分析</h6>
        <select v-model="shapCode" @change="loadShap" class="stock-select">
          <option v-for="r in selectableStocks" :key="r.stock_code" :value="r.stock_code">
            ★ {{ r.stock_code }} {{ r.stock_name }}
          </option>
        </select>
        <div class="bar-chart shap-chart" v-if="shapFeatures.length > 0" :style="{ height: shapFeatures.length * 35 + 'px' }">
          <div v-for="f in shapFeatures" :key="f.name" class="bar-row">
            <span class="bar-label shap-label">{{ f.name }}</span>
            <div class="bar-track">
              <div class="bar-fill" :class="f.direction === 'positive' ? 'bar-up' : 'bar-down'"
                :style="{ width: f.pct + '%', marginLeft: f.offset + '%' }"></div>
            </div>
            <span class="bar-val" :class="f.direction === 'positive' ? 'up' : 'down'">
              {{ f.direction === 'positive' ? '+' : '' }}{{ f.value.toFixed(4) }}
            </span>
          </div>
        </div>
      </div>
    </template>

    <div v-else class="empty-state">加载中...</div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { getSelection, getShap } from '@/api/client'
import type { SelectionResponse, ShapFeature } from '@/types/api'

const data = ref<SelectionResponse | null>(null)
const selectedCode = ref<string | null>(null)
const shapCode = ref('')
const shapFeatures = ref<(ShapFeature & { pct: number; offset: number })[]>([])
const sortKey = ref('ranking_score')
const sortAsc = ref(false)
const page = ref(1)
const perPage = 15

onMounted(async () => { data.value = await getSelection() })

const selectableStocks = computed(() => data.value?.rows ?? [])

const sortedRows = computed(() => {
  if (!data.value) return []
  const rows = [...data.value.rows]
  rows.sort((a, b) => {
    const va = a[sortKey.value] ?? 0, vb = b[sortKey.value] ?? 0
    return sortAsc.value ? (va > vb ? 1 : -1) : (va < vb ? 1 : -1)
  })
  return rows
})

const totalPages = computed(() => Math.max(1, Math.ceil(sortedRows.value.length / perPage)))
const pagedRows = computed(() => {
  const start = (page.value - 1) * perPage
  return sortedRows.value.slice(start, start + perPage)
})

const scoreBars = computed(() => {
  if (!data.value) return []
  const rows = [...data.value.rows].filter(r => r.ranking_score != null)
  rows.sort((a, b) => (b.ranking_score ?? 0) - (a.ranking_score ?? 0))
  const top20 = rows.slice(0, 20)
  const max = top20[0]?.ranking_score ?? 1
  return top20.map(r => ({
    code: r.stock_code, name: r.stock_name, score: r.ranking_score ?? 0,
    tier: r.signal_tier, pct: ((r.ranking_score ?? 0) / max) * 100,
  }))
})

async function loadShap() {
  if (!shapCode.value) return
  const res = await getShap(shapCode.value)
  const maxAbs = Math.max(...res.features.map(f => Math.abs(f.value)), 0.01)
  shapFeatures.value = res.features.map(f => {
    const pct = Math.abs(f.value) / maxAbs * 80
    return {
      ...f, pct,
      offset: f.direction === 'negative' ? (95 - pct) : 5,
    }
  })
}

function sortBy(key: string) {
  if (sortKey.value === key) sortAsc.value = !sortAsc.value
  else { sortKey.value = key; sortAsc.value = false }
  page.value = 1
}

function fmtVal(v: any): string {
  if (v === null || v === undefined) return '-'
  if (typeof v === 'number') return v.toFixed(2)
  return String(v)
}

watch(selectedCode, code => {
  if (code) { shapCode.value = code; loadShap() }
})
</script>

<style scoped>
.page { max-width: 1400px; margin: 0 auto; }
.page-title { margin-bottom: 10px; font-size: 18px; }
.two-col { display: flex; gap: 20px; }
.col-main { flex: 1; min-width: 0; }
.col-side { width: 380px; flex-shrink: 0; }
h6 { font-size: 13px; color: var(--text-muted); margin-bottom: 6px; }
.data-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.data-table th { background: #1a1a2e; color: var(--text-muted); padding: 6px 8px; text-align: left; cursor: pointer; border-bottom: 1px solid var(--border-color); white-space: nowrap; }
.data-table td { padding: 5px 8px; border-bottom: 1px solid rgba(42,46,57,0.5); white-space: nowrap; }
.data-table tr:hover { background: rgba(255,255,255,0.03); }
.data-table tr.selected { background: rgba(239,71,111,0.08); }
.data-table tr.strong { background: rgba(239,71,111,0.12); }
.pagination { display: flex; gap: 10px; align-items: center; justify-content: center; padding: 10px 0; font-size: 12px; }
.pagination button { padding: 3px 10px; background: var(--bg-input); color: var(--text-primary); border: 1px solid var(--border-color); border-radius: 3px; cursor: pointer; }
.pagination button:disabled { opacity: 0.4; cursor: default; }
.bar-chart { padding: 4px 0; }
.bar-row { display: flex; align-items: center; gap: 4px; height: 24px; font-size: 11px; }
.bar-label { width: 55px; text-align: right; color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bar-track { flex: 1; height: 12px; background: rgba(42,46,57,0.5); border-radius: 2px; position: relative; overflow: hidden; }
.bar-fill { height: 100%; border-radius: 2px; min-width: 2px; }
.bar-strong { background: var(--accent-up); }
.bar-normal { background: var(--accent-gold); }
.bar-up { background: var(--accent-up); }
.bar-down { background: var(--accent-down); }
.bar-val { width: 55px; text-align: left; color: var(--text-muted); }
.bar-val.up { color: var(--accent-up); }
.bar-val.down { color: var(--accent-down); }
.shap-label { width: 120px; }
.shap-chart { min-height: 200px; }
.stock-select { margin-bottom: 10px; padding: 6px 10px; background: var(--bg-input); color: var(--text-primary); border: 1px solid var(--border-color); border-radius: 4px; max-width: 300px; }
hr { border-color: var(--border-color); margin: 16px 0; }
.empty-state { padding: 60px 0; text-align: center; color: var(--text-muted); }
</style>
