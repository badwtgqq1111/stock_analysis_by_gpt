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

export interface OhlcvResponse {
  code: string
  name: string
  data: OhlcvDataPoint[]
  signal_stats: SignalStats | null
  chips: ChipData | null
  latest: OhlcvLatest
}

export interface ChartMarker {
  time: import('lightweight-charts').Time
  position: 'aboveBar' | 'belowBar'
  color: string
  shape: 'arrowUp' | 'arrowDown'
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

export interface SelectionResponse {
  rows: SelectionRow[]
  columns: SelectionColumn[]
  empty: boolean
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
