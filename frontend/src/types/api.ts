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
  realtime?: { available: boolean; asof: string; provisional: boolean }
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
  today_attempts?: number
  today_last_error?: string | null
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
  live?: LiveMirror
}

// ─── 实盘镜像（真实持仓：中泰证券账户快照 + 盯市 + 逐票建议）───
export interface LiveMirrorPosition {
  asof_date?: string
  stock_code: string
  name: string
  shares: number
  cost_price: number
  last_price: number
  price_source?: string
  cost_basis: number
  market_value: number
  pnl_amount: number
  pnl_pct: number
  weight: number
  // 建议列由 scripts/build_live_mirror.py 一并写进 positions.csv（前端不再二次 join）
  action?: '买入' | '持有' | '卖出' | null
  rule?: string
  reason?: string
  momentum_20d?: number | null
  in_selected?: boolean
  in_preselected?: boolean
  selected_rank?: number | null
}
export interface LiveMirror {
  available: boolean
  generated_at?: string | null
  asof?: string | null
  broker?: string | null
  account_masked?: string | null
  captured_at?: string | null
  snapshot_age_days?: number | null
  price_source?: string | null
  live_quotes_error?: string
  cash?: number | null
  market_value?: number | null
  total_assets?: number | null
  cost_basis?: number | null
  unrealized_pnl?: number | null
  realized_pnl?: number | null
  float_pnl_incl_realized?: number | null
  initial_capital_implied?: number | null
  cumulative_return?: number | null
  top1_weight?: number | null
  top3_weight?: number | null
  max_weight_single_allowed?: number | null
  advice_counts?: Record<string, number>
  advice_rules?: Record<string, string>
  observations?: number | null
  sample_status?: string | null
  relative_20d?: {
    code?: string; name?: string; window_start?: string; window_end?: string
    return_20d?: number; portfolio_20d?: number | null; excess_20d?: number | null
    coverage_weight?: number
    per_position?: Record<string, { momentum_20d: number | null; excess_20d: number | null }>
  }
  picks_date?: string | null
  selected_codes?: string[]
  preselected_codes?: string[]
  notes?: string[]
  reconciliation?: Record<string, number | null>
  positions: LiveMirrorPosition[]
  cleared_positions: { stock_code: string; name: string; realized_pnl: number }[]
  curve: { date: string; nav: number; cum_return: number; drawdown: number; price_source?: string }[]
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
  realtime?: RealtimeMeta
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
  realtime?: RealtimeMeta
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
  realtime?: RealtimeMeta
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
  realtime?: RealtimeMeta
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

// ─── 行业热度（分行业排名 + 行业内涨跌排名）───
export interface IndustryHeatWeight { key: string; weight: number; label: string }

export interface IndustryOverview {
  latest_trade_date: string | null
  trade_dates: string[]
  periods: { key: string; label: string; span: number }[]
  weights: IndustryHeatWeight[]
  n_industries: number
  industries: { industry: string; n: number }[]
  session: SessionPhase
  notes: string[]
}

export interface IndustryHeatRow {
  rank: number
  industry: string
  heat: number
  heat_z: number
  heat_prev: number | null
  heat_delta: number | null
  ret: number
  ret_mean: number
  up_ratio: number
  n: number
  net_amount_wan: number
  net_amount_yi: number | null
  flow_intensity: number
  zt_days: number
  zt_density: number
  boards_ge2: number
  amount: number
  volume_boost: number
  leaders: string
}

export interface IndustryHeatResponse {
  realtime?: RealtimeMeta
  available: boolean
  period: string
  period_label: string
  weights: IndustryHeatWeight[]
  market: {
    date: string; base_date: string; span: number; ret: number; up_ratio: number
    n_stocks: number; n_industries: number; n_unclassified: number
    net_amount_wan: number; zt_days: number; amount: number; source: string
  }
  rows: IndustryHeatRow[]
  n_total: number
  min_n?: number
  notes: string[]
  hint?: string
}

export interface IndustryStockRow {
  rank: number
  stock_code: string
  name: string
  market: string
  /** 流通市值（亿元），缺市值快照时为 null */
  mktcap_yi: number | null
  /** 行业内流通市值排名：1=龙一、2=龙二、null=未进前二（只标不荐，买龙头回放更差） */
  leader_rank: number | null
  period_ret: number
  pct_chg: number | null
  close: number | null
  amount: number | null
  net_amount_wan: number | null
  net_amount_yi: number | null
  boards: number
  is_limit_up: boolean
  is_st: boolean
}

export interface IndustryStocksResponse {
  realtime?: RealtimeMeta
  available: boolean
  industry: string
  period: string
  period_label: string
  stats: { n: number; ret: number; up_ratio: number; net_amount_yi: number | null }
  market: IndustryHeatResponse['market']
  rows: IndustryStockRow[]
  n_total: number
}

// ─── 行业热力图（行业 → 成分股 两级；面积 = 流通市值，颜色 = 区间涨跌幅）─────────
export interface IndustryHeatmapStock {
  stock_code: string
  name: string
  ret: number | null
  pct_chg: number | null
  close: number | null
  mktcap_yi: number | null
  amount: number | null
  boards: number
  is_limit_up: boolean
}

export interface IndustryHeatmapIndustry {
  industry: string
  n: number
  ret: number | null
  mktcap_yi: number | null
  amount: number | null
  up_ratio: number | null
  stocks: IndustryHeatmapStock[]
}

export interface IndustryHeatmapResponse {
  realtime?: RealtimeMeta
  available: boolean
  period: string
  period_label: string
  date: string
  base_date: string
  span: number
  market_filter: string
  mktcap_date: string | null
  mktcap_missing: number
  industries: IndustryHeatmapIndustry[]
  totals: {
    n_stocks: number
    n_industries: number
    mktcap_yi: number | null
    up: number
    down: number
    flat: number
  }
  notes: string[]
  hint?: string
}

// ─── 策略池（config/strategies.json + output/strategy_pool/*）─────────────────
export interface StrategyArtifact {
  path: string
  exists: boolean
  rows: number | null
  latest_date: string | null
  mtime: string | null
  bytes: number | null
  detail: string
}

export interface StrategyPerformanceCurve {
  status: string
  /** selection_tracking_gross（名单跟踪）/ research_replay（独立研究）/ shadow_account_net（模拟盘净值） */
  kind: string
  source?: string
  reason: string
  methodology: string
  benchmark_name: string
  benchmark_code: string
  start_date?: string
  end_date?: string
  observations?: number
  dates: string[]
  nav: number[]
  benchmark_nav: number[]
  metrics: Record<string, number | null>
  benchmark_metrics: Record<string, number | null>
  excess_return?: number
}

export interface StrategyPerformance {
  tracking?: StrategyPerformanceCurve
  research?: StrategyPerformanceCurve
  common?: StrategyPerformanceCurve
  stale?: boolean
}

export interface StrategyPoolEntry {
  id: string
  name: string
  group: string
  kind: string
  stage: string
  stage_label: string
  owner: string
  tag_class: string
  summary: string
  entry: string
  schedule: string
  ledger_sources: string[]
  selection_source: string
  performance?: StrategyPerformance
  /** 年化主榜：至少252个日收益，短样本/缺数不授予名次。 */
  annual_rank?: {
    rank: number; of: number; metric: string; value: number
    observations?: number | null; window?: string; basis?: string
  }
  annual_rank_reason?: string
  /** 收益率排名（后端 sync_strategy_pool 计算；各策略窗口不同 ⇒ 只做同池横比参考） */
  return_rank?: {
    rank: number; of: number; metric: string; value: number | null
    excess_return?: number | null; observations?: number | null; window?: string; basis?: string
  }
  excess_rank?: {
    rank: number; of: number; metric: string; value: number | null
    observations?: number | null; window?: string; basis?: string
  }
  latest_date: string | null
  lag_days: number | null
  fresh: boolean
  n_picks: number | null
  n_snapshots: number
  history_dir: string
  artifacts: StrategyArtifact[]
  /** 策略自己的回放指标（来自 metrics_file，如 arm_concept_backtest.json） */
  metrics: { key: string; label: string; value: number }[]
  metrics_pct: string[]
  health: 'ok' | 'stale' | 'missing'
  /** 这条策略今天的名单刷新结果（来自 output/results_cn/strategy_lists/status.json） */
  refresh?: StrategyListRefresh | null
  /** 抽象层接线表：这条策略接到了哪些页面/流程、靠什么接（新增策略自动出现） */
  wiring?: StrategyWiring | null
}

/** 单个消费端的接线情况（`wired=False` 时看 `how` 为空 —— 多数是设计如此，见 coverage.by_design）。 */
export interface StrategyWiringConsumer {
  wired: boolean
  how?: string | null
  [key: string]: unknown
}

export type StrategyWiring = Record<string, StrategyWiringConsumer>

/** 抽象层接线矩阵（/api/strategies/registry）。 */
export interface StrategyRegistryResponse {
  generated_at: string
  n_strategies: number
  consumers: Record<string, string>
  rows: {
    id: string
    name: string
    stage: string
    stage_label: string
    kind: string
    group: string
    selection_source: string | null
    refresh: string
    ledger_sources: string[]
    curve: Record<string, boolean>
    consumers: StrategyWiring
  }[]
  coverage: {
    generated_at: string
    n_strategies: number
    consumers: Record<string, string>
    matrix: Record<string, string[]>
    gaps: { strategy: string; consumer: string; reason: string | null }[]
    by_design: { strategy: string; consumer: string; reason: string | null }[]
  }
  problems: string[]
  kinds: Record<string, string>
}

/** 单条策略的每日名单刷新结果。状态：updated / already_fresh / no_signal / stale / timeout / failed / skipped。 */
export interface StrategyListRefresh {
  id: string
  name?: string
  stage?: string
  mode: string
  status: string
  target_date?: string | null
  date_after?: string | null
  history_after?: string | null
  rows?: number | null
  elapsed_seconds?: number
  command?: string
  log?: string | null
  reason?: string
  archive_note?: string
}

/** 每日名单刷新契约的汇总（策略池 / 选股结果 / 选股日历 / K 线自选 共用的同一份产物）。 */
export interface StrategyListsStatus {
  available: boolean
  hint?: string
  generated_at?: string
  target_date?: string | null
  rows?: StrategyListRefresh[]
  counts?: Record<string, number>
  /** 没有任何 stale / timeout / failed 时为 true（no_signal 不算问题） */
  ok?: boolean
  /** 需要人管的策略 id（stale / timeout / failed） */
  attention?: string[]
  /** 命令跑成功但当日无信号的策略 id（事件 / 结构型策略的正常态） */
  quiet?: string[]
  markdown_path?: string | null
}

export interface StrategyCandidate {
  id: string
  name: string
  kind: string
  stage: string
  stage_label: string
  owner: string
  origin: string
  summary: string
  entry: string
  metrics: Record<string, unknown>
  artifacts: string[]
  evidence: string[]
  created_at: string
  updated_at: string
  file: string
  selection_source?: string
  performance?: StrategyPerformance
}

export interface StrategyEvidence {
  key: string
  title: string
  path: string
  mtime: string
}

export interface StrategyPoolResponse {
  generated_at: string
  reference_trade_date: string | null
  config_path: string
  config_updated_at: string
  stages: { key: string; label: string }[]
  strategies: StrategyPoolEntry[]
  candidates: StrategyCandidate[]
  /** 每日名单刷新契约的汇总（与 /api/strategies/lists-status 同一份产物） */
  lists_status?: StrategyListsStatus
  performance_generated_at?: string | null
  common_window?: { status: string; start_date?: string; end_date?: string; reason?: string }
  /** 收益率排名榜（用户 2026-10-15：「策略池里把策略按收益率排名」） */
  rankings?: {
    by_annual_return?: { id: string; rank: number; annual_return: number; observations: number; window: string }[]
    annual_excluded?: { id: string; reason: string }[]
    by_total_return: { id: string; rank: number; total_return: number | null; annual_return: number | null
                       excess_return: number | null; observations: number | null; window: string; kind?: string }[]
    by_excess_return: { id: string; rank: number; total_return: number | null; excess_return: number | null
                        observations: number | null; window: string }[]
    excluded: { id: string; reason: string }[]
    basis: string
    caveat: string
  }
  evidence: StrategyEvidence[]
  totals: {
    n_strategies: number
    n_production: number
    n_candidates: number
    n_evidence: number
    stale_production: string[]
    fresh: boolean
  }
  notes?: string[]
  sync_error?: string
}

// ─── 逐股 AI 多空综合分析（K 线页 /api/stocks/<code>/ai）────────────────────
export interface AiPickRow {
  decision_date: string
  stock_code: string
  name: string
  /** 命中的策略 id（cn_ensemble / arm_select / arm_concept / jq_multi_factor / ...） */
  strategies: string[]
  /** 策略 id 的中文名（后端按 config/strategies.json 映射） */
  strategy_labels: string[]
  /** 买入 / 持有 / 卖出；未分析时为「待分析」 */
  action: string
  /** 0~1；资料不足时可能为 null */
  confidence: number | null
  summary: string
  bull_case: string
  bear_case: string
  risk_flags: string[]
  horizon: string
  status: string
  error: string
  generated_at: string
}

export interface StockAiResponse {
  code: string
  available: boolean
  n: number
  name?: string
  latest: AiPickRow | null
  rows: AiPickRow[]
  actions: Record<string, number>
  dates?: string[]
  hint?: string
}

export interface AiPicksSummaryDay {
  date: string
  n: number
  actions: Record<string, number>
  pending: number
  strategies: string[]
  strategy_labels: string[]
}

export interface AiPicksSummary {
  available: boolean
  latest_date?: string
  dates: string[]
  by_date: AiPicksSummaryDay[]
  hint?: string
}

// ─── 投研执行台（agent）─────────────────────────────────────────────────────
// 数据源：backend/api/agent.py；轨迹由 scripts/agent_run.py 写 output/agent_runs/<run_id>/run.json
export interface AgentFamilyItem {
  id: string
  name: string
  stage: string
  stage_label: string
  group: string
  entry: string
  selection_source: string
  latest_date: string | null
  n_picks: number | null
  fresh: boolean | null
  health: string | null
  summary: string
}

export interface AgentFamilyBranch {
  key: string
  label: string
  note: string
  items: AgentFamilyItem[]
  evidence: string[]
}

export interface AgentFamily {
  key: string
  label: string
  hint: string
  branches: AgentFamilyBranch[]
}

export interface AgentGate {
  name: string
  label: string
  status: string
  detail: string
  artifact: string | null
}

export interface AgentLayer {
  key: string
  label: string
  families: string[]
  status: string
  detail: string
  metric?: Record<string, unknown>
}

export interface AgentRunSummary {
  run_id: string
  kind: string
  title: string
  /** 挖掘轨迹的会话锚点（`agent_run.py from-session` 写的）；执行台据此跳回「因子挖掘」标签页 */
  session_id?: string | null
  status: string
  started_at: string
  duration_seconds: number | null
  totals: Record<string, number>
  gates: AgentGate[]
  path: string
}

export interface AgentStage {
  name: string
  status: string
  verdict: string
  duration_seconds: number | null
  artifact: string | null
  reason: string | null
  exit_code: number | null
  evidence: string
}

export interface AgentRollback {
  action: string
  path: string
  cmd: string
  reversible: boolean
  note: string
  targets?: string[]
}

export interface AgentRunDetail {
  run_id: string
  kind: string
  title: string
  session_id?: string | null
  factors?: { name: string; expression: string; ic_mean: number | null; icir: number | null; nw_t: number | null }[]
  status: string
  exit_status: number | null
  started_at: string
  finished_at: string
  duration_seconds: number | null
  command: string
  source: Record<string, unknown>
  budget: { seconds?: number | null; used_seconds?: number | null; over?: boolean; tokens?: unknown; requests?: unknown }
  stages: AgentStage[]
  gates: AgentGate[]
  artifacts: string[]
  totals: Record<string, number>
  rollback: AgentRollback
}

export interface AgentProfile {
  role: string
  language: string
  detail_level: string
  system_prompt: string
  notes: string
  updated_at: string | null
  is_default: boolean
}

export interface AgentOverview {
  generated_at: string
  families: AgentFamily[]
  layers: AgentLayer[]
  runs: { count: number; latest: AgentRunSummary | null; n_ok?: number; n_failed?: number }
  gates: AgentGate[]
  factor_library: {
    available: boolean
    generated_at: string | null
    totals: { n: number; by_status?: Record<string, number>; by_kind?: Record<string, number> }
    criteria: Record<string, number>
  }
  profile: { role: string; updated_at: string | null; is_default: boolean }
}

export interface AgentProfileResponse {
  profile: AgentProfile
  default: { role: string; system_prompt: string; language: string; detail_level: string }
  history_count: number
  path: string
}

export interface AgentFactorItem {
  id: string
  kind: string
  source: string
  family: string
  status: string
  grade: string | null
  /** 分类：量价 / 价值 / 财务质量 / 资金流 / 波动 / 流动性 / 结构 / 学术代理 …（见 config/factor_glossary.json） */
  category: string | null
  category_label: string | null
  category_core: string | null
  /** 功能描述（来源如实标：curated 人工 / llm_reason 会话解释 / name_decoder 规则解码） */
  description: string | null
  description_source: string | null
  performance_summary: string | null
  expression: string | null
  note: string | null
  ic_mean: number | null
  icir: number | null
  nw_t: number | null
  fdr_pass: boolean | null
  hlz_pass: boolean | null
  max_corr: number | null
  months: number | null
  cluster_size: number | null
  representative: boolean | null
  evidence: string[]
}

export interface AgentFactorResponse {
  available: boolean
  generated_at: string | null
  criteria: Record<string, number>
  window_months: string[]
  delta: { newly_admitted?: string[]; admitted_previous?: number; admitted_current?: number }
  totals: {
    n: number
    by_status?: Record<string, number>
    by_kind?: Record<string, number>
    by_family?: Record<string, number>
    by_category?: Record<string, number>
    n_admitted?: number
  }
  categories?: { key: string; label: string; core: string }[]
  filtered: number
  offset: number
  limit: number
  items: AgentFactorItem[]
}

// ─── 实盘持仓的日/周/月 收益对比（vs 中证500）+ 收益日历 ───
export interface LiveCurveStats {
  periods: number
  start?: string
  end?: string
  total_return?: number
  benchmark_total_return?: number
  excess_return?: number
  annualized_return?: number | null
  benchmark_annualized_return?: number | null
  max_drawdown?: number
  win_rate?: number | null
  best_period?: number | null
  worst_period?: number | null
  ann_vol?: number | null
  mean_excess?: number | null
}
export interface LiveCalendarCell {
  period: string
  port_ret: number
  bench_ret: number
  excess: number
}
export interface LiveFrequencyBlock {
  dates: string[]
  port_nav: number[]
  bench_nav: number[]
  port_ret: number[]
  bench_ret: number[]
  excess_ret: number[]
  stats: LiveCurveStats
  calendar: LiveCalendarCell[]
}
export interface LivePerformance {
  available: boolean
  reason?: string
  generated_at?: string
  asof?: string
  window?: { start?: string; end?: string; years?: number }
  benchmark?: { code: string; name: string; source: string }
  weight_mode?: string
  weights?: Record<string, number>
  coverage_weight?: number
  notes?: string[]
  stats_daily?: LiveCurveStats
  windows?: Record<string, { label: string; stats: LiveCurveStats }>
  daily?: LiveFrequencyBlock
  weekly?: LiveFrequencyBlock
  monthly?: LiveFrequencyBlock
}

// ─── 实盘账户的**盘中实时**视图（每 2 秒 TTL，前端 15 秒轮询）───
export interface LiveRealtimePosition extends LiveMirrorPosition {
  disk_price?: number
  live_time?: string | null
  change_pct?: number | null
  prev_close?: number | null
}
export interface LiveRealtime {
  available: boolean
  reason?: string
  asof?: string
  cash?: number
  market_value?: number
  total_assets?: number
  cost_basis?: number
  unrealized_pnl?: number
  realized_pnl?: number
  initial_capital_implied?: number
  cumulative_return?: number
  top1_weight?: number
  top3_weight?: number
  n_positions?: number
  priced?: number
  unpriced?: number
  day_pnl?: number | null
  day_pnl_coverage?: number | null
  session?: SessionPhase
  price_source?: string
  /** 报价口径的中文标签：实时 / 今日收盘 / 上一收盘 / 盘后产物 */
  price_basis_label?: string
  /** 报价日期（行情源时间戳里的日期，YYYY-MM-DD） */
  price_date?: string | null
  quote_error?: string
  updated_at?: string
  advice_source?: string
  positions: LiveRealtimePosition[]
}

// ─── 执行台的「可点动作」（P2.42 §4.4 上线评估 / §4.5 下单前 checklist）───
export interface AgentActionCheck {
  name: string
  category: string
  status: 'pass' | 'warn' | 'block'
  detail: string
  evidence: string
}
export interface AgentActionResult {
  generated_at?: string
  account?: string
  date?: string
  latest_trade_date?: string | null
  capital?: number
  verdict?: string
  verdict_label?: string
  counts?: Record<string, number>
  checks?: AgentActionCheck[]
  next_actions?: string[]
  id?: string
  stage_now?: string | null
  reason?: string
  gate_run?: string | null
  gate_row?: Record<string, any> | null
  shadow?: Record<string, any> | null
  evidence_logs?: string[]
  _path?: string
}
export interface AgentActionsResponse {
  actions: {
    key: string
    label: string
    desc: string
    command: string
    accounts?: string[]
    strategies?: { id: string; name: string; stage: string }[]
  }[]
  pre_order: { paper: AgentActionResult | null; live: AgentActionResult | null }
  promote: AgentActionResult | null
}
export interface AgentActionRun {
  action: string
  command: string
  exit: number
  stdout: string
  stderr: string
  result: AgentActionResult | null
}

// ─── 挖掘工作台的「本次上下文」chips（P2.43 §2.3）───
export interface MiningContextTool {
  name: string
  command: string
  artifact: string
  page: string
}
export interface MiningContextTemplate { label: string; direction: string; source?: string }
export interface ResearchSource {
  id: string
  name: string
  region: string
  kind: string
  url?: string | null
  api?: string | null
  cost?: string
  what?: string
  use?: string[]
  caution?: string
  entry?: string
}
export interface ResearchSourcesResponse {
  generated_at?: string
  note?: string[]
  sources: ResearchSource[]
  by_region: Record<string, ResearchSource[]>
  templates: MiningContextTemplate[]
  counts: { sources: number; templates: number }
  path?: string | null
}
export interface MiningContext {
  generated_at?: string
  panel: {
    available: boolean
    version?: string
    stocks?: number | null
    features?: number | null
    asof?: string
    source?: string
  }
  criteria: Record<string, number>
  last_session: {
    available: boolean
    session_id?: string
    env_line?: string
    panel_rows?: string | null
    trading_days?: string | null
    n_factors?: number
    best?: {
      name: string; ic_mean: number; icir: number; nw_t: number
      fdr_pass: boolean; expression: string; reason?: string
    } | null
  }
  tools: MiningContextTool[]
  templates: MiningContextTemplate[]
  research_sources?: ResearchSource[]
}

// ─── 威科夫点图（P&F）与阶段速读（K 线页）─────────────────────────────────────
export interface PnFColumn {
  kind: 'X' | 'O'
  boxes: number[]
  dates: string[]
  top: number
  bottom: number
  n: number
}
export interface PnFChart {
  available: boolean
  reason?: string
  box_size: number
  box_pct: number
  reversal: number
  columns: PnFColumn[]
  support: number | null
  resistance: number | null
  count_target: number | null
  cause_columns?: number
  last_column: 'X' | 'O' | null
  bars: number
  start: string
  end: string
}
export interface WyckoffEvidence { type: string; date: string; detail: string }
export interface WyckoffEvent {
  type: string; label: string; side: number; prob: number; date?: string
  /** TSO 这类"需要后续 K 线确认"的事件在末根上还没确认 */
  pending?: boolean
  /** SOT：那一根是否恰好落在已确认端点上（研报写"端点处"，实现放宽为逐根判定） */
  at_endpoint?: boolean
}
/** 结构摘要（波段端点 / 两顶两底分类 / 辅助线）—— 11 个结构类事件的前置 */
export interface WyckoffStructure {
  pattern: string
  endpoints: number
  lines: number
  endpoint_source?: string
  key_resistance?: string[]
  ob_line?: number | null
  os_line?: number | null
  tops?: number[]
  bottoms?: number[]
}
export interface WyckoffCloud {
  available: boolean
  reason?: string
  date?: string
  cloud?: WyckoffEvent[]
  collapsed?: { type: string; label: string; side: number; prob: number } | null
  threshold?: number
  score?: { wso: number; n_events: number; half_life: number; mapping: string }
  recent_collapsed?: WyckoffEvent[]
  implemented?: string[]
  not_implemented?: string[]
  /** 研报未给构造细节、由本项目口径自定的地方（如实列出） */
  calibers?: string[]
  structure?: WyckoffStructure
  source?: string
  methodology?: string
}

export interface WyckoffRead {
  available: boolean
  reason?: string
  code?: string
  phase?: string
  phase_label?: string
  phase_note?: string
  support?: number | null
  resistance?: number | null
  spring?: { date: string; low: number; recovered_on: string } | null
  upthrust?: { date: string; high: number; rejected_on: string } | null
  sos?: { date: string; close: number; range_high: number; volume_ratio: number | null; volume_ok: boolean } | null
  ret_120?: number | null
  volume_ratio?: number | null
  count_target?: number | null
  cause_columns?: number
  evidence?: WyckoffEvidence[]
  methodology?: string
  disclaimer?: string
  /** 事件概率云（民生证券公式）：每根 K 线的事件似然度 → 概率坍缩 → WSO 分数 */
  event_cloud?: WyckoffCloud
  reference?: string
  pnf: PnFChart
}

export interface RealtimeMeta {
  available: boolean
  asof?: string
  date?: string
  valid?: number
  expected?: number
  excluded?: number
  coverage?: number
  reason?: string | null
}

export interface VolatilityRiskResponse {
  available: boolean
  hint: string
  date?: string
  variant?: string
  dates: string[]
  variants: string[]
  target_vol?: number | null
  audit?: { status: string; error: string | null; exposure: number; predicted_annual_vol: number | null }[]
  contributions?: { stock_code: string; weight: number; marginal_vol: number; component_vol: number; risk_share: number | null; reference_budget_share: number; budget_deviation: number | null }[]
  correlation?: { names: string[]; rows: Record<string, string | number | null>[]; observations: number; asof: string; short_sample: boolean }
  forward?: { status: string; captured_at?: string; eligible_decisions?: number; gates?: Record<string, { status: string; observations: number; decisions: number; checks: Record<string, boolean> }> }
}
