<!--
  威科夫点图（P&F，Point & Figure）—— K 线页用（用户 2026-10-14："在 K 线图里补充一下威科夫点图"）。

  为什么单独画而不是叠加在 K 线上：点数图**没有时间轴**（横轴是"列"= 一次反转），
  这正是威科夫的用意 —— 把噪音过滤掉，横盘的"宽度"直接等于 cause 的大小。
  图上给的注解也都来自后端可复现的规则（`backend/services/wyckoff_service.py`）：
    · 实线框：交易区间（近 N 日高/低），spring/SOS 都相对它判定；
    · 金色点线：横向计数目标价（区间上沿 + 区间内列数 × 箱）；
    · ▼ / ▲：spring（下沿假跌破后收回）/ upthrust（上沿假突破后收回）。
-->
<template>
  <div class="wyckoff" v-if="pnf && pnf.columns?.length">
    <div class="wk-legend">
      <span><i class="sw up" />X 上升列</span>
      <span><i class="sw down" />O 下降列</span>
      <span v-if="read?.support"><i class="sw line" />区间 {{ fmt(read.support) }} ~ {{ fmt(read.resistance) }}</span>
      <span v-if="read?.count_target"><i class="sw gold" />计数目标 {{ fmt(read.count_target) }}</span>
      <span class="dim tiny">箱 {{ fmt(pnf.box_size) }}（{{ (pnf.box_pct * 100).toFixed(1) }}%）· {{ pnf.reversal }} 箱反转 ·
        {{ pnf.columns.length }} 列 · {{ pnf.start }} → {{ pnf.end }}</span>
    </div>
    <div class="wk-scroll">
      <svg :width="width" :height="H" :viewBox="`0 0 ${width} ${H}`">
        <!-- 价格网格 -->
        <g>
          <line v-for="(t, i) in priceTicks" :key="'g' + i" :x1="0" :x2="width" :y1="t.y" :y2="t.y"
                stroke="var(--border-color)" stroke-width="1" />
          <text v-for="(t, i) in priceTicks" :key="'tl' + i" :x="4" :y="t.y - 3"
                class="axis" fill="var(--text-dim)">{{ t.label }}</text>
        </g>
        <!-- 交易区间 -->
        <template v-if="read?.support && read?.resistance">
          <line :x1="0" :x2="width" :y1="yOf(read.resistance)" :y2="yOf(read.resistance)"
                stroke="var(--accent-up)" stroke-width="1" stroke-dasharray="6 4" />
          <line :x1="0" :x2="width" :y1="yOf(read.support)" :y2="yOf(read.support)"
                stroke="var(--accent-down)" stroke-width="1" stroke-dasharray="6 4" />
        </template>
        <!-- 计数目标 -->
        <line v-if="read?.count_target" :x1="0" :x2="width" :y1="yOf(read.count_target)"
              :y2="yOf(read.count_target)" stroke="var(--accent-gold)" stroke-width="1" stroke-dasharray="2 5" />
        <!-- X / O 列 -->
        <g v-for="(col, ci) in pnf.columns" :key="'c' + ci">
          <template v-if="col.kind === 'X'">
            <g v-for="(level, bi) in col.boxes" :key="'x' + ci + '-' + bi">
              <line :x1="xOf(ci) - G / 2 + 2" :y1="yOf(level) - G / 2 + 2"
                    :x2="xOf(ci) + G / 2 - 2" :y2="yOf(level) + G / 2 - 2"
                    stroke="var(--accent-up)" stroke-width="1.4" />
              <line :x1="xOf(ci) + G / 2 - 2" :y1="yOf(level) - G / 2 + 2"
                    :x2="xOf(ci) - G / 2 + 2" :y2="yOf(level) + G / 2 - 2"
                    stroke="var(--accent-up)" stroke-width="1.4" />
            </g>
          </template>
          <template v-else>
            <circle v-for="(level, bi) in col.boxes" :key="'o' + ci + '-' + bi"
                    :cx="xOf(ci)" :cy="yOf(level)" :r="G / 2 - 2.5"
                    fill="none" stroke="var(--accent-down)" stroke-width="1.4" />
          </template>
        </g>
        <!-- spring / upthrust 标记 -->
        <g v-if="read?.spring">
          <text :x="springX" :y="yOf(read.spring.low) + 16" fill="var(--accent-up)" font-size="13"
                text-anchor="middle">▲ {{ read.spring.date }}</text>
        </g>
        <g v-if="read?.upthrust">
          <text :x="upthrustX" :y="yOf(read.upthrust.high) - 6" fill="var(--accent-down)" font-size="13"
                text-anchor="middle">▼ {{ read.upthrust.date }}</text>
        </g>
      </svg>
    </div>
  </div>
  <p v-else class="hint tiny">点数图不可用：{{ pnf?.reason || '数据不足' }}</p>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { WyckoffRead } from '@/types/api'

const props = defineProps<{ read: WyckoffRead | null; height?: number }>()

const G = 15                                  // 一格像素（箱）
const PAD = 18
const read = computed(() => props.read)
const pnf = computed(() => props.read?.pnf ?? null)

/** 价格 → 像素：先算所有列的最低/最高箱，再加一点点边距 */
const bounds = computed(() => {
  const cols = pnf.value?.columns || []
  const levels = cols.flatMap((c) => c.boxes)
  const extra = [read.value?.count_target, read.value?.resistance, read.value?.support]
    .filter((v): v is number => typeof v === 'number')
  const all = [...levels, ...extra]
  if (!all.length) return { lo: 0, hi: 1 }
  const lo = Math.min(...all)
  const hi = Math.max(...all)
  const pad = Math.max((hi - lo) * 0.04, 0.01)
  return { lo: lo - pad, hi: hi + pad }
})

const H = computed(() => Math.max(220, props.height ?? 420))
const width = computed(() => Math.max(360, (pnf.value?.columns.length || 1) * (G + 3) + 60))

function yOf(level: number): number {
  const { lo, hi } = bounds.value
  const ratio = (level - lo) / Math.max(hi - lo, 1e-9)
  return PAD + (1 - ratio) * (H.value - 2 * PAD)
}
function xOf(index: number): number {
  return 44 + index * (G + 3) + G / 2
}
function fmt(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : Number(v).toFixed(2)
}

/** 右轴只标 6 档，避免和点图抢空间 */
const priceTicks = computed(() => {
  const { lo, hi } = bounds.value
  const out: { y: number; label: string }[] = []
  for (let i = 0; i <= 5; i += 1) {
    const level = lo + ((hi - lo) * i) / 5
    out.push({ y: yOf(level), label: level.toFixed(2) })
  }
  return out
})

/** spring/upthrust 落在哪一列：按日期找该日期所在的列（取该列第一次出现的箱日期） */
function columnXOf(dateStr: string | undefined): number {
  const cols = pnf.value?.columns || []
  for (let i = 0; i < cols.length; i += 1) {
    if ((cols[i].dates || []).includes(dateStr || '')) return xOf(i)
  }
  return xOf(Math.max(cols.length - 1, 0))
}
const springX = computed(() => columnXOf(read.value?.spring?.date))
const upthrustX = computed(() => columnXOf(read.value?.upthrust?.date))
</script>

<style scoped>
.wyckoff { width: 100%; }
.wk-scroll { overflow-x: auto; }
.wk-legend { display: flex; flex-wrap: wrap; gap: var(--sp-2); align-items: center; font-size: var(--fs-xs);
  color: var(--text-muted); margin-bottom: 4px; }
.wk-legend .sw { display: inline-block; width: 14px; height: 2px; margin-right: 4px; vertical-align: middle; }
.sw.up { background: var(--accent-up); }
.sw.down { background: var(--accent-down); }
.sw.line { background: var(--text-dim); }
.sw.gold { background: var(--accent-gold); }
.axis { font-size: 10px; font-family: var(--font-mono, monospace); }
</style>
