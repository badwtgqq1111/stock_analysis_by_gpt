/** Strategy groups come from the pool, not a hard-coded subset of selection sources. */
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
}

export function strategyGroups(entries: {
  id: string; name: string; selection_source?: string; stage: string
  stage_label?: string; summary?: string; latest_date?: string | null
}[]): WatchlistGroup[] {
  return entries.filter((s) => Boolean(s.selection_source) && s.stage !== 'retired').map((s) => ({
    key: s.id, label: s.name, source: s.selection_source!, stage: s.stage_label || s.stage,
    note: s.summary || s.name, date: s.latest_date || null, rows: [], loading: false, error: '',
  }))
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
