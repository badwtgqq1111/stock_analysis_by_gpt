import { computed, onUnmounted, ref, watch } from 'vue'
import { getOhlcv } from '@/api/client'
import type { OhlcvDataPoint, OhlcvResponse } from '@/types/api'

export function useKlineData() {
  const stockCode = ref<string | null>(null)
  const days = ref(365)
  const showSignals = ref(true)
  const showChips = ref(true)
  const archived = ref<OhlcvResponse | null>(null)
  const liveBar = ref<{ candle: OhlcvDataPoint; asof: string; available: boolean } | null>(null)
  const clock = ref(Date.now())
  const isLoading = ref(false)
  const error = ref<string | null>(null)
  let timer: ReturnType<typeof setTimeout> | null = null
  let sequence = 0
  let liveSequence = 0
  let pending = ''
  const data = computed<OhlcvResponse | null>(() => {
    if (!archived.value) return null
    const bar = liveBar.value
    const cn = new Date(clock.value).toLocaleTimeString('en-GB', { timeZone: 'Asia/Shanghai', hour12: false })
    const today = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(new Date(clock.value))
    const active = (cn >= '09:30:00' && cn <= '11:30:00') || (cn >= '13:00:00' && cn <= '15:00:00')
    if (!bar?.available || bar.candle.time !== today || (active && clock.value - Date.parse(bar.asof.replace(' ', 'T') + '+08:00') > 180000)) return archived.value
    const rows = archived.value.data.filter((r) => r.time < bar.candle.time)
    const current = { ...bar.candle }
    const withCurrent = [...rows, current]
    for (const n of [5, 10, 20, 60] as const) {
      current[`ma${n}`] = withCurrent.length >= n ? withCurrent.slice(-n).reduce((a, r) => a + r.close, 0) / n : null
    }
    const prev = rows[rows.length - 1]?.close
    current.amplitude = prev ? (current.high - current.low) / prev * 100 : null
    return { ...archived.value, data: withCurrent, realtime: { available: true, asof: bar.asof, provisional: true },
      latest: { close: current.close, change_pct: current.change_pct || 0, total_bars: withCurrent.length } }
  })
  async function fetchLive() {
    const code = stockCode.value
    if (!code || !/^\d{6}\.(SH|SZ|BJ)$/.test(code) || document.hidden || pending === code) return
    const request = ++liveSequence
    pending = code
    try {
      const response = await fetch(`/api/intraday/daily/${encodeURIComponent(code)}`)
      if (!response.ok) return
      const bar = await response.json()
      if (request === liveSequence && code === stockCode.value) liveBar.value = bar.available ? bar : null
    } catch { /* 保留最近有效蜡烛；时间戳可见 */ }
    finally { if (pending === code) pending = '' }
  }
  async function fetchData() {
    const request = ++sequence
    const code = stockCode.value
    if (!code) { archived.value = null; return }
    isLoading.value = true; error.value = null
    try {
      const result = await getOhlcv(code, days.value, showSignals.value, showChips.value)
      if (request !== sequence) return
      archived.value = result
      void fetchLive()
    } catch (e: any) {
      if (request === sequence) { error.value = e.message || '取数失败'; archived.value = null }
    } finally { if (request === sequence) isLoading.value = false }
  }
  watch(stockCode, () => { ++sequence; ++liveSequence; archived.value = null; liveBar.value = null })
  watch([stockCode, days, showSignals, showChips], () => {
    if (timer) clearTimeout(timer)
    timer = setTimeout(fetchData, 200)
  })
  const poll = setInterval(() => { clock.value = Date.now(); void fetchLive() }, 10000)
  onUnmounted(() => { ++sequence; ++liveSequence; clearInterval(poll); if (timer) clearTimeout(timer) })
  return { data, isLoading, error, stockCode, days, showSignals, showChips, refresh: fetchData }
}
