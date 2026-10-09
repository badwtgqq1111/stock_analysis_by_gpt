// ─── Stocks ───
export interface StockOption {
  code: string
  name: string
  is_selected: boolean
}

export interface StockListResponse {
  stocks: StockOption[]
}

// ─── OHLCV / K-line ───
export interface OhlcvDataPoint {
  time: string
  open: number
  high: number
  low: number
  close: number
  volume: number
  ma5: number | null
  ma10: number | null
  ma20: number | null
  ma60: number | null
  vol_ma5: number | null
  vol_ma20: number | null
  change_pct: number | null
  amplitude: number | null
  buy_signal: boolean
  sell_signal: boolean
}

export interface SignalStats {
  buy_count: number
  sell_count: number
  paired_trades: number
  avg_return: number | null
  win_rate: number | null
  max_win: number | null
  max_loss: number | null
}

export interface ChipData {
  prices: number[]
  volumes: number[]
  current_price: number
}

export interface OhlcvLatest {
  close: number
  change_pct: number
  total_bars: number
}

export interface StockProfile {
  code: string
  name: string
  board: string
  industry: string
  list_date: string | null
  list_status: string
  listed_days: number | null
  flags: string[]
}

export interface TrendStage {
  stage: string
  label: string
  evidence: string
  metrics: Record<string, number | null>
}

export interface OhlcvResponse {
  code: string
  name: string
  data: OhlcvDataPoint[]
  signal_stats: SignalStats | null
  chips: ChipData | null
  stage: TrendStage | null
  profile: StockProfile | null
  latest: OhlcvLatest
}

export interface ChartMarker {
  time: import('lightweight-charts').Time
  position: 'aboveBar' | 'belowBar'
  color: string
  /** 'circle' 用于"被选中的交易日"标记（K 线页点开看分时的那一天） */
  shape: 'arrowUp' | 'arrowDown' | 'circle' | 'square'
  text: string
  size?: number
}

// ─── Selection ───
export interface SelectionColumn {
  key: string
  title: string
}

export interface SelectionRow {
  stock_code: string
  stock_name: string
  [key: string]: any
}

export interface SelectionSourceFile {
  role: string; path: string; exists: boolean; mtime: string | null
  rows: number | null; bytes?: number
}
export interface SelectionSourceInfo {
  key: string; label: string; note: string; files: SelectionSourceFile[]
  trade_date: string | null; row_count: number; supports_shap: boolean
}
export interface SelectionSourceOption {
  key: string; label: string; note: string; available: boolean
  supports_shap: boolean; files: SelectionSourceFile[]
}
export interface SelectionResponse {
  rows: SelectionRow[]
  columns: SelectionColumn[]
  empty: boolean
  source?: SelectionSourceInfo
  available?: SelectionSourceOption[]
}

export interface ShapFeature {
  name: string
  value: number
  direction: 'positive' | 'negative'
}

export interface ShapResponse {
  features: ShapFeature[]
}

// ─── Factor IC ───
export interface FactorICSummary {
  factor: string
  mean_ic: number | null
  std_ic: number | null
  ic_ir: number | null
  mean_rank_ic: number | null
  ic_positive_rate: number | null
}

export interface FactorICResponse {
  dates: string[]
  factors: string[]
  ic_series: Record<string, (number | null)[]>
  rank_ic_series: Record<string, (number | null)[]>
  summary: FactorICSummary[]
  top10: FactorICSummary[]
  factor_set: string
  horizon: number
  top_n: number
}

export interface FeatureImportance {
  factor: string
  importance: number
}

export interface ImportanceResponse {
  factor_set: string
  importances: FeatureImportance[]
  feature_count: number
  train_rows: number
}

// ─── Portfolio ───
export interface PortfolioResponse {
  dates: string[]
  equity: number[]
  drawdown: number[]
  metrics: Record<string, string>
  holdings: Record<string, any>[]
  description: string
}

// ─── 自动化进度监控 ───
export interface AutomationStageProgress {
  percent: number; current: number; total: number
  elapsed_s: number; eta_s: number; detail: string
}
export interface AutomationStage {
  group: 'data' | 'main' | 'tail'
  name: string; label: string
  status: 'ok' | 'running' | 'pending' | 'failed' | 'not_in_run'
  seconds?: number; finished_at?: string; note?: string
  progress?: AutomationStageProgress | null
  median_seconds?: number | null
}
export interface AutomationSidecar {
  name: string; available: boolean
  updated_at?: string; done_total?: number; target_total?: number
  session_processed?: number; rate_per_sec?: number; quarantined?: number
}
export interface AutomationAgent { label: string; pid: number | null; running: boolean; last_exit: string }
export interface AutomationCompletenessCheck {
  key: string; name: string; cadence: string; latest: string | null
  n: number | null; ok: boolean; detail: string
}
export interface AutomationStatus {
  generated_at: string
  trade_date: string | null
  today?: string
  today_started?: boolean
  daily: {
    log_dir: string | null
    started_at: string | null
    elapsed_seconds: number | null
    state: 'running' | 'finished' | 'failed' | 'idle'
    failed_stage: string | null
    lock_pid: number | null
    lock_stale_pid?: number | null
    finished_at?: string | null
    done_stages: number
    total_stages: number
    progress: number
    eta_seconds: number | null
    stages: AutomationStage[]
  }
  intraday: AutomationSidecar
  financials: AutomationSidecar
  completeness: (Record<string, any> & { available?: boolean; age_seconds?: number; checks?: AutomationCompletenessCheck[]; not_ok?: string[]; as_of?: string; generated_at?: string }) | null
  agents: AutomationAgent[]
}

// ─── 组合绩效（对照聚宽「收益概述」）───
export interface PerformanceMetric {
  key: string; label: string; value: number | string | null
  fmt: 'pct' | 'pct_raw' | 'num' | 'int' | 'text' | 'money'; note?: string
}
export interface PerformanceCurvePoint {
  date: string; nav: number; bench: number; excess: number; drawdown: number
}
export interface PerformanceTrade {
  date: string; code: string; side: 'buy' | 'sell'; price: number; shares: number
  reason?: string; amount?: number
}
export interface PerformanceExplain {
  title: string; strategy: string; execution: string
  benchmark: string; how_to_read: string; not_what: string
}
export interface PerformanceDetail {
  tag: string; generated_at: string; source: string; trades_source: string | null
  explain?: PerformanceExplain
  account?: { initial_capital: number; final_capital: number; arm: string; start: string; end: string }
  benchmark: { code: string; name: string }
  window: [string, string]; days: number
  metrics: PerformanceMetric[]
  curve: PerformanceCurvePoint[]
  trades: PerformanceTrade[]
  closed_trades: Record<string, any>[]
}
export interface PerformanceSummary {
  available: boolean
  generated_at?: string
  benchmark?: { code: string; name: string }
  source_dir?: string
  arms: {
    tag: string; window: [string, string]; days: number
    total_return: number; ann_return: number; sharpe: number; max_drawdown: number
    sortino: number; calmar: number | null; bench_return: number; excess_return: number
    information_ratio: number; win_rate: number | null; profit_factor: number | null
  }[]
  hint?: string
}

// ─── 模拟盘（paper account，前向口径）───
export interface PaperStatus {
  available: boolean
  generated_at: string | null
  explain: { title: string; what: string; why_short: string; not_what: string }
  account: {
    initial_capital: number; final_nav: number | null; max_drawdown: number | null
    nav_rows: number | null; window: (string | null)[]; decision_dates: number
  }
  curve: { date: string; nav: number; drawdown: number }[]
  positions: Record<string, any>[]
  fills: Record<string, any>[]
  orders: Record<string, any>[]
  // 执行假设（与日频回放共用一份）+ 一致性快照 + 长样本口径摘要
  assumptions?: ExecutionAssumptions
  parity?: { available: boolean; differ?: number; designed?: string[]; generated_at?: string; version?: string }
  forward?: ForwardHint
  shadow?: ShadowSummary
  guard?: GuardSnapshot
  promotion?: PromotionSnapshot
  reference?: ReferenceAccount
}

// ─── 参考账户（模拟盘绑定的那条规则）───
export interface ReferenceAccount {
  available: boolean
  variant?: string
  label?: string
  history?: string
  first_decision?: string | null
  final_nav?: number
  max_drawdown?: number
  nav_rows?: number
  n_fills?: number
  curve: { date: string; nav: number; drawdown: number }[]
  positions: Record<string, any>[]
  fills: Record<string, any>[]
  consistency?: {
    reference_variant?: string
    generated_at?: string
    vs_shadow_same_variant?: { days: number; max_abs_nav_diff: number; identical: boolean }
    vs_production_account?: Record<string, any>
  }
}

// ─── 上线闸门（影子组合够不够硬）───
export interface PromotionRow {
  variant: string
  label: string
  vs_baseline: string
  decision_dates: number
  common_periods: number
  better_periods: number
  worse_periods: number
  median_diff_vs_baseline: number | null
  sign_p: number | null
  total_return: number
  max_drawdown: number
  max_single_weight: number | null
  min_names_per_date: number | null
  verdict: 'PROMOTE' | 'KEEP' | 'REJECT'
  reason: string
}
export interface PromotionSnapshot {
  available: boolean
  generated_at?: string
  counts?: Record<string, number>
  rows?: PromotionRow[]
}

// ─── PK 护栏（不该拿到权重的票）───
export interface GuardViolation {
  stock_code: string
  name: string
  rank: number
  weight: number
  note?: string
}
export interface GuardSnapshot {
  available: boolean
  date?: string
  status?: 'ok' | 'violations'
  rank_cap?: number
  n_holdings?: number
  max_weight?: number | null
  top1_code?: string | null
  top1_name?: string | null
  median_rank_of_holdings?: number | null
  violations?: GuardViolation[]
  violation_weight_share?: number
  generated_at?: string
  history?: { days: number; violation_days: number; median_violation_weight_share: number | null }
}

// ─── 影子组合（决策逻辑对照：现行 vs 对照规则，同一执行假设）───
export interface ShadowVariant {
  variant: string
  label: string
  decision_dates: number
  first_decision: string
  last_decision: string
  last_nav_date: string
  final_nav: number
  total_return: number
  max_drawdown: number
  ann_vol: number | null
  fills: number
  orders: number
  vs_production: number | null
}
export interface ShadowSummary {
  available: boolean
  generated_at?: string
  capital?: number
  caveats?: string
  assumptions_version?: string
  variants: ShadowVariant[]
  curves: Record<string, { date: string; nav: number; drawdown: number }[]>
}

export interface ExecutionAssumptions {
  version: string
  lot_size: number
  costs: { commission_bps: number; slippage_bps: number; stamp_tax_bps_sell: number }
  costs_bps?: Record<string, number>
  price_limit: Record<string, any>
  suspension: Record<string, any>
  fill_reference: Record<string, any>
}

export interface ForwardHint {
  available: boolean
  horizon_days?: number
  sources?: Record<string, { decision_dates: number; window: string[]; mean_period_return: number; win_rate_periods: number }>
}

// ─── 前瞻收益记录（选股台账串出来的长样本口径）───
export interface ForwardValidation {
  available: boolean
  horizon_days: number | null
  sources: Record<string, {
    source: string; horizon_days: number; decision_dates: number; window: string[]
    span_years: number; picks_mean_per_date: number; mean_period_return: number
    mean_excess_return: number; win_rate_periods: number
    chained_total_return: number; chained_total_return_bench: number
    max_drawdown_chained: number; annualized_chained: number | null
    caveats: string; costs_applied: boolean
  }>
  caveats: string | null
  hint?: string | null
}

export interface ForwardSeries {
  source: string
  horizon_days: number
  metrics: Record<string, any>
  columns: { key: string; title: string }[]
  rows: Record<string, any>[]
}

// ─── 导出（同花顺自选股）───
export interface ThsExportSource {
  key: 'all' | 'holdings' | 'preselected' | 'arm'
  label: string
  n: number
  codes: string[]
  names: string[]
  files: string[]
}
export interface ThsExportList {
  available: boolean
  trade_date?: string | null
  dir?: string
  sources: ThsExportSource[]
  how_to_import?: string[] | null
  hint?: string
}

// ─── 选股归因（逐股"为什么被选中"）───
export interface AttributionIndex { available: boolean; dates: string[]; hint?: string | null }
export interface AttributionDetail {
  date: string
  columns: { key: string; title: string }[]
  n: number
  rows: Record<string, any>[]
  markdown: string
  winners_vs_losers: string
  family_columns: string[]
}

// ─── 行情榜单（每日/周 涨跌 + 资金流）───
export interface RankingsOverview {
  latest_trade_date: string | null
  trade_dates: string[]
  moneyflow_dates: string[]
  moneyflow_sources: { key: string; label: string }[]
  boards: string[]
  periods: { key: string; label: string }[]
  session: SessionPhase
  notes: string[]
}

export interface SessionPhase {
  phase: 'auction' | 'am' | 'lunch' | 'pm' | 'closed'
  live: boolean
  is_trading_day_guess: boolean
  now: string
}

export interface RankingRow {
  rank: number
  stock_code: string
  name: string
  industry: string
  market: string
  value: number | null
  unit: string
  value_label: string
  /** 涨跌榜：区间涨跌幅（小数） */
  period_ret?: number
  /** 资金流榜：当日 / 区间净额（万元，面板原始单位） */
  net_amount_wan?: number
  net_amount_yi?: number | null
  net_5d_yi?: number | null
  net_10d_yi?: number | null
  net_20d_yi?: number | null
  net_pct?: number | null
  close: number | null
  pct_chg: number | null
  amount: number | null
  boards: number
  is_limit_up: boolean
  is_limit_down?: boolean
  is_st?: boolean
}

export interface RankingsChangeResponse {
  period: string
  direction: string
  limit: number
  date: string | null
  base_date: string | null
  span: number | null
  stats: Record<string, any>
  rows: RankingRow[]
  notes: string[]
}

export interface RankingsMoneyflowResponse {
  available: boolean
  period: string
  direction: string
  limit: number
  source: string
  source_label: string
  date: string
  dates: string[]
  stats: Record<string, any>
  rows: RankingRow[]
  live: { requested: boolean; available: boolean; reason: string | null; rows: Record<string, any>[] }
  notes: string[]
  hint?: string
}

// ─── 连板记录统计 ───
export interface LimitUpBucket { key: string; label: string }

export interface LimitUpPromotion { prev: number; next: number; rate: number | null }

export interface LimitUpLadderDay {
  date: string
  counts: Record<string, number>
  total: number
  max_boards: number
  promotion: Record<string, LimitUpPromotion>
}

export interface LimitUpLadder {
  buckets: LimitUpBucket[]
  dates: string[]
  series: LimitUpLadderDay[]
  latest: LimitUpLadderDay | null
  lookback: number
  source: string
  notes: string[]
}

export interface LimitUpRow {
  rank?: number
  stock_code: string
  name: string
  market?: string
  industry?: string
  boards: number
  boards_live?: number | null
  bucket: string
  close: number | null
  pct_chg: number | null
  amount: number | null
  st_pit?: boolean
  source?: string
  seal_amount?: number | null
  first_seal?: string | null
  last_seal?: string | null
  open_times?: number | null
  stat?: string | null
}

export interface LimitUpDetail {
  available: boolean
  date: string
  dates: string[]
  counts: Record<string, number>
  counts_label: Record<string, number>
  total: number
  max_boards: number
  rows: LimitUpRow[]
  live: { requested: boolean; available: boolean; reason: string | null }
  session: SessionPhase
  notes: string[]
  hint?: string
}

export interface LimitUpLive {
  date: string
  session: SessionPhase
  available: boolean
  reason: string | null
  rows: LimitUpRow[]
  counts: Record<string, number>
  counts_label: Record<string, number>
  total: number
  max_boards: number
}

// ─── 分时行情 ───
export interface IntradayPoint {
  time: string
  clock: string
  price: number | null
  avg: number | null
  open: number | null
  high: number | null
  low: number | null
  volume_shares: number
  amount_yuan: number | null
}

export interface IntradayLatest {
  price: number | null
  change: number | null
  change_pct: number | null
  avg: number | null
  high: number | null
  low: number | null
  open: number | null
  volume_shares: number
  amount_yuan: number | null
  points: number
  time: string
}

export interface IntradayResponse {
  code: string
  name: string
  period: string
  requested_period: string
  mode: string
  source: 'tx_minute' | 'em' | 'tx_kline' | 'store'
  source_label: string
  date: string
  prev_close: number | null
  latest: IntradayLatest
  points: IntradayPoint[]
  session: SessionPhase
  live: { available: boolean; reason: string | null }
  store: {
    available: boolean
    path: string | null
    first_date?: string | null
    last_date?: string | null
    n_dates?: number
    has_date?: boolean
  }
  notes: string[]
}

/** 分时可用范围（K 线页点某一天之前先问一次：这天到底有没有分时） */
export interface IntradayCoverage {
  code: string
  today: string
  store: { available: boolean; first_date: string | null; last_date: string | null; n_dates: number }
  store_dates: string[]
  live: { available: boolean; dates: string[]; reason: string | null }
  session: SessionPhase
  notes: string[]
}

export interface IntradayQuote {
  stock_code: string
  name?: string
  available: boolean
  source?: string
  price?: number | null
  prev_close?: number | null
  change?: number | null
  change_pct?: number | null
  high?: number | null
  low?: number | null
  open?: number | null
  amount_yuan?: number | null
  volume_shares?: number | null
  time?: string
  reason?: string | null
}
