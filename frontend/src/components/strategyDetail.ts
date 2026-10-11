/**
 * 策略池的**公共显示项**与**优劣判定**（把"每种策略该看什么"抽成一处，页面不再各自拼字段）。
 *
 * 为什么单独一个模块：策略池里现在的策略来自不同来源（生产 pipeline / 研究手臂 / 候选规则策略），
 * 它们的产物、指标口径、甚至有没有曲线都不一样。如果卡片里各写各的，页面就会长成三种样子，
 * 也没法回答"这条策略到底行不行"。这里统一成四块：
 *   ① identity   身份（id / 名称 / 分组 / 阶段 / 归属 / 摘要）
 *   ② freshness  新鲜度（最新交易日 / 落后天数 / 名单条数 / 快照数 / 产物清单）
 *   ③ curve      曲线（口径 / 窗口 / 观测数 / 累计 / 相对中证500 / 年化 / 回撤 / 卡玛 / 夏普 / 日度 t）
 *   ④ verdict    优劣（**只由数据规则得出**：正期望、显著性、样本量、回撤、阶段、产物、新鲜度）
 *
 * 判定阈值都写在下面 `RULES` 里，改口径改一处；`verdict` 的每条都带数字原因，便于核对。
 */
import type { StrategyPoolEntry, StrategyPerformanceCurve } from '@/types/api'

export const KIND_LABEL: Record<string, string> = {
  selection_tracking_gross: '名单跟踪（毛收益）',
  shadow_account_net: '模拟盘净值（含成本）',
  strategy_rule_net: '策略原始口径（逐笔模拟）',
  research_replay: '独立研究回放',
}

export const RULES = {
  minObsForJudgement: 30,      // 少于这个观测数：不判方向（卡片标"样本不足"）
  minObsForConfidence: 252,    // 一个完整年：才敢说"样本充足"
  significantT: 1.5,           // |日度 t| ≥ 1.5 视为"有方向性证据"（粗口径，正式判定走 promotion_check.py）
  bigDrawdown: -0.30,          // 回撤超过 30% 记为风险项
  weakRiskAdjusted: 0.5,       // 卡玛与夏普都 < 0.5 且样本≥一年 → 记「风险调整后收益偏弱」
} as const

export interface VerdictItem {
  key: string
  label: string
  detail: string
  tone: 'ok' | 'warn' | 'bad' | 'dim'
}

export interface CurveInfo {
  available: boolean
  kind: string
  kindLabel: string
  source: string
  startDate: string | null
  endDate: string | null
  observations: number
  total: number | null
  excess: number | null
  annual: number | null
  maxDrawdown: number | null
  calmar: number | null
  sharpe: number | null
  t: number | null
  negative: boolean
  significant: boolean
}

export interface StrategyDetail {
  identity: {
    id: string; name: string; group: string; groupClass: string
    stage: string; stageLabel: string; stageClass: string
    owner: string; kind: string; summary: string
  }
  freshness: {
    latestDate: string | null; lagDays: number | null; fresh: boolean
    nPicks: number | null; nSnapshots: number; historyDir: string
    artifacts: { path: string; label: string; exists: boolean; latestDate: string | null }[]
    missingArtifacts: number
  }
  curve: CurveInfo
  config: { entry: string; schedule: string; ledgerSources: string[]; selectionSource: string }
  verdict: { headline: VerdictItem; pros: VerdictItem[]; cons: VerdictItem[] }
}

const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const pctText = (v: number | null, digits = 2): string =>
  v === null ? '—' : `${v > 0 ? '+' : ''}${(v * 100).toFixed(digits)}%`

/** 日收益 t 值：t = mean / (sd / √n)。用来给"显著为负/为正"一个可核对的数。 */
export function dailyT(nav: number[] | undefined): number | null {
  if (!nav || nav.length < 3) return null
  const rets: number[] = []
  for (let i = 1; i < nav.length; i++) if (nav[i - 1] > 0) rets.push(nav[i] / nav[i - 1] - 1)
  const n = rets.length
  if (n < 2) return null
  const mean = rets.reduce((a, b) => a + b, 0) / n
  const variance = rets.reduce((a, b) => a + (b - mean) ** 2, 0) / (n - 1)
  const sd = Math.sqrt(variance)
  return sd > 0 ? mean / (sd / Math.sqrt(n)) : null
}

/** 取该策略"用来展示与对比"的那条曲线：研究/策略口径优先，其次名单跟踪。 */
export function primaryCurve(perf?: { tracking?: StrategyPerformanceCurve; research?: StrategyPerformanceCurve; common?: StrategyPerformanceCurve }): StrategyPerformanceCurve | null {
  if (perf?.research?.nav?.length) return perf.research
  if (perf?.tracking?.nav?.length) return perf.tracking
  return null
}

export function curveInfo(curve: StrategyPerformanceCurve | null): CurveInfo {
  if (!curve) {
    return { available: false, kind: '', kindLabel: '无曲线', source: '', startDate: null, endDate: null,
             observations: 0, total: null, excess: null, annual: null, maxDrawdown: null, calmar: null,
             sharpe: null, t: null, negative: false, significant: false }
  }
  const metrics = curve.metrics || {}
  const total = num(metrics.total_return)
  const t = dailyT(curve.nav)
  const obs = curve.observations ?? Math.max((curve.nav?.length || 1) - 1, 0)
  return {
    available: true,
    kind: curve.kind || '',
    kindLabel: KIND_LABEL[curve.kind || ''] || curve.kind || '曲线',
    source: curve.source || '',
    startDate: curve.start_date || null,
    endDate: curve.end_date || null,
    observations: obs,
    total,
    excess: num(curve.excess_return),
    annual: num(metrics.annual_return),
    maxDrawdown: num(metrics.max_drawdown),
    calmar: num(metrics.calmar),
    sharpe: num(metrics.sharpe_rf0),
    t,
    negative: (total ?? 0) < 0,
    significant: t !== null && Math.abs(t) >= RULES.significantT,
  }
}

export function isThin(curve: CurveInfo): boolean {
  return curve.available && curve.observations < RULES.minObsForJudgement
}

/** 默认折叠：负收益 + 样本足够 + 日度 t ≤ -1.5（"显著为负"的粗口径） */
export function shouldCollapse(curve: CurveInfo): boolean {
  if (!curve.available || isThin(curve) || !curve.negative) return false
  return curve.t === null || curve.t <= -RULES.significantT
}

function buildVerdict(entry: StrategyPoolEntry, curve: CurveInfo,
                      freshness: StrategyDetail['freshness']): StrategyDetail['verdict'] {
  const pros: VerdictItem[] = []
  const cons: VerdictItem[] = []

  if (curve.available) {
    if (curve.total !== null && curve.total > 0) {
      pros.push({ key: 'positive', label: '正期望', tone: 'ok',
                  detail: `累计 ${pctText(curve.total)}（${curve.kindLabel}，${curve.observations} 个观测）` })
    }
    if (curve.excess !== null && curve.excess > 0) {
      const strong = curve.t !== null && curve.t >= RULES.significantT
      pros.push({ key: 'beat-benchmark', label: strong ? '显著跑赢中证500' : '跑赢中证500',
                  tone: strong ? 'ok' : 'warn',
                  detail: `超额 ${pctText(curve.excess)}${curve.t === null ? '' : `，日度 t ${curve.t.toFixed(2)}`}` })
    }
    if (curve.excess !== null && curve.excess < 0) {
      cons.push({ key: 'under-benchmark', label: '跑输中证500', tone: 'bad',
                  detail: `超额 ${pctText(curve.excess)}` })
    }
    if (curve.total !== null && curve.total < 0) {
      const sig = curve.t !== null && curve.t <= -RULES.significantT
      cons.push({ key: 'negative', label: sig ? '显著负期望' : '负期望', tone: 'bad',
                  detail: `累计 ${pctText(curve.total)}${curve.t === null ? '' : `，日度 t ${curve.t.toFixed(2)}`}` })
    }
    if (curve.observations < RULES.minObsForConfidence) {
      cons.push({ key: 'thin', label: '样本不足一年', tone: 'warn',
                  detail: `${curve.observations} 个收益观测（< ${RULES.minObsForConfidence}），年化/夏普不作排名` })
    } else {
      pros.push({ key: 'enough-obs', label: '样本充足', tone: 'ok',
                  detail: `${curve.observations} 个收益观测（≥ ${RULES.minObsForConfidence}）` })
    }
    if (curve.observations >= RULES.minObsForConfidence
        && curve.calmar !== null && curve.calmar < RULES.weakRiskAdjusted
        && curve.sharpe !== null && curve.sharpe < RULES.weakRiskAdjusted) {
      cons.push({ key: 'weak-risk-adjusted', label: '风险调整后收益偏弱', tone: 'warn',
                  detail: `卡玛 ${curve.calmar.toFixed(3)}、夏普 ${curve.sharpe.toFixed(3)}（都 < ${RULES.weakRiskAdjusted}）` })
    }
    if (curve.maxDrawdown !== null && curve.maxDrawdown <= RULES.bigDrawdown) {
      cons.push({ key: 'drawdown', label: '回撤偏大', tone: 'warn',
                  detail: `最大回撤 ${pctText(curve.maxDrawdown)}` })
    }
  } else {
    cons.push({ key: 'no-curve', label: '无收益曲线', tone: 'dim',
                detail: '还没有可展示的曲线：先归档名单并跑 build_strategy_pool_performance.py' })
  }

  if (entry.stage === 'production') {
    pros.push({ key: 'production', label: '已在生产', tone: 'ok',
                detail: `每日自动化在跑，最新交易日 ${freshness.latestDate || '—'}` })
  } else if (entry.stage === 'candidate') {
    cons.push({ key: 'candidate', label: '仍是候选', tone: 'warn',
                detail: '未过上线闸门（promotion_check.py 的配对显著 + 前后半窗同号），不参与生产权重' })
  }

  if (freshness.missingArtifacts > 0) {
    cons.push({ key: 'missing-artifacts', label: '产物缺失', tone: 'bad',
                detail: `${freshness.missingArtifacts} / ${freshness.artifacts.length} 项找不到` })
  } else if (freshness.artifacts.length) {
    pros.push({ key: 'artifacts', label: '产物齐全', tone: 'ok',
                detail: `${freshness.artifacts.length} 项全部存在` })
  }
  if (freshness.lagDays) {
    cons.push({ key: 'stale', label: '落后基准交易日', tone: 'bad',
                detail: `落后 ${freshness.lagDays} 天（最新 ${freshness.latestDate || '—'}）` })
  } else if (freshness.latestDate && entry.stage === 'production') {
    pros.push({ key: 'fresh', label: '新鲜度达标', tone: 'ok', detail: `最新交易日 ${freshness.latestDate}` })
  }

  const headline: VerdictItem = !curve.available
    ? { key: 'unknown', label: '待评估', tone: 'dim', detail: '缺少收益曲线，暂无方向' }
    : isThin(curve)
      ? { key: 'thin', label: '样本不足', tone: 'dim',
          detail: `${curve.observations} 个收益观测，方向不可判定` }
      : curve.negative
        ? (shouldCollapse(curve)
            ? { key: 'negative', label: '负期望', tone: 'bad', detail: `累计 ${pctText(curve.total)}，日度 t ${curve.t === null ? '—' : curve.t.toFixed(2)}` }
            : { key: 'weak-negative', label: '弱负', tone: 'warn', detail: `累计 ${pctText(curve.total)}，未达显著` })
        : (curve.excess !== null && curve.excess < 0
            ? { key: 'positive-under', label: '正收益·跑输基准', tone: 'warn',
                detail: `累计 ${pctText(curve.total)}，但相对中证500 ${pctText(curve.excess)}` }
            : { key: 'positive', label: '正期望', tone: 'ok',
                detail: `累计 ${pctText(curve.total)}，相对中证500 ${pctText(curve.excess)}` })

  return { headline, pros, cons }
}

export function strategyDetail(entry: StrategyPoolEntry): StrategyDetail {
  const curve = curveInfo(primaryCurve(entry.performance))
  const artifacts = (entry.artifacts || []).map((a) => ({
    path: a.path,
    label: a.path.split('/').pop() || a.path,
    exists: Boolean(a.exists),
    latestDate: a.latest_date || null,
  }))
  const freshness = {
    latestDate: entry.latest_date || null,
    lagDays: entry.lag_days ?? null,
    fresh: Boolean(entry.fresh),
    nPicks: entry.n_picks ?? null,
    nSnapshots: entry.n_snapshots ?? 0,
    historyDir: entry.history_dir || '',
    artifacts,
    missingArtifacts: artifacts.filter((a) => !a.exists).length,
  }
  return {
    identity: {
      id: entry.id, name: entry.name, group: entry.group,
      groupClass: entry.tag_class || 'dim',
      stage: entry.stage, stageLabel: entry.stage_label || entry.stage,
      stageClass: `st-${entry.stage}`,
      owner: entry.owner || '—', kind: entry.kind || '—',
      summary: entry.summary || '',
    },
    freshness,
    curve,
    config: {
      entry: entry.entry || '',
      schedule: entry.schedule || '',
      ledgerSources: entry.ledger_sources || [],
      selectionSource: entry.selection_source || '',
    },
    verdict: buildVerdict(entry, curve, freshness),
  }
}

export { pctText }
