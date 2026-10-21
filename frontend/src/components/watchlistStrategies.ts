/** Strategy groups come from the pool, not a hard-coded subset of selection sources. */
import type { StrategyListRefresh, StrategyPoolEntry } from '@/types/api'
import { annualRankOrder } from './strategyRanking'

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
  /** 选股台账里的来源名；历史回顾用它与 /api/calendar/day 对齐 */
  ledgerSources?: string[]
  rows: { code: string; name: string; close?: number | null; pct_chg?: number | null;
    amount?: number | null; turnover?: number | null }[]
  loading: boolean
  error: string
  verdict?: GroupVerdict
  annual_rank?: StrategyPoolEntry['annual_rank']
  annual_rank_reason?: string
  /** 这条策略今天的名单刷新结果（每日名单刷新契约；不再是"读到旧文件也不知道"） */
  refresh?: StrategyListRefresh | null
}

export function strategyGroups(
  entries: {
    id: string; name: string; selection_source?: string; stage: string
    ledger_sources?: string[]
    stage_label?: string; summary?: string; latest_date?: string | null
    refresh?: StrategyListRefresh | null
    annual_rank?: StrategyPoolEntry['annual_rank']; annual_rank_reason?: string
  }[],
  verdictOf?: (entry: { id: string }) => GroupVerdict | undefined,
): WatchlistGroup[] {
  return entries.filter((s) => Boolean(s.selection_source) && s.stage !== 'retired').map((s) => ({
    key: s.id, label: s.name, source: s.selection_source!, stage: s.stage_label || s.stage,
    note: s.summary || s.name, date: s.latest_date || null,
    ledgerSources: s.ledger_sources || [], rows: [], loading: false, error: '',
    verdict: verdictOf ? verdictOf(s) : undefined,
    refresh: s.refresh ?? null, annual_rank: s.annual_rank, annual_rank_reason: s.annual_rank_reason,
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

/** 侧栏默认按年化主榜，累计/超额等仍可手动选择。 */
export type GroupSortKey = 'annual' | 'default' | 'total' | 'excess' | 't' | 'name'

/** K线实盘固定在策略分组最前，不受年化/累计/超额/t/名称/登记顺序影响。 */
export function sortGroups(groups: WatchlistGroup[], key: GroupSortKey): WatchlistGroup[] {
  const sorted = sortUnpinnedGroups(groups, key)
  return [...sorted.filter((g) => g.key === 'live_holdings'),
          ...sorted.filter((g) => g.key !== 'live_holdings')]
}

function sortUnpinnedGroups(groups: WatchlistGroup[], key: GroupSortKey): WatchlistGroup[] {
  if (key === 'default') return groups
  if (key === 'annual') return [...groups].sort((a, b) => {
    // 模拟盘不是选股策略，固定在策略分组之后。
    if ((a.key === 'paper') !== (b.key === 'paper')) return a.key === 'paper' ? 1 : -1
    return annualRankOrder(a.annual_rank?.rank, b.annual_rank?.rank, a.key, b.key)
  })
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
  extra?: number; baseTag?: string;
  /** P2.75 候选池综合排名/分数（没有排序产物的票为 null） */
  rank?: number | null; score?: number | null;
  /** 实盘持仓：指定置顶，但保留排名/分数 */
  isLive?: boolean;
  /** 当前排序模式下的本地排名/分数（资金流/个股多空会重排） */
  displayRank?: number | null; displayScore?: number | null;
  flowScore?: number | null; flow5?: number | null; flow20?: number | null;
  longshort?: number | null; comboScore?: number | null;
  /** 历史回顾：决策日收盘 / 当日涨跌 / 成交额，与回看日期对齐 */
  histClose?: number | null; histPct?: number | null; histAmount?: number | null }

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
      } else seen.set(item.code, {
        ...item, tag: g.label, baseTag: g.label, local: false, source: g.source,
        histClose: item.close ?? null, histPct: item.pct_chg ?? null, histAmount: item.amount ?? null,
      } as WatchlistRow)
    }
  }
  return [...seen.values()]
}
