/**
 * 自选股 —— 全局单例（localStorage 持久化）。
 *
 * 为什么要抽出来：K 线页侧栏、选股日历、行业热度"行业内个股"、选股结果页都需要
 * 同一份自选，2026-10-09 之前只有 `StockWatchlist.vue` 内部自己读写
 * `kline.watchlist.v1`，别的页面既看不到也改不了（用户反馈"K 线自选和选股日历怎么关联"）。
 *
 * 约定：
 *   - 存储仍是 `kline.watchlist.v1`（旧数据不动，避免用户自选丢失）+ `kline.watchlist.names.v1`
 *     存代码→名称，供日历这种"只有代码"的地方显示名字。
 *   - 模块级 `ref` 单例：任何组件 import 后看到的是同一份状态，无需 pinia。
 *   - 同 tab 内跨组件用 `quant:watchlist` 事件通知；跨 tab 用 `storage` 事件。
 */
import { computed, ref } from 'vue'

export const WATCHLIST_STORAGE_KEY = 'kline.watchlist.v1'
export const WATCHLIST_NAMES_KEY = 'kline.watchlist.names.v1'
const SYNC_EVENT = 'quant:watchlist'

function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return fallback
    const parsed = JSON.parse(raw)
    return parsed ?? fallback
  } catch {
    return fallback
  }
}

const codes = ref<string[]>(readJson<string[]>(WATCHLIST_STORAGE_KEY, []))
const names = ref<Record<string, string>>(readJson<Record<string, string>>(WATCHLIST_NAMES_KEY, {}))

let listening = false

function persist() {
  try {
    localStorage.setItem(WATCHLIST_STORAGE_KEY, JSON.stringify(codes.value))
    localStorage.setItem(WATCHLIST_NAMES_KEY, JSON.stringify(names.value))
  } catch {
    /* 隐私模式 / 配额满：内存态继续可用 */
  }
}

function broadcast() {
  try {
    window.dispatchEvent(new CustomEvent(SYNC_EVENT))
  } catch {
    /* SSR 忽略 */
  }
}

/** 只挂一次监听：同 tab 自定义事件 + 跨 tab storage 事件 */
function ensureListener() {
  if (listening || typeof window === 'undefined') return
  listening = true
  window.addEventListener(SYNC_EVENT, () => {
    codes.value = readJson<string[]>(WATCHLIST_STORAGE_KEY, [])
    names.value = readJson<Record<string, string>>(WATCHLIST_NAMES_KEY, {})
  })
  window.addEventListener('storage', (e) => {
    if (!e.key || ![WATCHLIST_STORAGE_KEY, WATCHLIST_NAMES_KEY].includes(e.key)) return
    codes.value = readJson<string[]>(WATCHLIST_STORAGE_KEY, [])
    names.value = readJson<Record<string, string>>(WATCHLIST_NAMES_KEY, {})
  })
}

/** A 股代码归一：`600519` → `600519.SH`，已经是 `600519.SH` 的原样返回。 */
export function normalizeCode(input: string): string {
  const raw = String(input || '').trim().toUpperCase()
  if (!raw) return ''
  if (raw.includes('.')) return raw
  if (!/^\d{6}$/.test(raw)) return raw
  if (raw.startsWith('6') || raw.startsWith('9')) return `${raw}.SH`
  if (raw.startsWith('4') || raw.startsWith('8')) return `${raw}.BJ`
  return `${raw}.SZ`
}

export function useWatchlist() {
  ensureListener()

  const set = computed(() => new Set(codes.value))
  const size = computed(() => codes.value.length)
  const rows = computed(() => codes.value.map((code) => ({ code, name: names.value[code] || '' })))

  function has(code: string): boolean {
    return set.value.has(normalizeCode(code))
  }

  /** 返回操作后的状态：true = 现在在自选里 */
  function toggle(code: string, name = ''): boolean {
    const c = normalizeCode(code)
    if (!c) return false
    if (names.value[c] !== undefined && name) {
      /* 已有名字时用新名字覆盖（搜索结果通常带名称） */
    }
    if (set.value.has(c)) {
      codes.value = codes.value.filter((x) => x !== c)
      persist()
      broadcast()
      return false
    }
    codes.value = [...codes.value, c]
    if (name) names.value = { ...names.value, [c]: name }
    persist()
    broadcast()
    return true
  }

  function add(code: string, name = '') {
    if (!has(code)) toggle(code, name)
    else if (name) rememberName(code, name)
  }

  function remove(code: string) {
    const c = normalizeCode(code)
    if (set.value.has(c)) toggle(c)
  }

  function clear() {
    codes.value = []
    persist()
    broadcast()
  }

  function rememberName(code: string, name: string) {
    const c = normalizeCode(code)
    if (!c || !name) return
    names.value = { ...names.value, [c]: name }
    persist()
  }

  /** 用当前页面拿到的名单批量补名称（日历/行业列表里顺手回填缓存） */
  function rememberMany(items: { code: string; name?: string }[]) {
    let dirty = false
    const next = { ...names.value }
    for (const item of items) {
      const c = normalizeCode(item.code)
      if (c && item.name && next[c] !== item.name) {
        next[c] = item.name
        dirty = true
      }
    }
    if (dirty) {
      names.value = next
      persist()
    }
  }

  return { codes, names, rows, size, set, has, toggle, add, remove, clear, rememberName, rememberMany }
}

export function nameOf(code: string): string {
  return names.value[normalizeCode(code)] || ''
}
