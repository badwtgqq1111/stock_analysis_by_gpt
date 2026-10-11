/**
 * 策略池的"方向判定"→ **来源** 映射（选股结果页来源 tab、K 线自选侧栏分组都用它）。
 *
 * 为什么单独一个 composable：同一个判定必须只算一次、只在一处维护。判定规则在
 * `components/strategyDetail.ts`（样本 ≥30 才判方向、|日度 t| ≥1.5 视为有证据…），
 * 这里只负责"把策略池快照按 selection_source 摊平成一张表"，并做一次模块级缓存
 * （选股结果页与 K 线页会各问一次，但共享同一份结果）。
 */
import { ref } from 'vue'
import { getStrategyPool } from '@/api/client'
import { strategyDetail } from '@/components/strategyDetail'
import type { StrategyPoolResponse } from '@/types/api'

export interface SourceVerdict {
  id: string
  label: string
  tone: 'ok' | 'warn' | 'bad' | 'dim'
  detail: string
  stage: string
  total: number | null
  excess: number | null
  t: number | null
  observations: number
}

const verdicts = ref<Record<string, SourceVerdict>>({})
const loadedAt = ref<Date | null>(null)
let inflight: Promise<void> | null = null

function build(pool: StrategyPoolResponse): Record<string, SourceVerdict> {
  const out: Record<string, SourceVerdict> = {}
  for (const item of pool.strategies || []) {
    const source = item.selection_source
    if (!source || item.stage === 'retired') continue
    const detail = strategyDetail(item)
    out[source] = {
      id: item.id,
      label: detail.verdict.headline.label,
      tone: detail.verdict.headline.tone,
      detail: detail.verdict.headline.detail,
      stage: item.stage,
      total: detail.curve.total,
      excess: detail.curve.excess,
      t: detail.curve.t,
      observations: detail.curve.observations,
    }
  }
  return out
}

export function usePoolVerdicts() {
  async function load(force = false): Promise<void> {
    if (!force && loadedAt.value && Date.now() - loadedAt.value.getTime() < 60_000) return
    if (inflight) return inflight
    inflight = (async () => {
      try {
        const pool = await getStrategyPool(false)
        verdicts.value = build(pool)
        loadedAt.value = new Date()
      } catch {
        /* 拿不到策略池就不显示徽章，不影响名单本身 */
      } finally {
        inflight = null
      }
    })()
    return inflight
  }
  return { verdicts, load }
}
