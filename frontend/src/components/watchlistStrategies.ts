/** Strategy groups come from the pool, not a hard-coded subset of selection sources. */
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
}

export function strategyGroups(
  entries: {
    id: string; name: string; selection_source?: string; stage: string
    stage_label?: string; summary?: string; latest_date?: string | null
  }[],
  verdictOf?: (entry: { id: string }) => GroupVerdict | undefined,
): WatchlistGroup[] {
  return entries.filter((s) => Boolean(s.selection_source) && s.stage !== 'retired').map((s) => ({
    key: s.id, label: s.name, source: s.selection_source!, stage: s.stage_label || s.stage,
    note: s.summary || s.name, date: s.latest_date || null, rows: [], loading: false, error: '',
    verdict: verdictOf ? verdictOf(s) : undefined,
  }))
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

export interface WatchlistRow { code: string; name: string; tag: string; local: boolean }

export function groupRows(groups: WatchlistGroup[], key: string): WatchlistRow[] {
  const seen = new Map<string, WatchlistRow>()
  for (const g of groups.filter((g) => key === 'all' ? g.key !== 'paper' : g.key === key)) {
    for (const item of g.rows) {
      const existing = seen.get(item.code)
      if (existing) {
        existing.tag += ` · ${g.label}`
        if (!existing.name) existing.name = item.name
      } else seen.set(item.code, { ...item, tag: g.label, local: false })
    }
  }
  return [...seen.values()]
}
