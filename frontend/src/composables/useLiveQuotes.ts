/**
 * 实时快照：**交易时段批量轮询 + 状态驱动渲染**（2026-10-12）。
 *
 * 为什么这么设计（而不是"定时刷新 UI"）：
 *   1. **一次请求多只**：走 `/api/intraday/quotes?codes=a,b,c`（批量快照），不是每只一个请求；
 *   2. **只在交易时段轮询**：时段判断用后端返回的 `session.phase`（`/api/rankings/overview` 也有），
 *      前端不自己算节假日/夏令时；非交易时段**自动停**（省配额、省流量）；
 *   3. **页面隐藏时停**：`document.hidden` 时不再请求，回来立刻补一次；
 *   4. **降级可见**：返回体里带 `source/source_label`，页面照实标「实时 / 延迟 / 归档」，
 *      失败时保留上一次成功数据并记录时间，不静默清空。
 */
import { onMounted, onUnmounted, ref, watch, type Ref } from 'vue'
import { getIntradayQuotes } from '@/api/client'
import type { IntradayQuote, SessionPhase } from '@/types/api'

export interface LiveQuotesState {
  quotes: Ref<Record<string, IntradayQuote>>
  session: Ref<SessionPhase | null>
  updatedAt: Ref<Date | null>
  error: Ref<string>
  loading: Ref<boolean>
  /** 当前是否在轮询（交易时段 + 页面可见 + 有自选） */
  polling: Ref<boolean>
  reload: () => Promise<void>
}

/** `/api/intraday/quotes` 的硬上限（后端 `[:40]`）—— 每批不得超过它，否则末尾的票静默丢价。 */
const MAX_CODES_PER_REQUEST = 40

export function useLiveQuotes(codes: Ref<string[]>, options: { intervalMs?: number } = {}): LiveQuotesState {
  const intervalMs = options.intervalMs ?? 4000
  const quotes = ref<Record<string, IntradayQuote>>({})
  const session = ref<SessionPhase | null>(null)
  const updatedAt = ref<Date | null>(null)
  const error = ref('')
  const loading = ref(false)
  const polling = ref(false)
  let timer: ReturnType<typeof setInterval> | null = null

  async function reload() {
    const wanted = (codes.value || []).filter(Boolean)
    if (!wanted.length) {
      polling.value = false
      return
    }
    loading.value = true
    try {
      // 事故（2026-10-15）：这里原先是"一次发完、`.slice(0, 60)`"，可 `/api/intraday/quotes`
      // 只收前 40 个代码（`[:40]`）⇒ 分组列表一变长，**末尾那批行就永远没有实时价**
      // （用户看到的就是 K 线页「动态诊断候选」整组空白、只有前两组有价）。
      // 现在按 MAX_CODES_PER_REQUEST 分批**并行**发，再把结果合并；任何一批失败都不影响其它批。
      const batches: string[][] = []
      for (let i = 0; i < wanted.length; i += MAX_CODES_PER_REQUEST) {
        batches.push(wanted.slice(i, i + MAX_CODES_PER_REQUEST))
      }
      const settled = await Promise.allSettled(batches.map((batch) => getIntradayQuotes(batch)))
      const ok = settled.filter((r): r is PromiseFulfilledResult<Awaited<ReturnType<typeof getIntradayQuotes>>> =>
        r.status === 'fulfilled')
      if (!ok.length) {
        const first = settled[0] as PromiseRejectedResult
        throw first?.reason ?? new Error('quotes failed')
      }
      const next: Record<string, IntradayQuote> = { ...quotes.value }
      for (const result of ok) {
        for (const quote of result.value.quotes || []) next[quote.stock_code] = quote
        if (result.value.session) session.value = result.value.session
      }
      quotes.value = next
      updatedAt.value = new Date()
      error.value = ok.length === settled.length ? '' : `部分批次失败（${ok.length}/${settled.length}）`
    } catch (err) {
      error.value = String(err)          // 保留上一次成功数据，只标错，不清空
    } finally {
      loading.value = false
    }
  }

  function stop() {
    if (timer) clearInterval(timer)
    timer = null
    polling.value = false
  }

  function start() {
    stop()
    if (document.hidden || !(codes.value || []).length) return
    // 只在"实时源应当有数据"的时段轮询；收盘/午休/盘前不轮询（快照仍可手动刷新一次）
    const phase = session.value?.phase
    if (phase && phase !== 'am' && phase !== 'pm' && phase !== 'auction') return
    timer = setInterval(() => { if (!document.hidden) void reload() }, intervalMs)
    polling.value = true
  }

  function onVisibility() {
    if (document.hidden) stop()
    else { void reload().then(start) }
  }

  onMounted(async () => {
    await reload()
    start()
    document.addEventListener('visibilitychange', onVisibility)
  })
  onUnmounted(() => {
    stop()
    document.removeEventListener('visibilitychange', onVisibility)
  })
  watch(codes, () => { void reload().then(start) }, { deep: true })

  return { quotes, session, updatedAt, error, loading, polling, reload }
}

/** 页面标签：实时 / 延迟 / 归档（照实标注，不静默降级） */
export function quoteLabel(quote: IntradayQuote | undefined): string {
  if (!quote || !quote.available || quote.price == null) return '—'
  // 优先用后端给的 `source_label`（它对源有完整说明）；拿不到再按 source 关键词兜底。
  const label = String((quote as { source_label?: string }).source_label || '')
  if (label) return label
  const source = String(quote.source || '').toLowerCase()
  // 实测的 source 取值是 `tx_quote`（腾讯批量快照）—— 只匹配 'tencent' 会把它判成「延迟」，
  // 那是在**误标**：这批价本身就是实时价。所以按"已知实时源前缀"判定，不用宽泛的子串匹配
  // （`tx` 这种两个字母放进子串匹配会把无关来源也判成实时）。
  const livePrefixes = ['tx', 'tencent', 'em', 'eastmoney', 'sina', 'live']
  if (livePrefixes.some((p) => source.startsWith(p) || source.includes(`${p}_`) || source.includes(`_${p}`))) {
    return '实时'
  }
  if (source.includes('store') || source.includes('archive')) return '归档'
  return source ? '延迟' : '—'
}
