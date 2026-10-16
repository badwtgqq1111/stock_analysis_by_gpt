/** Strategy groups come from the pool, not a hard-coded subset of selection sources. */
import type { StrategyListRefresh } from '@/types/api'

export interface GroupVerdict {
  /** 正期望 / 负期望 / 样本不足 …（与策略池详情同一套判定） */
  label: string
  tone: 'ok' | 'warn' | 'bad' | 'dim'
  detail: string
  total: number | null
  excess: number | null
  t: number | null
}

export interface WatchlistGroup {
  key: string
  label: string
  note: string
  source: string
  stage: string
  date: string | null
  rows: { code: string; name: string }[]
  loading: boolean
  error: string
  verdict?: GroupVerdict
  /** 这条策略今天的名单刷新结果（每日名单刷新契约；不再是"读到旧文件也不知道"） */
  refresh?: StrategyListRefresh | null
}

export function strategyGroups(
  entries: {
    id: string; name: string; selection_source?: string; stage: string
    stage_label?: string; summary?: string; latest_date?: string | null
    refresh?: StrategyListRefresh | null
  }[],
  verdictOf?: (entry: { id: string }) => GroupVerdict | undefined,
): WatchlistGroup[] {
  return entries.filter((s) => Boolean(s.selection_source) && s.stage !== 'retired').map((s) => ({
    key: s.id, label: s.name, source: s.selection_source!, stage: s.stage_label || s.stage,
    note: s.summary || s.name, date: s.latest_date || null, rows: [], loading: false, error: '',
    verdict: verdictOf ? verdictOf(s) : undefined,
    refresh: s.refresh ?? null,
  }))
}

/** 名单刷新状态 → 侧栏分组上的小标记（落后/失败要显眼，当日无信号只是灰点）。 */
export function refreshMark(refresh?: StrategyListRefresh | null): { icon: string; tone: 'ok' | 'warn' | 'bad' | 'dim'; detail: string } | null {
  if (!refresh) return null
  const detail = `名单刷新 ${refresh.status}：${refresh.date_after || '—'}（${refresh.reason || ''}）`
  if (refresh.status === 'stale') return { icon: '⚠️', tone: 'warn', detail }
  if (refresh.status === 'timeout') return { icon: '⏱️', tone: 'warn', detail }
  if (refresh.status === 'failed') return { icon: '❌', tone: 'bad', detail }
  if (refresh.status === 'no_signal') return { icon: '➖', tone: 'dim', detail }
  if (refresh.status === 'skipped') return { icon: '⏭️', tone: 'dim', detail }
  return { icon: '✅', tone: 'ok', detail }
}

/** 侧栏分组的排序键：默认（策略池顺序）/ 累计 / 超额 / 日度 t / 名称。 */
export type GroupSortKey = 'default' | 'total' | 'excess' | 't' | 'name'

export function sortGroups(groups: WatchlistGroup[], key: GroupSortKey): WatchlistGroup[] {
  if (key === 'default') return groups
  const copy = [...groups]
  if (key === 'name') return copy.sort((a, b) => a.label.localeCompare(b.label, 'zh'))
  return copy.sort((a, b) => {
    const va = a.verdict?.[key] ?? null
    const vb = b.verdict?.[key] ?? null
    // 没有曲线的排最后（不要让"缺数据"混进正期望里）
    if (va === null && vb === null) return a.label.localeCompare(b.label, 'zh')
    if (va === null) return 1
    if (vb === null) return -1
    return (vb as number) - (va as number)
  })
}

export interface WatchlistRow { code: string; name: string; tag: string; local: boolean; source?: string;
  /** 命中多条策略时的计数与首个策略名（标签显示成「策略A +N」） */
  extra?: number; baseTag?: string }

export function groupRows(groups: WatchlistGroup[], key: string): WatchlistRow[] {
  const seen = new Map<string, WatchlistRow>()
  for (const g of groups.filter((g) => key === 'all' ? g.key !== 'paper' : g.key === key)) {
    for (const item of g.rows) {
      const existing = seen.get(item.code)
      if (existing) {
        // 命中多条策略时不再把每个策略名都拼进标签（那会把「名称」这一列挤到 0 宽，
        // 实测 2026-10-13：83 行里 42 行名称看不见）；改成"首个策略 +N"，完整的看 title。
        const extra = Number(existing.extra || 0) + 1
        existing.extra = extra
        existing.tag = `${existing.baseTag || existing.tag} +${extra}`
        if (!existing.name) existing.name = item.name
      } else seen.set(item.code, { ...item, tag: g.label, baseTag: g.label, local: false, source: g.source } as WatchlistRow)
    }
  }
  return [...seen.values()]
}
