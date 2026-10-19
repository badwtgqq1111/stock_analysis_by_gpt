<template>
  <section v-if="data?.available" class="panel market-strip">
    <div class="ms-head">
      <div class="ms-title">
        <b>大盘速览</b>
        <span class="tiny dim">{{ data.asof || '—' }} 收盘</span>
      </div>
      <div class="ms-turnover">
        <span class="tiny dim">全 A 成交额</span>
        <b class="mono">{{ money(data.total_amount_yi) }}</b>
        <span class="tiny dim" v-if="data.n_stocks">· {{ data.n_stocks.toLocaleString('zh-CN') }} 只</span>
        <span class="tiny dim" v-if="data.turnover_asof && data.turnover_asof !== data.asof">
          · 截至 {{ data.turnover_asof }}
        </span>
      </div>
      <router-link class="link tiny ms-more" to="/market">大盘详情 / K 线 →</router-link>
    </div>
    <div class="ms-grid">
      <button v-for="q in core" :key="q.code" class="ms-card" @click="open(q.code)"
              :title="`${q.name} ${q.code}：打开指数 K 线`">
        <div class="ms-card-top">
          <span class="ms-name">{{ q.name }}</span>
          <span class="ms-code mono tiny">{{ q.code }}</span>
        </div>
        <div class="ms-card-mid">
          <b class="ms-price mono">{{ fmt(q.close) }}</b>
          <span class="ms-chg mono" :class="tone(q.pct_chg)">{{ pct(q.pct_chg) }}</span>
        </div>
        <Sparkline :values="(q.spark || []).map(x => x.close)" :height="26"
                   :color="(q.pct_chg ?? 0) >= 0 ? 'var(--state-bad)' : 'var(--state-ok)'" />
      </button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { getMarketOverview } from '@/api/client'
import type { MarketOverview } from '@/types/api'
import Sparkline from '@/components/ui/Sparkline.vue'

const router = useRouter()
const data = ref<MarketOverview | null>(null)
const core = computed(() => (data.value?.indices || []).slice(0, 4))

onMounted(async () => {
  try { data.value = await getMarketOverview(20) } catch { /* 首页不因大盘接口失败而阻塞 */ }
})

function open(code: string) { void router.push({ path: '/market', query: { code } }) }
function fmt(v: number | null) {
  return v == null ? '—' : v.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}
function pct(v: number | null) {
  return v == null ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
}
function money(v: number | null) {
  return v == null ? '—' : `${v.toLocaleString('zh-CN', { maximumFractionDigits: 1 })} 亿`
}
function tone(v: number | null) { return (v ?? 0) >= 0 ? 'up' : 'down' }
</script>

<style scoped>
.market-strip { padding: var(--sp-3) var(--sp-4); }
.ms-head { display: flex; align-items: center; gap: var(--sp-4); flex-wrap: wrap; margin-bottom: var(--sp-2); }
.ms-title { display: flex; align-items: baseline; gap: var(--sp-2); }
.ms-title b { font-size: var(--fs-lg); }
.ms-turnover { display: flex; align-items: baseline; gap: 6px; margin-left: auto; }
.ms-turnover b { font-size: var(--fs-lg); color: var(--accent-gold); }
.ms-more { white-space: nowrap; }
.ms-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--sp-2); }
.ms-card {
  display: block; text-align: left; padding: var(--sp-2) var(--sp-3);
  background: var(--bg-input); border: 1px solid var(--border-color);
  border-radius: var(--r-3); color: var(--text-primary); cursor: pointer;
  transition: border-color 0.15s, transform 0.15s;
}
.ms-card:hover { border-color: var(--accent-gold); transform: translateY(-1px); }
.ms-card-top, .ms-card-mid { display: flex; align-items: baseline; justify-content: space-between; gap: 6px; }
.ms-name { font-weight: 700; font-size: var(--fs-md); }
.ms-code { color: var(--text-dim); }
.ms-price { font-size: var(--fs-xl); }
.ms-chg { font-size: var(--fs-sm); }
.ms-card :deep(.sparkline) { margin-top: 4px; opacity: 0.85; }
@media (max-width: 900px) {
  .ms-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .ms-turnover { margin-left: 0; }
}
</style>
