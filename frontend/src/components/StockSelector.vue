<template>
  <div class="stock-selector" ref="rootRef">
    <div class="input-wrapper">
      <input
        ref="inputRef"
        v-model="searchText"
        @focus="showDropdown = true"
        @blur="onBlur"
        @keydown.enter="selectFirst"
        @keydown.arrow-down.prevent="moveDown"
        @keydown.arrow-up.prevent="moveUp"
        placeholder="输入股票代码或名称搜索..."
        autocomplete="off"
      />
      <span v-if="modelValue" class="code-badge">
        ★ {{ modelValue }} <template v-if="selectedName">{{ selectedName }}</template>
      </span>
    </div>
    <ul v-if="showDropdown && filteredOptions.length > 0" class="dropdown">
      <li
        v-for="(opt, i) in filteredOptions"
        :key="opt.code"
        :class="['dropdown-item', { active: i === activeIndex, selected: opt.code === modelValue }]"
        @mousedown.prevent="select(opt)"
        @mouseenter="activeIndex = i"
      >
        <span v-if="opt.is_selected" class="star">★</span>
        <span class="code">{{ opt.code }}</span>
        <span class="name">{{ opt.name }}</span>
      </li>
    </ul>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick } from 'vue'
import { useStockList } from '@/composables/useStockList'

const props = defineProps<{ modelValue: string | null }>()
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()

const { stocks, filtered, load } = useStockList()

const searchText = ref('')
const showDropdown = ref(false)
const activeIndex = ref(0)
const rootRef = ref<HTMLElement | null>(null)
const inputRef = ref<HTMLInputElement | null>(null)

load()

const selectedName = computed(() => {
  if (!props.modelValue) return ''
  const s = stocks.value.find(s => s.code === props.modelValue)
  return s ? ` ${s.name}` : ''
})

const filteredOptions = computed(() => {
  const q = searchText.value.toLowerCase().trim()
  if (!q) return stocks.value
  return stocks.value.filter(
    s => s.code.includes(q) || s.name.toLowerCase().includes(q),
  )
})

watch(() => props.modelValue, (val) => {
  if (val) searchText.value = ''
})

function select(opt: { code: string }) {
  emit('update:modelValue', opt.code)
  searchText.value = ''
  showDropdown.value = false
}

function onBlur() {
  setTimeout(() => { showDropdown.value = false }, 150)
}

function selectFirst() {
  const opts = filteredOptions.value
  if (opts.length > 0 && activeIndex.value < opts.length) {
    select(opts[activeIndex.value])
  }
}

function moveDown() {
  showDropdown.value = true
  activeIndex.value = Math.min(activeIndex.value + 1, filteredOptions.value.length - 1)
}

function moveUp() {
  showDropdown.value = true
  activeIndex.value = Math.max(activeIndex.value - 1, 0)
}
</script>

<style scoped>
.stock-selector {
  position: relative;
}
.input-wrapper {
  position: relative;
}
.input-wrapper input {
  width: 100%;
  padding: 6px 10px;
  background: var(--bg-input);
  border: 1px solid var(--border-color);
  border-radius: 4px;
  color: var(--text-primary);
  outline: none;
  transition: border-color 0.15s;
}
.input-wrapper input:focus {
  border-color: var(--accent-blue);
}
.code-badge {
  position: absolute;
  right: 8px;
  top: 50%;
  transform: translateY(-50%);
  font-size: 12px;
  color: var(--accent-gold);
  pointer-events: none;
}
.dropdown {
  position: absolute;
  top: 100%;
  left: 0;
  right: 0;
  max-height: 240px;
  overflow-y: auto;
  background: #1a1d24;
  border: 1px solid var(--border-color);
  border-radius: 4px;
  margin-top: 2px;
  list-style: none;
  z-index: 100;
}
.dropdown-item {
  padding: 5px 10px;
  cursor: pointer;
  display: flex;
  gap: 8px;
  align-items: center;
  font-size: 13px;
}
.dropdown-item:hover, .dropdown-item.active {
  background: #252a33;
}
.dropdown-item.selected {
  color: var(--accent-gold);
}
.star { color: var(--accent-gold); }
.code { font-weight: 600; min-width: 50px; }
.name { color: var(--text-muted); }
</style>
