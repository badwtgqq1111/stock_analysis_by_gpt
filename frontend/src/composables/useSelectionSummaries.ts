/** 入选摘要缓存：自选侧栏悬停看"哪条策略/rank/权重/收益"用的批量数据。
 *
 *  为什么不做成每行一个 `/api/stocks/{code}/bundle`：自选 100+ 行 = 100 个请求，
 *  每个还要读估值/财务/龙虎榜一堆无关块（那种请求实测 200~400ms）。这里一次拉一批
 *  （上限 400 只/次，分 200 一批），并且**跨页面/跨换票复用**（模块级缓存，台账一天
 *  只更新一次，重复拉没有意义）；`as_of` 记录台账最后的决策日，卡片据此说明口径。
 */
import { computed, ref, watch, type Ref } from 'vue'
import { getSelectionByCode, type LedgerCodeSummary } from '@/api/client'

const CACHE = new Map<string, LedgerCodeSummary>()      // 有摘要（被选中过）
const PROBED = new Set<string>()                          // 查过（含"从没入选过"）
const AS_OF = ref('')
const LOADING = ref(false)
const ERROR = ref('')
/** 缓存版本号：新增/清空时 +1，让 computed 重新求值（Map 本身不是响应式的） */
const VERSION = ref(0)
const BATCH = 200

async function loadCodes(codes: string[]): Promise<void> {
  const want = [...new Set(codes.map((c) => String(c || '')).filter(Boolean))]
  const missing = want.filter((c) => !PROBED.has(c))
  if (!missing.length) return
  for (const c of missing) PROBED.add(c)                   // 先占位，避免并发重复拉
  LOADING.value = true
  try {
    for (let i = 0; i < missing.length; i += BATCH) {
      const data = await getSelectionByCode(missing.slice(i, i + BATCH), 3)
      if (data.as_of) AS_OF.value = data.as_of
      for (const [code, summary] of Object.entries(data.codes || {})) CACHE.set(code, summary)
      VERSION.value += 1
    }
    ERROR.value = ''
  } catch (e) {
    ERROR.value = String(e)
    for (const c of missing) if (!CACHE.has(c)) PROBED.delete(c)   // 失败的下次还能重试
  } finally {
    LOADING.value = false
  }
}

/** 当前悬停那一行的摘要（没悬停 / 没入选过 ⇒ null）。`hit` 用来区分"还没查到"与"确实没入选"。 */
export function useSelectionSummaries(
  codes: Ref<string[]>, hovered: Ref<string | null>,
) {
  const hoverSummary = computed<LedgerCodeSummary | null>(() => {
    void VERSION.value                                     // 依赖版本号，数据到了会重算
    const code = String(hovered.value || '')
    return code ? CACHE.get(code) || null : null
  })
  const hoverProbed = computed(() => {
    void VERSION.value
    return Boolean(hovered.value) && PROBED.has(String(hovered.value))
  })
  // 可见代码一变就补拉（分组的增删/切换分组都会触发）。
  // ⚠️ 默认分组 'all' 的策略名单是**陆续到达**的 ⇒ 不防抖就会按"增量批次"发十几个请求
  //    （实测 13 个：远好于逐行 154 个，但没必要）。300ms 窗口把同一轮渲染合起来。
  let timer: ReturnType<typeof setTimeout> | null = null
  watch(() => [...new Set(codes.value)].sort().join(','), () => {
    if (timer) clearTimeout(timer)
    timer = setTimeout(() => { void loadCodes(codes.value) }, 300)
  }, { immediate: true })
  return { hoverSummary, hoverProbed, asOf: AS_OF, loading: LOADING, error: ERROR }
}
