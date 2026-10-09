import axios, { AxiosError } from 'axios'
import type {
  AttributionDetail, AttributionIndex, ThsExportList, PaperStatus, PerformanceDetail, PerformanceSummary, StockListResponse, OhlcvResponse, SelectionResponse, ShapResponse,
  FactorICResponse, PortfolioResponse, ImportanceResponse, AutomationStatus,
  ForwardValidation, ForwardSeries,
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
}
export interface CalendarDayDetail {
  date: string; source: string; count: number; per_source: Record<string, number>
  mean: Record<string, number | null>
  mean_excess: Record<string, number | null>
  strategies?: string[]
  compare?: CalendarCompareRow[]
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
export type { ThsExportList, ThsExportSource } from '@/types/api'
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
