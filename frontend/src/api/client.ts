import axios, { AxiosError } from 'axios'
import type {
  AttributionDetail, AttributionIndex, ThsExportList, PaperStatus, PerformanceDetail, PerformanceSummary, StockListResponse, OhlcvResponse, SelectionResponse, ShapResponse,
  FactorICResponse, PortfolioResponse, ImportanceResponse, AutomationStatus,
  ForwardValidation, ForwardSeries,
  RankingsOverview, RankingsChangeResponse, RankingsMoneyflowResponse,
  LimitUpLadder, LimitUpDetail, LimitUpLive, IntradayResponse, IntradayQuote, IntradayCoverage, SessionPhase,
  IndustryOverview, IndustryHeatResponse, IndustryStocksResponse,
  IndustryHeatmapResponse,
  LivePerformance, LiveRealtime, AgentActionsResponse, AgentActionRun, MiningContext,
  ResearchSourcesResponse,
} from '@/types/api'

/** 统一的接口错误：页面只认 `err.message` 与 `err.status`，不用各自解析 axios 的结构。 */
export class ApiError extends Error {
  status: number | null
  url: string
  /** 后端 fastapi HTTPException 的 detail（有的话） */
  detail: string | null
  constructor(message: string, opts: { status?: number | null; url?: string; detail?: string | null } = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = opts.status ?? null
    this.url = opts.url ?? ''
    this.detail = opts.detail ?? null
  }
}

const http = axios.create({ baseURL: '/api', timeout: 60000 })

// 统一把"超时 / 404 / 500 / 后端 detail"翻译成人话。
// 以前每个页面各写一遍 try/catch，写漏的地方就变成控制台里的未捕获 rejection。
http.interceptors.response.use(
  (resp) => resp,
  (err: AxiosError<any>) => {
    const url = String(err.config?.url ?? '')
    if (err.code === 'ECONNABORTED' || /timeout/i.test(err.message)) {
      return Promise.reject(new ApiError(`请求超时（${url}）`, { url }))
    }
    if (!err.response) {
      return Promise.reject(new ApiError(`后端无响应（${url}）—— 确认 uvicorn 还在跑`, { url }))
    }
    const { status, data } = err.response
    const detail = data && typeof data === 'object' ? (data as any).detail : null
    const text = detail
      ? typeof detail === 'string' ? detail : JSON.stringify(detail)
      : `HTTP ${status}`
    return Promise.reject(new ApiError(`接口失败：${text}`, { status, url, detail: text }))
  },
)

// Stocks
export async function getStocks(): Promise<StockListResponse> {
  const { data } = await http.get<StockListResponse>('/stocks')
  return data
}

export interface StockSearchHit {
  code: string; name: string; market: string; board: string
  industry: string; list_status: string; match: number; suffix: string
  flags?: string[]
}
export interface StockSearchOptions {
  limit?: number
  board?: string
  include_delisted?: boolean
  main_board_only?: boolean
  market?: string
}

/** 代码 / 名称 / 拼音首字母 模糊搜索（A 股为主；港股仍可用但不重点关注）。 */
export async function searchStocks(q: string, options: StockSearchOptions = {}): Promise<StockSearchHit[]> {
  const { data } = await http.get<{ query: string; count: number; results: StockSearchHit[] }>(
    '/stocks/search', { params: { q, limit: options.limit ?? 20, ...options } },
  )
  return data.results
}

// OHLCV
export async function getOhlcv(
  code: string, days: number, signals: boolean, chips: boolean,
): Promise<OhlcvResponse> {
  const { data } = await http.get<OhlcvResponse>(`/stocks/${code}/ohlcv`, {
    params: { days, signals, chips },
  })
  return data
}

// Selection
/** 选股结果。`source` 留空 = 后端默认（CN 生产名单）。 */
export async function getSelection(source = ''): Promise<SelectionResponse> {
  const { data } = await http.get<SelectionResponse>('/selection', { params: { source } })
  return data
}

export async function getShap(code: string): Promise<ShapResponse> {
  const { data } = await http.get<ShapResponse>(`/selection/${code}/shap`)
  return data
}

// Factor IC
export async function getFactorIC(
  factorSet: string, horizon: number, topN: number = 10,
): Promise<FactorICResponse> {
  const { data } = await http.get<FactorICResponse>('/factor-ic', {
    params: { factor_set: factorSet, horizon, top_n: topN },
  })
  return data
}

// Importance
export async function getImportance(
  factorSet: string = '',
): Promise<ImportanceResponse> {
  const { data } = await http.get<ImportanceResponse>('/importance', {
    params: { factor_set: factorSet },
  })
  return data
}

// Portfolio
export async function getPortfolio(): Promise<PortfolioResponse> {
  const { data } = await http.get<PortfolioResponse>('/portfolio')
  return data
}

// ─── LLM 因子挖掘会话 ───
export interface MiningMessage { role: string; text: string }
export interface MiningFactor {
  name: string; ic_mean?: number; icir?: number; nw_t?: number
  pos_ratio?: number; max_corr_with_fields?: number
  fdr_pass?: boolean | string; expression?: string; reason?: string
}
export interface MiningSessionDetail {
  session_id: string; status: string; request: Record<string, any>
  messages: MiningMessage[]; factors: MiningFactor[]
  out_dir: string | null; error: string | null
  created_at?: string; updated_at?: string
}
export interface MiningSessionSummary {
  session_id: string; status: string; direction?: string
  n_messages: number; n_factors: number; out_dir: string | null
  created_at?: string; updated_at?: string
}
export interface MiningCreateResponse { session_id: string; status: string }

export async function createMiningSession(body: {
  direction: string; rounds: number; proposals: number; model: string; dry_run?: boolean
}): Promise<MiningCreateResponse> {
  const { data } = await http.post<MiningCreateResponse>('/factor-mining/sessions', body)
  return data
}

export async function getMiningSession(id: string): Promise<MiningSessionDetail> {
  const { data } = await http.get<MiningSessionDetail>(`/factor-mining/sessions/${id}`)
  return data
}

export async function listMiningSessions(): Promise<MiningSessionSummary[]> {
  const { data } = await http.get<MiningSessionSummary[]>('/factor-mining/sessions')
  return data
}

// ─── CN 数据流水线 ───
export interface StageArtifact {
  path: string; exists: boolean; kind?: string; bytes?: number
  mtime?: string; rows?: number; files?: number
}
export interface StageCatalogItem {
  stage: string; configured: boolean; overwrites_results: boolean
  last_status?: string | null; last_detail?: string | null; artifacts: StageArtifact[]
}
export interface PipelineOverview {
  config: string; stages_configured: string[]; stage_catalog: StageCatalogItem[]
  latest_report: {
    file?: string; started_at?: string; result?: string; seconds?: number; config?: string
    stages: { name: string; status: string; detail?: string | null }[]
  }
  recent_reports: {
    file: string; started_at?: string; finished_at?: string; result?: string
    seconds?: number | null; config?: string
    stage_status: Record<string, string>; failed: string[]
  }[]
  runs: { run_id: string; stage: string; status: string; returncode: number | null
          started_at?: string; finished_at?: string; log_lines: number }[]
}
export interface SelectionPayload {
  exists: boolean; path: string; rows: Record<string, any>[]; columns: string[]
  count?: number; mtime?: string
}
export interface PipelineSelection {
  source_dir: string
  arm?: { payload: SelectionPayload; meta: Record<string, any> }
  // 概念/板块手臂（arm_concept 阶段）：与 base_v1 手臂并列，带月频回放摘要
  arm_concept?: { payload: SelectionPayload; meta: Record<string, any>; backtest?: Record<string, any> }
  candidate_dirs: { dir: string; mtime_text: string; rows: number }[]
  stats: Record<string, any>
  preselected: SelectionPayload
  selected: SelectionPayload
  diff: { entered: string[]; exited: string[]; kept: string[]
          entered_names: Record<string, string>; exited_names: Record<string, string> }
}
export interface PipelineRunDetail {
  run_id: string; stage: string; status: string; returncode: number | null
  started_at?: string; finished_at?: string; log: string[]
}

export async function getPipelineOverview(): Promise<PipelineOverview> {
  const { data } = await http.get<PipelineOverview>('/pipeline/overview')
  return data
}

export async function getPipelineSelection(): Promise<PipelineSelection> {
  const { data } = await http.get<PipelineSelection>('/pipeline/selection')
  return data
}

export async function startPipelineStage(body: {
  stage: string; config?: string; confirm_overwrite?: boolean; extra_args?: string[]
}): Promise<{ run_id: string; status: string; overwrites_results: boolean }> {
  const { data } = await http.post('/pipeline/runs', body)
  return data
}

export async function getPipelineRun(id: string): Promise<PipelineRunDetail> {
  const { data } = await http.get<PipelineRunDetail>(`/pipeline/runs/${id}`)
  return data
}

// ─── 选股日历 / 历史业绩 ───
export interface CalendarSummary {
  built: boolean; hint?: string
  ledger_mtime?: string
  meta?: Record<string, any>
  sources: string[]
  source_counts?: Record<string, number>
  dates: string[]
  months: string[]
  by_date: Record<string, any>[]
}
export interface CalendarDayBrief {
  date: string; per_source: Record<string, number>; n: number; n_with_return: number
  mean_r1: number | null; mean_r5: number | null; mean_excess_r5: number | null
  mean_r10: number | null; mean_r20: number | null; win_rate_r5: number | null
}
export interface CalendarMonth { month: string; source: string; days: CalendarDayBrief[] }
export interface CalendarPick {
  decision_date: string; source: string; stock_code: string; name: string; industry: string
  rank: number | null; model_score: number | null; weight: number | null; lots: number | null
  note: string
  r1: number | null; r5: number | null; r10: number | null; r20: number | null
  excess_r1: number | null; excess_r5: number | null
  excess_r10: number | null; excess_r20: number | null
  reason: string; stage: string; stage_label: string; stage_evidence: string
  risk: string; risk_notes: string
  /** 这只票命中的策略（后端按 STRATEGY_META 映射，明细行与对比行都有） */
  strategy_keys?: string[]
  strategy_labels?: string[]
  strategy_groups?: string[]
  strategy_text?: string
}
export interface CalendarSourceRelation {
  a: string; b: string
  relation: 'identical' | 'subset' | 'superset' | 'disjoint' | 'partial'
  both: number; only_a: number; only_b: number
}
export interface CalendarCompareCell { rank: number | null; weight: number | null; lots: number | null }
export interface CalendarCompareRow {
  stock_code: string; name: string; industry: string; model_score: number | null
  cells: Record<string, CalendarCompareCell>
  rank?: number | null; lots?: number | null; note?: string; reason?: string
  stage?: string; stage_label?: string; stage_evidence?: string
  risk?: string; risk_notes?: string
  r1?: number | null; r5?: number | null; r10?: number | null; r20?: number | null
  excess_r1?: number | null; excess_r5?: number | null
  excess_r10?: number | null; excess_r20?: number | null
  /** 这只票命中的策略（后端 STRATEGY_META 映射）*/
  strategy_keys?: string[]
  strategy_labels?: string[]
  strategy_groups?: string[]
  strategy_text?: string
}
export interface CalendarDayDetail {
  date: string; source: string; count: number; per_source: Record<string, number>
  mean: Record<string, number | null>
  mean_excess: Record<string, number | null>
  strategies?: string[]
  compare?: CalendarCompareRow[]
  strategy_meta?: Record<string, { label: string; group: string }>
  strategy_summary?: { key: string; label: string; group: string; n: number; stocks: string[]; sources?: string[] }[]
  source_relations?: CalendarSourceRelation[]
  rows: CalendarPick[]
}
export interface CalendarPerformance {
  source: string
  overall: Record<string, any>
  by_date: Record<string, any>[]
  curve: { date: string; equity: number; period_return: number }[]
  by_source?: Record<string, any>[]
  by_stage?: Record<string, any>[]
}

export async function getCalendarSummary(): Promise<CalendarSummary> {
  const { data } = await http.get<CalendarSummary>('/calendar/summary')
  return data
}
export async function getCalendarMonth(month: string, source = ''): Promise<CalendarMonth> {
  const { data } = await http.get<CalendarMonth>('/calendar/month', { params: { month, source } })
  return data
}
export async function getCalendarDay(date: string, source = ''): Promise<CalendarDayDetail> {
  const { data } = await http.get<CalendarDayDetail>('/calendar/day', { params: { date, source } })
  return data
}
export async function getCalendarPerformance(source = ''): Promise<CalendarPerformance> {
  const { data } = await http.get<CalendarPerformance>('/calendar/performance', { params: { source } })
  return data
}

// ─── 系统健康：实现在 api/health.ts（无 axios 依赖，供入口 chunk 使用）───
export { getHealth, type HealthResponse } from './health'

// ─── 自动化进度（轮询；用原生 fetch，避免把 axios 拖进该页的依赖）───
export async function getAutomationStatus(): Promise<AutomationStatus> {
  const resp = await fetch('/api/automation/status')
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return (await resp.json()) as AutomationStatus
}

// ─── 组合绩效（对照聚宽收益概述）───
export async function getPerformanceSummary(): Promise<PerformanceSummary> {
  const resp = await fetch('/api/performance')
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return (await resp.json()) as PerformanceSummary
}
export async function getPerformanceDetail(tag: string): Promise<PerformanceDetail> {
  const resp = await fetch(`/api/performance/${encodeURIComponent(tag)}`)
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return (await resp.json()) as PerformanceDetail
}

// ─── 模拟盘（前向口径）───
export async function getPaperStatus(): Promise<PaperStatus> {
  const resp = await fetch('/api/paper')
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return (await resp.json()) as PaperStatus
}

/** 实盘账户的**盘中实时**视图（实时价重算市值/权重/浮盈/累计；TTL 2 秒，页面 15 秒轮询）。 */
export async function getLiveRealtime(): Promise<LiveRealtime> {
  const resp = await fetch('/api/paper/live/realtime')
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return (await resp.json()) as LiveRealtime
}

/** 实盘持仓的日/周/月 收益对比（vs 中证500）+ 收益日历。payload 较大，独立接口按需取。 */
export async function getLivePerformance(): Promise<LivePerformance> {
  const resp = await fetch('/api/paper/live/performance')
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return (await resp.json()) as LivePerformance
}

/** 挖掘工作台的上下文 chips（面板规模 / 准入口径 / 上次最佳因子 / 研究工具清单）。 */
export async function getMiningContext(): Promise<MiningContext> {
  const { data } = await http.get<MiningContext>('/agent/factors/context')
  return data
}

/** 灵感来源 / 知识库注册表（P2.51）：挖因子时去哪儿找方向、每个来源怎么用、坑在哪。 */
export async function getResearchSources(): Promise<ResearchSourcesResponse> {
  const { data } = await http.get<ResearchSourcesResponse>('/agent/research-sources')
  return data
}

/** 单条因子的审查档案（Inspector）：滚动 IC / 衰减 / 分位组合 / 换手 / 冗余 / 血缘。 */
export async function getFactorInspector(factorId: string): Promise<any> {
  const { data } = await http.get(`/agent/factors/${encodeURIComponent(factorId)}/inspector`)
  return data
}

/** 多条因子的审查档案对比（2–5 条）：滚动 IC 对齐叠加 + 指标表。 */
export async function getFactorInspectorCompare(ids: string[], includeModel = false): Promise<any> {
  const { data } = await http.get('/agent/factors/inspector/compare',
    { params: { ids: ids.join(','), include_model: includeModel ? 1 : 0 } })
  return data
}

/** 因子适应度（可配置 Fitness Hook）打分表。 */
export async function getFactorFitness(profile = 'default'): Promise<any> {
  const { data } = await http.get('/agent/factors/fitness', { params: { profile } })
  return data
}

/** 执行台可点动作清单 + 上次结果（下单前 checklist / 上线评估）。 */
export async function getAgentActions(): Promise<AgentActionsResponse> {
  const { data } = await http.get<AgentActionsResponse>('/agent/actions')
  return data
}

/** 跑一个动作（白名单：pre_order_check / promote_candidate）。 */
export async function runAgentAction(body: {
  action: string; account?: string; strategy_id?: string; factor_id?: string
}): Promise<AgentActionRun> {
  const { data } = await http.post<AgentActionRun>('/agent/actions/run', body)
  return data
}

// ─── 前瞻收益记录（长样本口径，来自选股台账）───
export async function getForwardValidation(): Promise<ForwardValidation> {
  const resp = await fetch('/api/forward-validation')
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return (await resp.json()) as ForwardValidation
}
export async function getForwardSeries(source: string): Promise<ForwardSeries> {
  const resp = await fetch(`/api/forward-validation/${encodeURIComponent(source)}`)
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return (await resp.json()) as ForwardSeries
}

// ─── 导出（同花顺自选股）───
// 类型统一放 @/types/api，避免两处各写一份导致不兼容（这里再转出一次方便调用方）
export type {
  ThsExportList, ThsExportSource, MiningContext, ResearchSource, ResearchSourcesResponse,
} from '@/types/api'
export async function getThsExportFiles(): Promise<ThsExportList> {
  const resp = await fetch('/api/export/ths')
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return (await resp.json()) as ThsExportList
}
export function thsDownloadUrl(name: string) {
  return `/api/export/ths/${encodeURIComponent(name)}`
}

// ─── 选股归因 ───
export async function getAttributionIndex(): Promise<AttributionIndex> {
  const resp = await fetch('/api/attribution')
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return (await resp.json()) as AttributionIndex
}
export async function getAttributionDetail(date: string): Promise<AttributionDetail> {
  const resp = await fetch(`/api/attribution/${encodeURIComponent(date)}`)
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return (await resp.json()) as AttributionDetail
}

// ─── 行情榜单（每日/周 涨跌 + 资金流）───
export async function getRankingsOverview(): Promise<RankingsOverview> {
  const resp = await fetch('/api/rankings/overview')
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return (await resp.json()) as RankingsOverview
}

export interface ChangeParams {
  period: 'daily' | 'weekly'
  direction: 'up' | 'down'
  limit: number
  board?: string
  exclude_st?: boolean
  min_amount?: number
  live?: boolean
  date?: string
}

export async function getChangeRanking(params: ChangeParams): Promise<RankingsChangeResponse> {
  const query = new URLSearchParams({
    period: params.period, direction: params.direction, limit: String(params.limit),
    board: params.board ?? '', exclude_st: String(params.exclude_st ?? false),
    min_amount: String(params.min_amount ?? 0), live: params.live ? '1' : '0',
    date: params.date ?? '',
  })
  const resp = await fetch(`/api/rankings/change?${query}`)
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return (await resp.json()) as RankingsChangeResponse
}

export interface MoneyflowParams {
  period: 'daily' | 'weekly'
  direction: 'in' | 'out'
  limit: number
  source: string
  date?: string
  board?: string
  exclude_st?: boolean
  live?: boolean
}

export async function getMoneyflowRanking(params: MoneyflowParams): Promise<RankingsMoneyflowResponse> {
  const query = new URLSearchParams({
    period: params.period, direction: params.direction, limit: String(params.limit),
    source: params.source, date: params.date ?? '', board: params.board ?? '',
    exclude_st: String(params.exclude_st ?? false), live: params.live ? '1' : '0',
  })
  const resp = await fetch(`/api/rankings/moneyflow?${query}`)
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return (await resp.json()) as RankingsMoneyflowResponse
}

// ─── 连板记录统计 ───
export async function getLimitUpLadder(lookback: number, date = '', live = false): Promise<LimitUpLadder> {
  const query = new URLSearchParams({ lookback: String(lookback), date, live: live ? '1' : '0' })
  const resp = await fetch(`/api/limitup/ladder?${query}`)
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return (await resp.json()) as LimitUpLadder
}

export async function getLimitUpDetail(date: string, live = true): Promise<LimitUpDetail> {
  const query = new URLSearchParams({ date, limit: '300', live: live ? 'auto' : '0' })
  const resp = await fetch(`/api/limitup/detail?${query}`)
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return (await resp.json()) as LimitUpDetail
}

export async function getLimitUpLive(): Promise<LimitUpLive> {
  const resp = await fetch('/api/limitup/live')
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return (await resp.json()) as LimitUpLive
}

// ─── 分时行情（实时）───
export async function getIntraday(
  code: string, period: '1' | '5' = '1', mode: 'auto' | 'live' | 'store' = 'auto', date = '',
): Promise<IntradayResponse> {
  const query = new URLSearchParams({ period, mode, date })
  const resp = await fetch(`/api/intraday/${encodeURIComponent(code)}?${query}`)
  if (!resp.ok) {
    let detail: any = null
    try { detail = (await resp.json()).detail } catch { /* 非 JSON 错误体 */ }
    throw new Error(detail?.message ? `${detail.message}${detail.live_error ? `（${detail.live_error}）` : ''}` : `HTTP ${resp.status}`)
  }
  return (await resp.json()) as IntradayResponse
}

export async function getIntradayCoverage(code: string): Promise<IntradayCoverage> {
  const resp = await fetch(`/api/intraday/coverage/${encodeURIComponent(code)}`)
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return (await resp.json()) as IntradayCoverage
}

export async function getIntradayQuotes(codes: string[]): Promise<{ quotes: IntradayQuote[]; session: SessionPhase }> {
  if (!codes.length) return { quotes: [], session: { phase: 'closed', live: false, is_trading_day_guess: false, now: '' } }
  const resp = await fetch(`/api/intraday/quotes?codes=${encodeURIComponent(codes.join(','))}`)
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return (await resp.json()) as { quotes: IntradayQuote[]; session: SessionPhase }
}

// ─── 行业热度（分行业排名 + 行业内涨跌排名）───
export async function getIndustryOverview(): Promise<IndustryOverview> {
  const resp = await fetch('/api/industry/overview')
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return (await resp.json()) as IndustryOverview
}

export async function getIndustryHeat(
  period: 'daily' | 'weekly' | 'monthly', limit = 60, minN = 0, date = '', live = false,
): Promise<IndustryHeatResponse> {
  const query = new URLSearchParams({ period, limit: String(limit), min_n: String(minN), date, live: live ? '1' : '0' })
  const resp = await fetch(`/api/industry/heat?${query}`)
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return (await resp.json()) as IndustryHeatResponse
}

export async function getIndustryStocks(
  industry: string, period: 'daily' | 'weekly' | 'monthly', direction: 'up' | 'down', limit = 50, date = '', live = false,
): Promise<IndustryStocksResponse> {
  const query = new URLSearchParams({ period, direction, limit: String(limit), date, live: live ? '1' : '0' })
  const resp = await fetch(`/api/industry/${encodeURIComponent(industry)}/stocks?${query}`)
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return (await resp.json()) as IndustryStocksResponse
}

/** 行业热力图（a-share-heatmap 风格：面积=流通市值、颜色=区间涨跌幅、两级下钻）。 */
export async function getIndustryHeatmap(
  period: 'daily' | 'weekly' | 'monthly', market = '', date = '', live = false,
): Promise<IndustryHeatmapResponse> {
  const query = new URLSearchParams({ period, date, live: live ? '1' : '0' })
  if (market) query.set('market', market)
  const resp = await fetch(`/api/industry/heatmap?${query}`)
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return (await resp.json()) as IndustryHeatmapResponse
}


// ─── 个股资料包（日 K 页右侧：档案/估值/财务/资金/龙虎榜/选股记录/公告）───
export interface StockBundle {
  code: string
  built_at: string
  profile: Record<string, any>
  valuation: Record<string, any>
  financials: { available: boolean; rows?: Record<string, any>[]; reason?: string }
  flow: Record<string, any>
  lhb: Record<string, any>
  limits: Record<string, any>
  selections: Record<string, any>
  announcements: Record<string, any>
  links: Record<string, string>
  notes: string[]
}

export async function getStockBundle(code: string): Promise<StockBundle> {
  const resp = await fetch(`/api/stocks/${encodeURIComponent(code)}/bundle`)
  if (!resp.ok) {
    let detail: any = null
    try { detail = (await resp.json()).detail } catch { /* 非 JSON 错误体 */ }
    throw new Error(typeof detail === 'string' ? detail : `HTTP ${resp.status}`)
  }
  return (await resp.json()) as StockBundle
}


// ─── Web 页面新鲜度（自动化最后一步的体检产物）───
export interface WebFreshness {
  available: boolean
  latest_trade_date: string | null
  generated_at?: string
  ok?: boolean
  stale?: string[]
  rows?: { page: string; kind: string; date: string | null; ok: boolean | null
           note: string; artifact: string; mode?: string }[]
  markdown_path?: string | null
  hint?: string
}

export async function getWebFreshness(): Promise<WebFreshness> {
  const resp = await fetch('/api/freshness')
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return (await resp.json()) as WebFreshness
}

// ─── 策略池 ─────────────────────────────────────────────────────────────────
export interface StrategyCandidateInput {
  name: string
  summary?: string
  id?: string
  kind?: string
  origin?: string
  owner?: string
  entry?: string
  stage?: string
  metrics?: Record<string, unknown>
  artifacts?: string[]
  evidence?: string[]
}

export async function getStrategyPool(refresh = true): Promise<import('@/types/api').StrategyPoolResponse> {
  const { data } = await http.get('/strategies', { params: { refresh } })
  return data
}

/** 威科夫点图（P&F）+ 阶段速读（K 线页「威科夫点图」面板）。 */
export async function getWyckoff(code: string, params: { days?: number; box_pct?: number; reversal?: number; range_days?: number } = {}): Promise<import('@/types/api').WyckoffRead> {
  const { data } = await http.get(`/stocks/${encodeURIComponent(code)}/wyckoff`, { params })
  return data
}

/** 策略抽象层的接线矩阵（每条策略接到了哪些消费端；新增策略自动出现）。 */
export async function getStrategyRegistry(): Promise<import('@/types/api').StrategyRegistryResponse> {
  const { data } = await http.get('/strategies/registry')
  return data
}

/** 每条策略的「每日名单刷新」契约状态（策略池 / 选股结果 / 选股日历 / K 线自选 共用同一份产物）。 */
export async function getStrategyListsStatus(): Promise<import('@/types/api').StrategyListsStatus> {
  const { data } = await http.get('/strategies/lists-status')
  return data
}

/** 把挖掘结果登记成候选策略（因子挖掘页「入池」按钮）。同 id 幂等覆盖。 */
export async function registerStrategyCandidate(payload: StrategyCandidateInput) {
  const { data } = await http.post('/strategies/candidates', payload)
  return data as { ok: boolean; id: string; file: string; totals: Record<string, unknown> }
}

export async function setStrategyStage(id: string, stage: string) {
  const { data } = await http.post(`/strategies/${encodeURIComponent(id)}/stage`, { stage })
  return data as { ok: boolean; id: string; stage: string; hint?: string }
}

// ─── 逐股 AI 多空综合分析（K 线页）───────────────────────────────────────────
/** 这只票历史上每次被选中时，AI 的多空分析与买入/持有/卖出评价。 */
export async function getStockAi(code: string, limit = 20): Promise<import('@/types/api').StockAiResponse> {
  const { data } = await http.get(`/stocks/${encodeURIComponent(code)}/ai`, { params: { limit } })
  return data
}

/** AI 结论按决策日的覆盖情况（哪个交易日已经分析过、买/持/卖分布）。 */
export async function getAiPicksSummary(): Promise<import('@/types/api').AiPicksSummary> {
  const { data } = await http.get('/ai-picks/summary')
  return data
}

// ─── 投研执行台（agent）─────────────────────────────────────────────────────
/** 两族 / 四层状态 + 最近一次运行 + 提示词摘要（页面首屏一次取完） */
export async function getAgentOverview(): Promise<import('@/types/api').AgentOverview> {
  const { data } = await http.get('/agent/overview')
  return data
}

export async function getAgentRuns(limit = 20): Promise<{ count: number; runs: import('@/types/api').AgentRunSummary[] }> {
  const { data } = await http.get('/agent/runs', { params: { limit } })
  return data
}

/** 一次运行的完整轨迹（阶段表 + 门槛表 + 预算 + 回滚） */
export async function getAgentRun(runId: string): Promise<import('@/types/api').AgentRunDetail> {
  const { data } = await http.get(`/agent/runs/${encodeURIComponent(runId)}`)
  return data
}

export async function getAgentProfile(): Promise<import('@/types/api').AgentProfileResponse> {
  const { data } = await http.get('/agent/profile')
  return data
}

/** 保存人设/提示词（先备份旧版本；角色默认「量化领域专家」，可改） */
export async function saveAgentProfile(
  payload: Partial<Pick<import('@/types/api').AgentProfile, 'role' | 'system_prompt' | 'detail_level' | 'language' | 'notes'>>,
): Promise<{ saved: boolean; profile: import('@/types/api').AgentProfile; history_count: number }> {
  const { data } = await http.put('/agent/profile', payload)
  return data
}

export async function resetAgentProfile(): Promise<{ saved: boolean; profile: import('@/types/api').AgentProfile; history_count: number }> {
  const { data } = await http.post('/agent/profile/reset')
  return data
}

/** 因子库（族 B 因子 + 族 A 条件，同一张表；后台 scripts/sync_factor_library.py 生成） */
export async function getAgentFactors(
  params: { kind?: string; status?: string; category?: string; q?: string; limit?: number; offset?: number } = {},
): Promise<import('@/types/api').AgentFactorResponse> {
  const { data } = await http.get('/agent/factors', { params })
  return data
}

/** 一只票在选股台账里的入选摘要（自选侧栏悬停用；`/api/calendar/by-code`） */
export interface LedgerCodeSummary {
  code: string
  name: string
  n: number
  n_sources: number
  first_date: string
  last_date: string
  mean_excess_r5?: number | null
  rows: {
    decision_date: string; source: string
    rank?: number | null; weight?: number | null
    r1?: number | null; r5?: number | null; r20?: number | null; excess_r5?: number | null
  }[]
}
export interface LedgerByCodeResponse {
  built: boolean
  as_of: string | null
  requested?: number
  found?: number
  codes: Record<string, LedgerCodeSummary>
  hint?: string
}

/** 批量取入选摘要：100+ 行自选也只发 1 个请求（逐行 bundle 会是 100 个请求）。 */
export async function getSelectionByCode(codes: string[], limit = 3): Promise<LedgerByCodeResponse> {
  const { data } = await http.get<LedgerByCodeResponse>('/calendar/by-code', {
    params: { codes: codes.join(','), limit },
  })
  return data
}

export async function getVolatilityRisk(date?: string, variant = 'cn_ensemble_volcap'): Promise<import('@/types/api').VolatilityRiskResponse> {
  const { data } = await http.get('/strategies/volatility-risk', { params: { date, variant } })
  return data
}
