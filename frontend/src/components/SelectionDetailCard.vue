<script lang="ts">
/** 台账「选股记录」的一行（`/api/stocks/{code}/bundle` 的 selections.rows）。 */
export interface SelectionRow {
  decision_date: string
  source: string
  rank?: number | null
  weight?: number | null
  r1?: number | null
  r5?: number | null
  r20?: number | null
  excess_r5?: number | null
}

/** 策略 id → 中文名（策略池一份）；取不到就退回 id，不阻断渲染。 */
export function strategyLabel(src: string | null | undefined, names?: Record<string, string>): string {
  const key = String(src || '')
  return names?.[key] || key || '—'
}
export function pctText(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : `${Number(v) >= 0 ? '+' : ''}${(Number(v) * 100).toFixed(2)}%`
}
export function toneOf(v: number | null | undefined): string {
  return v === null || v === undefined ? '' : (Number(v) >= 0 ? 'up' : 'down')
}
</script>

<template>
  <!-- 入选明细卡：K 线上的入选记号 / 迷你日历条 共用同一份渲染（文案口径只有一处，
       免得「图上悬停」和「日历条悬停」说出两套话）。 -->
  <div class="ms-pop" :class="{ 'ms-pop-flat': flat }">
    <div class="ms-pop-head">
      <b class="mono">{{ date }}</b>
      <span class="dim tiny">入选 {{ rows.length }} 条</span>
      <router-link class="link tiny"
                   :to="{ path: '/calendar', query: calendarQuery }">当日全部 →</router-link>
    </div>
    <table v-if="showDates" class="ms-pop-table tiny">
      <tbody>
        <tr v-for="r in rows" :key="r.source + r.decision_date">
          <td class="mono dim nowrap">
            <!-- 决策日 → K 线看那一天（浮层里也能直接进图；自选侧栏的行点进 K 线是同一套深链） -->
            <a v-if="code" class="link" :title="`看 ${code} 在 ${r.decision_date} 的 K 线`"
               :href="klineHref(r.decision_date)" @click.stop>{{ r.decision_date }}</a>
            <span v-else>{{ r.decision_date }}</span>
          </td>
          <td><span class="tag tiny">{{ labelOf(r.source) }}</span></td>
          <td class="dim nowrap" v-if="r.rank != null">rank {{ r.rank }}</td>
          <td class="dim nowrap" v-if="r.weight">权重 {{ (r.weight * 100).toFixed(1) }}%</td>
          <td class="nowrap" :class="fwTone(r)" :title="fwHint(r)">{{ fwText(r) }}</td>
        </tr>
      </tbody>
    </table>
    <template v-else>
      <div class="ms-pop-row" v-for="r in rows" :key="r.source + r.decision_date">
        <span class="tag tiny">{{ labelOf(r.source) }}</span>
        <span class="tiny dim" v-if="r.rank != null">rank {{ r.rank }}</span>
        <span class="tiny dim" v-if="r.weight">权重 {{ (r.weight * 100).toFixed(1) }}%</span>
        <span class="tiny" :class="fwTone(r)" :title="fwHint(r)">{{ fwText(r) }}</span>
      </div>
    </template>
    <p class="tiny dim ms-pop-note" v-if="pending">这一天的 T+1/T+5/T+20 还没结算
      （前瞻窗口未走完，台账里为空，不是 0）。</p>
    <p class="tiny dim ms-pop-note" v-else-if="fallback">这条的 T+5 超额还没结算
      ⇒ 先显示 T+5/T+1 收益（同一行的 rank/权重不受影响）。</p>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  date: string
  rows: SelectionRow[]
  /** 策略 id → 中文名（策略池一份）；取不到就退回 id，不阻断渲染 */
  names?: Record<string, string>
  /** 当前看的月份（YYYY-MM）：只为把「当日全部 →」的落点对齐到同一月 */
  month?: string
  /** 行的决策日**不止一天**时（自选侧栏给的是"最近 N 条"）把日期显示出来 */
  showDates?: boolean
  /** 有股票代码时，把每一行的决策日做成"去 K 线看那天"的链接 */
  code?: string | null
  /** 嵌在别的定位容器里时用（K 线图上由父容器绝对定位） */
  flat?: boolean
}>()

/** 「当日全部 →」：带上当前看的月份，日历页就不用重新找月（两页共用同一条时间轴）。 */
const calendarQuery = computed(() => (props.month ? { date: props.date, month: props.month } : { date: props.date }))

/** 决策日 → K 线页深链（带上当前月份，让迷你日历条停在同一月）。 */
function klineHref(day: string): string {
  const q = new URLSearchParams({ code: String(props.code || ''), date: day })
  if (props.month) q.set('month', props.month)
  return `#/kline?${q.toString()}`
}

const labelOf = (src: string) => strategyLabel(src, props.names)

/** 前瞻收益：优先「T+5 超额」，没有就退回「T+5 收益」；两样都空说明还没结算。 */
function fwText(r: SelectionRow): string {
  if (r.excess_r5 != null) return `T+5 超额 ${pctText(r.excess_r5)}`
  if (r.r5 != null) return `T+5 收益 ${pctText(r.r5)}`
  if (r.r1 != null) return `T+1 收益 ${pctText(r.r1)}`
  if (r.r20 != null) return `T+20 收益 ${pctText(r.r20)}`
  return 'T+5 待结算'
}
function fwTone(r: SelectionRow): string {
  return toneOf(r.excess_r5 ?? r.r5 ?? r.r1 ?? r.r20)
}

/** 整张卡里一条结算收益都没有 ⇒ 给一句"不是 0"的解释（避免被读成"没跑赢/没数据"）。 */
const pending = computed(() =>
  props.rows.length > 0
  && props.rows.every((r) => r.excess_r5 == null && r.r5 == null && r.r1 == null && r.r20 == null))

/** 有行退化到 T+1/T+20 收益（T+5 超额还没结算）⇒ 在卡底说明为什么"衡量的不是同一个窗口"。 */
const fallback = computed(() =>
  props.rows.some((r) => r.excess_r5 == null && r.r5 == null && (r.r1 != null || r.r20 != null)))

function fwHint(r: SelectionRow): string {
  if (r.excess_r5 != null) return 'T+5 超额 = 该股 T+5 收益 − 当日全市场等权平均（台账口径）'
  if (r.r5 != null) return 'T+5 超额还没算出来，这里显示的是该股自己的 T+5 收益（未减基准）'
  if (r.r1 != null) return 'T+5 还没走完，先显示 T+1 收益（未减基准）'
  if (r.r20 != null) return 'T+1/T+5 都没算出来，显示 T+20 收益（未减基准）'
  return '前瞻窗口未走完，台账里还没有这一天的收益'
}
</script>

<style scoped>
.ms-pop { background: #151b23; border: 1px solid rgba(255, 209, 102, 0.35); border-radius: 6px;
  padding: 6px 8px; box-shadow: 0 8px 20px rgba(0, 0, 0, 0.45); max-width: 460px; }
/* flat：由父容器负责定位/层级（K 线图上的浮层） */
.ms-pop-flat { box-shadow: 0 6px 16px rgba(0, 0, 0, 0.5); }
.ms-pop-head { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; flex-wrap: nowrap; }
.ms-pop-row { display: flex; align-items: center; gap: 8px; padding: 1px 0; flex-wrap: nowrap; }
/* flex 子项默认可收缩到比内容还窄 ⇒ 一行里的「T+5 超额 −0.87%」会被折成两行、rank/权重列跟着错位
   （实测截图）。明细卡每个字段都是"一个不可分割的读数"，统一 nowrap。 */
.ms-pop-head > *, .ms-pop-row > * { white-space: nowrap; }
.ms-pop-table { border-collapse: collapse; }
.ms-pop-table td { padding: 1px 6px 1px 0; vertical-align: top; }
.nowrap { white-space: nowrap; }
.ms-pop-note { margin: 4px 0 0; }
</style>
