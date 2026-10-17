import axios from 'axios'
export interface Source { title: string; locator: string; url: string; authors?: string; date?: string }
export interface Semantics {
  dsl_version: string; result_type: string
  variables: { name: string; label: string; unit: string; window: string; availability: string; definition: string }[]
  operators: { name: string; description: string }[]
  parameters: { value: number; role: string }[]
}
export interface Factor {
  factor_id: string; version: number; name: string; formula: string; description: string; sources: Source[]
  expression_language?: string
  change_note: string; action: string; sha256: string; created_at: string; evaluation_status: string
  parent_factor_id: string | null; parent_version: number | null; semantics: Semantics
}
export interface Variant {
  id: string; name: string; decision_type: string; implementation_version: string; status: string
  benchmark_id: string | null; factor_refs: { factor_id: string; version: number }[]
  gates?: { factor_id: string; op: string; value?: number; other_factor_id?: string }[]
}
export interface Family { id: string; name: string; primary_benchmark: string; market_benchmark: string; variants: Variant[] }
export interface AlphaQCMPerformance {
  available: boolean; reason?: string; dates?: string[]; nav?: number[]; benchmark_nav?: number[]
  metrics?: Record<string, number | null>; benchmark_metrics?: Record<string, number | null>
  excess_return?: number; observations?: number; factor_count?: number
  start?: string; end?: string; run_dir?: string
}
export const factorAPI = {
  async list(): Promise<{ factors: Factor[]; families: Family[] }> { return (await axios.get('/api/factor-pool')).data },
  async history(id: string): Promise<Factor[]> { return (await axios.get(`/api/factor-pool/${id}/history`)).data.versions },
  async validate(formula: string, expression_language = 'p2_71_dsl'): Promise<Semantics> { return (await axios.post('/api/factor-pool/validate', { formula, expression_language })).data },
  async save(id: string, data: unknown): Promise<Factor> { return (await axios.post(`/api/factor-pool/${id}/versions`, data)).data },
  async alphaqcmPerformance(): Promise<AlphaQCMPerformance> { return (await axios.get('/api/factor-pool/alphaqcm/performance')).data },
  async importAlphaQCM(run_dir: string, update = false): Promise<unknown> { return (await axios.post('/api/factor-pool/alphaqcm/import', { run_dir, update })).data },
}
