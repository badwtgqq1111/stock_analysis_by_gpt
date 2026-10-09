/**
 * 系统探活 —— 刻意**不依赖 axios**。
 *
 * App.vue 属于入口 chunk；如果它 import 了 `api/client.ts`，整个 axios（~44 kB）
 * 就会被拽进首屏 bundle（实测 index chunk 96 kB → 148 kB）。
 * 所以这个模块只做一件事、只用原生 fetch。
 */
export interface HealthResponse {
  status: string
  frontend?: { dist: string | null; refs: string[]; missing: string[]; ok: boolean }
}

export async function getHealth(timeoutMs = 8000): Promise<HealthResponse> {
  const ac = new AbortController()
  const timer = setTimeout(() => ac.abort(), timeoutMs)
  try {
    // 注意：/health 不在 /api 前缀下
    const resp = await fetch('/health', { signal: ac.signal })
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    return (await resp.json()) as HealthResponse
  } finally {
    clearTimeout(timer)
  }
}
