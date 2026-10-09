import { getCurrentInstance, onMounted, ref, shallowRef, watch, type Ref, type WatchSource } from 'vue'
import { ApiError } from '@/api/client'

export interface AsyncState<T> {
  data: Ref<T | null>
  error: Ref<string>
  loading: Ref<boolean>
  /** 最近一次成功的取数时间（本地时钟） */
  loadedAt: Ref<Date | null>
  reload: () => Promise<void>
}

/**
 * 极简取数封装：loading / error / data 三态 + 重试。
 *
 * 为什么需要它：原先每个页面都手写 `const data = ref(null); onMounted(async () => { data.value = await getX() })`
 * —— 没有 catch（后端一挂就是控制台未捕获 rejection、页面永远停在"加载中"），
 * 也没有"重新加载"的能力。这里把三态和错误信息统一掉。
 *
 * @param fetcher 取数函数
 * @param watchOn 可选：依赖变化时自动重取（例如切换月份/来源）
 * @param options.immediate 挂载即取（默认 true）。
 *
 * 踩过的坑：第一版**没有** immediate，只返回 reload，结果页面 setup 里忘了手动调，
 * 组件正常渲染但一个请求都不发（表现为"没有 console 错误、数据永远空"）。
 * 既然这个 composable 的职责就是取数，默认自动取才是符合直觉的行为。
 */
export function useAsync<T>(
  fetcher: () => Promise<T>,
  watchOn?: WatchSource | WatchSource[],
  options: { immediate?: boolean } = {},
): AsyncState<T> {
  const { immediate = true } = options
  const data = shallowRef<T | null>(null)
  const error = ref('')
  const loading = ref(false)
  const loadedAt = ref<Date | null>(null)

  async function reload() {
    loading.value = true
    error.value = ''
    try {
      data.value = await fetcher()
      loadedAt.value = new Date()
    } catch (e) {
      error.value = e instanceof ApiError ? `${e.message}${e.status ? `（status=${e.status}）` : ''}` : String(e)
    } finally {
      loading.value = false
    }
  }

  if (watchOn) {
    watch(watchOn as any, () => { void reload() })
  }
  if (immediate && getCurrentInstance()) {
    onMounted(() => { void reload() })
  }

  return { data, error, loading, loadedAt, reload }
}
