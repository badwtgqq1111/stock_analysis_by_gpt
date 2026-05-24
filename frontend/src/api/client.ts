import axios from 'axios'
import type {
  StockListResponse, OhlcvResponse, SelectionResponse, ShapResponse,
  FactorICResponse, PortfolioResponse, ImportanceResponse,
} from '@/types/api'

const http = axios.create({ baseURL: '/api', timeout: 30000 })

// Stocks
export async function getStocks(): Promise<StockListResponse> {
  const { data } = await http.get<StockListResponse>('/stocks')
  return data
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
export async function getSelection(): Promise<SelectionResponse> {
  const { data } = await http.get<SelectionResponse>('/selection')
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
