<template>
  <div class="panel watchlist">
    <h6>
      自选股
      <span class="dim tiny">{{ local.length }} 自选 / {{ visibleRows.length }} 显示</span>
      <button class="btn-ghost btn-sm add-btn" @click="clearAll" v-if="local.length" title="清空本地自选">清空</button>
    </h6>

    <!-- 快速分组：直接取各策略的最新名单，省得一只一只搜 -->
    <div class="groups">
      <button v-for="g in groups" :key="g.key" class="chip" :class="{ active: group === g.key }"
              :title="g.note" @click="toggleGroup(g.key)">
        {{ g.label }}<span v-if="g.rows.length" class="dim tiny"> {{ g.rows.length }}</span>
      </button>
    </div>

    <div class="search">
      <input v-model="query" placeholder="代码 / 名称 / 拼音，回车添加" @keydown.enter="addByQuery"
             @input="onInput" />
      <button class="btn-ghost btn-sm" @click="addByQuery">加入</button>
    </div>
    <ul v-if="suggestions.length && query" class="suggest">
      <li v-for="s in suggestions" :key="s.code" @mousedown.prevent="addStock(s.code, s.name)">
        <span class="mono">{{ s.code }}</span> {{ s.name }}
      </li>
    </ul>

    <ul class="rows">
      <li v-for="row in visibleRows" :key="row.code" :class="{ active: row.code === modelValue }"
          @click="$emit('update:modelValue', row.code)">
        <span class="mono code">{{ row.code }}</span>
        <span class="name">{{ row.name || '—' }}</span>
        <span class="tag dim tiny" v-if="row.tag">{{ row.tag }}</span>
        <button class="star" :title="row.local ? '从自选移除' : '加入自选'"
                @click.stop="toggleLocal(row)">{{ row.local ? '★' : '☆' }}</button>
      </li>
      <li v-if="!visibleRows.length" class="empty tiny">还没有自选股：用上面的搜索或分组添加</li>
    </ul>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { getPaperStatus, getSelection, searchStocks } from '@/api/client'
import type { StockSearchHit } from '@/api/client'
import { useWatchlist } from '@/composables/useWatchlist'

const props = defineProps<{ modelValue: string | null }>()
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()

// 自选状态来自全局 composable（`composables/useWatchlist.ts`）：
// 选股日历 / 行业热度页勾的 ★ 会实时出现在这里，反之亦然。
const watchlist = useWatchlist()

const GROUP_META = [
  { key: 'cn_arm_concept', label: '概念手臂', note: '板块轮动手臂（arm_concept）当日名单' },
  { key: 'cn_arm', label: '研究手臂', note: 'base_v1 研究手臂当日名单' },
  { key: 'cn_ensemble', label: '生产名单', note: 'preselect→pk 的生产名单' },
  { key: 'paper', label: '模拟盘持仓', note: '模拟盘当前持仓' },
] as const

interface Row { code: string; name: string; tag?: string; local: boolean }

const local = watchlist.codes
const groups = ref<{ key: string; label: string; note: string; rows: { code: string; name: string }[] }[]>(
  GROUP_META.map((g) => ({ ...g, rows: [] })),
)
const group = ref<string>('cn_arm_concept')
const query = ref('')
const suggestions = ref<StockSearchHit[]>([])
let timer: ReturnType<typeof setTimeout> | null = null

const visibleRows = computed<Row[]>(() => {
  const seen = new Map<string, Row>()
  const active = groups.value.find((g) => g.key === group.value)
  for (const code of local.value) {
    seen.set(code, { code, name: watchlist.names.value[code] || '', local: true, tag: '自选' })
  }
  for (const item of active?.rows || []) {
    const exist = seen.get(item.code)
    if (exist) { exist.tag = exist.tag ? `${exist.tag}·${active!.label}` : active!.label }
    else seen.set(item.code, { code: item.code, name: item.name, local: false, tag: active!.label })
  }
  return [...seen.values()]
})

function toggleLocal(row: Row) {
  watchlist.toggle(row.code, row.name)
}

function addStock(code: string, name = '') {
  watchlist.add(code, name)
  emit('update:modelValue', code)
}

async function addByQuery() {
  const q = query.value.trim()
  if (!q) return
  if (/^\d{6}(\.[A-Z]{2})?$/.test(q)) { addStock(q); query.value = ''; suggestions.value = []; return }
  try {
    const hits = await searchStocks(q, { limit: 5 })
    if (hits.length) { addStock(hits[0].code, hits[0].name); query.value = ''; suggestions.value = [] }
  } catch { /* 搜索失败不阻断 */ }
}

function onInput() {
  if (timer) clearTimeout(timer)
  const q = query.value.trim()
  if (!q) { suggestions.value = []; return }
  timer = setTimeout(async () => {
    try { suggestions.value = (await searchStocks(q, { limit: 6 })).slice(0, 6) } catch { suggestions.value = [] }
  }, 220)
}

async function toggleGroup(key: string) {
  group.value = key
  const target = groups.value.find((g) => g.key === key)
  if (target && !target.rows.length) await loadGroup(key)
}

async function loadGroup(key: string) {
  const target = groups.value.find((g) => g.key === key)
  if (!target) return
  if (key === 'paper') {
    try {
      const status: any = await getPaperStatus()
      const positions = (status?.positions || []) as { stock_code: string; name?: string }[]
      target.rows = positions.map((p) => ({ code: String(p.stock_code), name: String(p.name || '') }))
    } catch { target.rows = [] }
    return
  }
  try {
    const data: any = await getSelection(key)
    target.rows = (data?.rows || []).map((r: any) => ({
      code: String(r.stock_code), name: String(r.stock_name || r.name || ''),
    }))
    watchlist.rememberMany(target.rows)
  } catch { target.rows = [] }
}

function clearAll() {
  watchlist.clear()
}

onMounted(async () => {
  await Promise.all(GROUP_META.map((g) => loadGroup(g.key)))
  if (!props.modelValue && visibleRows.value.length) emit('update:modelValue', visibleRows.value[0].code)
})
</script>

<style scoped>
.watchlist .groups { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: var(--sp-2); }
.chip { padding: 3px 8px; border-radius: var(--r-pill); border: 1px solid var(--border-color);
  background: var(--bg-input); color: var(--text-muted); font-size: var(--fs-xs); cursor: pointer; }
.chip.active { border-color: var(--accent-gold); color: var(--accent-gold); background: rgba(255, 209, 102, 0.08); }
.search { display: flex; gap: 6px; margin-bottom: var(--sp-2); }
.search input { flex: 1; min-width: 0; padding: 5px 8px; background: var(--bg-input);
  border: 1px solid var(--border-color); border-radius: var(--r-2); color: var(--text-primary); font-size: var(--fs-sm); }
.suggest { list-style: none; margin: 0 0 var(--sp-2); padding: 4px; background: var(--bg-card);
  border: 1px solid var(--border-color); border-radius: var(--r-2); max-height: 160px; overflow: auto; }
.suggest li { padding: 4px 6px; font-size: var(--fs-xs); cursor: pointer; border-radius: var(--r-1); }
.suggest li:hover { background: rgba(255, 209, 102, 0.1); }
.rows { list-style: none; margin: 0; padding: 0; max-height: 46vh; overflow: auto; }
.rows li { display: flex; align-items: center; gap: 6px; padding: 4px 6px; border-radius: var(--r-2);
  cursor: pointer; font-size: var(--fs-sm); }
.rows li:hover { background: rgba(255, 255, 255, 0.04); }
.rows li.active { background: rgba(255, 209, 102, 0.1); color: var(--accent-gold); }
.rows .code { color: inherit; }
.rows .name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.star { background: none; border: none; color: var(--accent-gold); cursor: pointer; font-size: var(--fs-md); padding: 0 2px; }
.add-btn { margin-left: auto; }
</style>
