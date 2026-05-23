import { ref, computed } from 'vue'
import { getStocks } from '@/api/client'
import type { StockOption } from '@/types/api'

export function useStockList() {
  const stocks = ref<StockOption[]>([])
  const isLoading = ref(false)
  const query = ref('')

  async function load() {
    isLoading.value = true
    try {
      const res = await getStocks()
      stocks.value = res.stocks
    } finally {
      isLoading.value = false
    }
  }

  const filtered = computed(() => {
    const q = query.value.toLowerCase().trim()
    if (!q) return stocks.value
    return stocks.value.filter(
      (s) => s.code.includes(q) || s.name.toLowerCase().includes(q),
    )
  })

  return { stocks, isLoading, query, filtered, load }
}
