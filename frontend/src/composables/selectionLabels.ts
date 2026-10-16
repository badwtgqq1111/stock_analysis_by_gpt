/** 台账「选股记录」的 source → 中文名的兜底表（2026-10-15）。
 *
 *  为什么需要兜底：`/api/strategies`（策略池）只登记**策略**，而台账里还有
 *  `preselected / notify / survey_raw / survey_gate / arm` 这些**口径来源**
 *  （候选池、PK 后最终名单、调研回看…）—— 只查策略池的话，浮层会退化成
 *  `preselected` 这种内部 id（实测踩到）。表与「选股日历」页的 STRATEGY_LABEL 同源，
 *  两边说的必须是同一个中文名。
 */
export const LEDGER_SOURCE_LABEL: Record<string, string> = {
  notify: '模型策略（PK 后最终名单）',
  preselected: '模型策略（候选池）',
  arm: '小市值/研究手臂（arm_select）',
  arm_concept: '行业轮动（概念/板块手臂）',
  rot_arm: '行业轮动（MST 大类 + 7 因子等权）',
  box_arm: '打板/低吸（箱体突破回踩）',
  first_ban_lowxi: '打板/低吸（首板低吸·反例）',
  jq_multi_factor: '多因子策略（低市值+高ROE）',
  jq_roe_roa: '多因子策略（ROE＋0.25×ROA）',
  survey_raw: '调研回看（原）',
  survey_gate: '调研回看（门禁）',
}

/** 把台账来源的中文名并入「策略池 id → 中文名」表（策略池优先，因为它跟着登记表走）。 */
export function mergeStrategyNames(
  pool: Record<string, string>, extra: Record<string, string> = LEDGER_SOURCE_LABEL,
): Record<string, string> {
  return { ...extra, ...pool }
}

// ── "月份"这个视图状态，选股日历与 K 线迷你日历条是同一条时间轴 ─────────────────
// 2026-10-15（第九十八轮）：日历页早就把月/来源/选中日写进 URL + localStorage
// （`selection_calendar_state_v1`），K 线的迷你条却自己记一份 ⇒ 从日历点进 K 线会跳回
// "最后一根 K 线所在月"，用户刚看的那个月丢了。这里做成**同一份状态**：
//   读：显式 ?month= → 日历存的月 → 都没有才让调用方自己兜底
//   写：?month= + 日历那份 localStorage（不回写的话，从 K 线切回日历又丢了）
export const CALENDAR_STATE_KEY = 'selection_calendar_state_v1'
const MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/

export function readSharedMonth(query?: Record<string, unknown>): string {
  const fromQuery = String((query?.month as string) || '')
  if (MONTH_RE.test(fromQuery)) return fromQuery
  try {
    const raw = localStorage.getItem(CALENDAR_STATE_KEY)
    const saved = raw ? (JSON.parse(raw) as Record<string, unknown>) : null
    const month = String(saved?.month || '')
    if (MONTH_RE.test(month)) return month
  } catch { /* 隐私模式 / 脏 JSON：当作没存过 */ }
  return ''
}

export function writeSharedMonth(month: string): void {
  if (!MONTH_RE.test(month)) return
  try {
    const raw = localStorage.getItem(CALENDAR_STATE_KEY)
    const saved = raw ? (JSON.parse(raw) as Record<string, unknown>) : {}
    localStorage.setItem(CALENDAR_STATE_KEY, JSON.stringify({ ...saved, month }))
  } catch { /* 忽略：写不进也不该阻断翻月 */ }
}
