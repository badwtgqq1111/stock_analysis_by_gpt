<template>
  <div class="stock-selector" ref="rootRef" @focusout="onFocusOut">
    <div class="input-wrapper">
      <input
        ref="inputRef"
        v-model="searchText"
        @focus="onFocus"
        @input="onInput"
        @keydown.enter.prevent="selectActive"
        @keydown.arrow-down.prevent="moveDown"
        @keydown.arrow-up.prevent="moveUp"
        @keydown.esc="showDropdown = false"
        placeholder="代码 / 名称 / 拼音首字母，如 600861、北京人力、bjrl"
        autocomplete="off"
        aria-label="全市场搜索股票" role="combobox" :aria-expanded="showDropdown"
        aria-autocomplete="list"
      />
      <span v-if="modelValue" class="code-badge">
        <template v-if="holdingCodes.includes(modelValue)">★ 持仓</template><template v-else>当前</template> {{ modelValue }} <template v-if="selectedName">{{ selectedName }}</template>
      </span>
    </div>
    <div class="filters">
      <label class="chk"><input type="checkbox" v-model="mainBoardOnly" @change="runSearch(searchText)" /> 只主板</label>
      <label class="chk"><input type="checkbox" v-model="includeDelisted" @change="runSearch(searchText)" /> 含退市/摘牌</label>
      <span class="count" v-if="results.length">{{ results.length }} 条</span>
    </div>
    <ul v-if="showDropdown && (results.length > 0 || searchText.trim())" class="dropdown" role="listbox" aria-label="股票搜索结果">
      <li v-if="loading" class="dropdown-hint">搜索中…</li>
      <li v-else-if="searchError" class="dropdown-hint search-error" role="alert">{{ searchError }}，请重新输入重试。</li>
      <li v-else-if="results.length === 0" class="dropdown-hint">
        没有匹配「{{ searchText }}」——可试 6 位代码 / 中文名 / 拼音首字母（如 bjrl）
      </li>
      <li
        v-for="(opt, i) in results"
        role="option" :aria-selected="i === activeIndex"
        :key="opt.code"
        :class="['dropdown-item', { active: i === activeIndex, selected: opt.code === modelValue }]"
        @mousedown.prevent="select(opt)"
        @mouseenter="activeIndex = i"
      >
        <span v-if="holdingCodes.includes(opt.code)" class="star">★</span>
        <span class="code">{{ opt.code }}</span>
        <span class="name">{{ opt.name || '—' }}</span>
        <span v-for="f in (opt.flags || [])" :key="f" class="flag" :class="flagClass(f)">{{ f }}</span>
        <span class="meta">{{ opt.market === 'HK' ? '港股' : opt.board }}<template v-if="opt.industry"> · {{ opt.industry }}</template></span>
      </li>
    </ul>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { getStocks, searchStocks } from '@/api/client'
import type { StockSearchHit } from '@/api/client'

const props = defineProps<{ modelValue: string | null }>()
const emit = defineEmits<{ 'update:modelValue': [value: string]; selected: [value: StockSearchHit] }>()

const searchText = ref('')
const showDropdown = ref(false)
const activeIndex = ref(0)
const results = ref<StockSearchHit[]>([])
const loading = ref(false)
const searchError = ref('')
const chosenName = ref('')
let requestId = 0
const holdingCodes = ref<string[]>([])
const mainBoardOnly = ref(false)      // 策略第六层过滤：只做沪深主板
const includeDelisted = ref(false)    // 默认不把退市/摘牌代码混进来
const rootRef = ref<HTMLElement | null>(null)

let timer: ReturnType<typeof setTimeout> | null = null

const selectedName = computed(() => {
  const hit = results.value.find((r) => r.code === props.modelValue)
  return hit?.name || chosenName.value
})

async function loadHoldings() {
  try {
    const res = await getStocks()
    holdingCodes.value = res.stocks.filter((s) => s.is_selected).map((s) => s.code)
  } catch { /* 选股清单拿不到不影响搜索 */ }
}

async function runSearch(q: string) {
  if (timer) { clearTimeout(timer); timer = null }
  const id = ++requestId
  const query = q.trim()
  results.value = []; searchError.value = ''; activeIndex.value = 0
  if (!query) { loading.value = false; return }
  loading.value = true
  try {
    const hits = await searchStocks(query, {
      limit: 20,
      main_board_only: mainBoardOnly.value,
      include_delisted: includeDelisted.value,
    })
    if (id === requestId) results.value = hits
  } catch {
    if (id === requestId) searchError.value = '股票搜索暂时失败'
  } finally {
    if (id === requestId) loading.value = false
  }
}

function onInput() {
  ++requestId // 输入改变即作废旧结果，不能在防抖期间选到旧股票
  results.value = []; searchError.value = ''; activeIndex.value = 0
  loading.value = !!searchText.value.trim()
  showDropdown.value = true
  if (timer) clearTimeout(timer)
  timer = setTimeout(() => runSearch(searchText.value), 220)
}

function onFocus() {
  showDropdown.value = true
  if (!results.value.length) runSearch(searchText.value)
}

function flagClass(flag: string) {
  if (flag.startsWith('已退市') || flag.startsWith('ST')) return 'flag-bad'
  if (flag.startsWith('次新')) return 'flag-warn'
  return 'flag-info'
}

/** 焦点仍在本组件内（例如点到"只主板/含退市"勾选框）时不要收起下拉。 */
function onFocusOut(event: FocusEvent) {
  const next = event.relatedTarget as Node | null
  if (next && rootRef.value?.contains(next)) return
  setTimeout(() => { showDropdown.value = false }, 150)
}

function moveDown() {
  if (!results.value.length) return
  activeIndex.value = (activeIndex.value + 1) % results.value.length
}
function moveUp() {
  if (!results.value.length) return
  activeIndex.value = (activeIndex.value - 1 + results.value.length) % results.value.length
}
function selectActive() {
  if (loading.value || searchError.value) return
  const hit = results.value[activeIndex.value]
  if (hit) select(hit)
}
function select(opt: StockSearchHit) {
  ++requestId
  if (timer) { clearTimeout(timer); timer = null }
  loading.value = false
  chosenName.value = opt.name || ''
  emit('selected', opt)
  emit('update:modelValue', opt.code)
  searchText.value = `${opt.code} ${opt.name}`.trim()
  showDropdown.value = false
}

watch(() => props.modelValue, (val) => {
  if (val && !searchText.value.startsWith(val)) {
    ++requestId
    if (timer) { clearTimeout(timer); timer = null }
    loading.value = false; searchError.value = ''; showDropdown.value = false
    chosenName.value = ''
    const hit = results.value.find((r) => r.code === val)
    searchText.value = hit ? `${hit.code} ${hit.name}` : val
  }
}, { immediate: true })

onMounted(loadHoldings)
onUnmounted(() => { ++requestId; if (timer) clearTimeout(timer) })
</script>

<style scoped>
.stock-selector { position: relative; }
.input-wrapper { display: flex; flex-direction: column; gap: 4px; }
.input-wrapper input {
  padding: 6px 10px;
  background: var(--bg-input);
  border: 1px solid var(--border-color);
  border-radius: 4px;
  color: var(--text-primary);
  outline: none;
  min-width: 0;
  width: 100%;
  box-sizing: border-box;
}
.code-badge { font-size: 11px; color: var(--accent-gold); }
.dropdown {
  position: absolute;
  z-index: 30;
  top: 100%;
  left: 0;
  right: 0;
  max-height: 320px;
  overflow-y: auto;
  margin: 4px 0 0;
  padding: 4px;
  list-style: none;
  background: var(--bg-card);
  border: 1px solid var(--border-color);
  border-radius: 6px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
}
.search-error { color: var(--accent-down); }
.dropdown-item {
  flex-wrap: wrap;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 12px;
}
.dropdown-item.active { background: rgba(255, 209, 102, 0.12); }
.dropdown-item.selected { color: var(--accent-gold); }
.dropdown-item .code { font-family: ui-monospace, Menlo, monospace; color: var(--text-primary); }
.dropdown-item .name { color: var(--text-primary); }
.dropdown-item .meta { margin-left: auto; color: var(--text-muted); font-size: 11px; }
.dropdown-hint { padding: 6px 8px; color: var(--text-muted); font-size: 11px; }
.filters { display: flex; align-items: center; gap: 10px; margin-top: 4px; font-size: 11px; color: var(--text-muted); }
.filters .chk { display: flex; align-items: center; gap: 4px; }
.filters .count { margin-left: auto; color: var(--accent-gold); }
.flag { font-size: 10px; padding: 0 5px; border-radius: 7px; border: 1px solid var(--border-color); white-space: nowrap; }
.flag-bad { color: var(--accent-up); border-color: var(--accent-up); }
.flag-warn { color: var(--accent-orange); border-color: var(--accent-orange); }
.flag-info { color: var(--text-muted); }
.star { color: var(--accent-gold); }
</style>
