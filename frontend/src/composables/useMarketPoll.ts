import { onMounted, onUnmounted } from 'vue'
/** Re-evaluate CN wall clock each time, including auction->open transition. */
export function useMarketPoll(reload: () => void | Promise<void>, enabled: () => boolean) {
  let busy = false
  async function tick() {
    if (!enabled() || document.hidden || busy) return
    const now = new Date()
    const cn = now.toLocaleTimeString('en-GB', { timeZone: 'Asia/Shanghai', hour12: false })
    const weekday = now.toLocaleDateString('en-US', { timeZone: 'Asia/Shanghai', weekday: 'short' })
    if (weekday === 'Sat' || weekday === 'Sun' || cn < '09:30:00' || cn > '15:01:00' || (cn > '11:31:00' && cn < '13:00:00')) return
    busy = true
    try { await reload() } finally { busy = false }
  }
  let timer: ReturnType<typeof setInterval> | undefined
  onMounted(() => { timer = setInterval(() => { void tick() }, 15000) })
  onUnmounted(() => { if (timer) clearInterval(timer) })
}

export const cnToday = () => new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date())
