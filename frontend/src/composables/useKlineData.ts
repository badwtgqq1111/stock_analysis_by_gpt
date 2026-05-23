import { ref, watch } from 'vue'
import { getOhlcv } from '@/api/client'
import type { OhlcvResponse } from '@/types/api'

export function useKlineData() {
  const stockCode = ref<string | null>(null)
  const days = ref<number>(365)
  const showSignals = ref<boolean>(true)
  const showChips = ref<boolean>(true)

  const data = ref<OhlcvResponse | null>(null)
  const isLoading = ref(false)
  const error = ref<string | null>(null)

  let timer: ReturnType<typeof setTimeout> | null = null

  async function fetch() {
    if (!stockCode.value) {
      data.value = null
      return
    }
    isLoading.value = true
    error.value = null
    try {
      data.value = await getOhlcv(
        stockCode.value,
        days.value,
        showSignals.value,
        showChips.value,
      )
    } catch (e: any) {
      error.value = e.message || 'Failed to fetch'
      data.value = null
    } finally {
      isLoading.value = false
    }
  }

  function debouncedFetch() {
    if (timer) clearTimeout(timer)
    timer = setTimeout(fetch, 200)
  }

  watch([stockCode, days, showSignals, showChips], debouncedFetch, { immediate: false })

  function refresh() {
    fetch()
  }

  return { data, isLoading, error, stockCode, days, showSignals, showChips, refresh }
}
