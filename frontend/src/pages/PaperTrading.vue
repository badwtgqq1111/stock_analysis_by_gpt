<template>
  <div class="page">
    <PageHeader
      title="模拟盘（前向）"
      description="按<strong>每天实际的最终名单</strong>下单、T+1 开盘成交，扣佣金+滑点+印花税，建模涨跌停/停牌/退市，逐日盯市。产物在 <code>output/paper_trading/account/</code>，前端只做展示。"
    />

    <StateBlock v-if="error" state="error" title="取不到模拟盘数据" :detail="error" :retry="reload" />
    <StateBlock v-else-if="!data" state="loading" title="加载中…" />
    <StateBlock v-else-if="!data.available" state="empty" title="还没有模拟盘产物"
                detail="先跑：uv run python scripts/run_cn_pipeline.py --stage paper_account" />

    <template v-else>
      <!-- 这页最容易误解成"又一个回测"。把差异写在最上面。 -->
      <div class="panel explain">
        <h6>{{ data.explain.title }}</h6>
        <ul>
          <li v-html="md(data.explain.what)"></li>
          <li v-html="md(data.explain.why_short)"></li>
        </ul>
        <p class="warn-text tiny">⚠️ {{ data.explain.not_what }}</p>
      </div>

      <div class="panel">
        <div class="pill-row">
          <span class="pill">区间 <b>{{ data.account.window[0] }} ~ {{ data.account.window[1] }}</b></span>
          <span class="pill">决策日 <b>{{ data.account.decision_dates }}</b> 天</span>
          <span class="pill">净值行 <b>{{ data.account.nav_rows }}</b></span>
          <span class="pill">成交 <b>{{ data.fills.length }}</b> 笔 / 订单 <b>{{ data.orders.length }}</b> 笔</span>
          <span class="pill">更新 <b>{{ data.generated_at }}</b></span>
        </div>
        <div class="stat-grid">
          <StatTile label="初始资金" :value="money(45000)" sub="账户起点" />
          <StatTile label="期末净值" :value="money(finalNav)" :tone="tone((finalNav ?? 45000) - 45000)" />
          <StatTile label="区间收益" :value="pct(totalReturn)"
                    :tone="tone((finalNav ?? 45000) - 45000)" sub="样本很短，别当结论" />
          <StatTile label="最大回撤" :value="pct(data.account.max_drawdown)" tone="bad" />
        </div>
      </div>

      <div class="panel">
        <h6>执行假设（与日频回放共用同一份）</h6>
        <p class="tiny muted">
          成本/整手/涨跌停/停牌/退市来自 <code>config/execution_assumptions_cn.json</code>；
          这段净值就是按它算的，页面上不放"看不见的口径"。
          <span v-if="assumptions">版本 <b class="mono">{{ assumptions.version }}</b></span>
        </p>
        <div class="pill-row" v-if="assumptions">
          <span class="pill">佣金 <b>{{ assumptions.costs.commission_bps }} bp</b></span>
          <span class="pill">滑点 <b>{{ assumptions.costs.slippage_bps }} bp</b></span>
          <span class="pill">印花税(卖) <b>{{ assumptions.costs.stamp_tax_bps_sell }} bp</b></span>
          <span class="pill">整手 <b>{{ assumptions.lot_size }}</b></span>
          <span class="pill">退市清算 <b>{{ assumptions.suspension.delist_after_missing_sessions }} 日无行情</b></span>
          <span class="pill">成交价 <b>{{ fillRefLabel }}</b></span>
        </div>
        <p class="tiny" :class="parityOk ? 'up' : 'down'" v-if="data.parity">
          <template v-if="!data.parity.available">一致性快照未生成（跑 <code>scripts/execution_parity_check.py</code>）</template>
          <template v-else-if="parityOk">
            ✅ 一致性检查通过：模拟盘与回放共 {{ 0 + (data.parity.differ ?? 0) }} 项不一致，
            已知设计差异：{{ (data.parity.designed || []).join('、') }}（{{ data.parity.generated_at }}）
          </template>
          <template v-else>⚠️ 有 {{ data.parity.differ }} 项执行假设不一致，去 <code>output/verification/execution_parity.json</code> 看是哪项</template>
        </p>
      </div>

      <div class="panel">
        <h6>长样本：候选池前瞻记录（另一条口径，别和上面的账户净值混）</h6>
        <p class="tiny muted">
          上面的模拟盘只有 {{ data.account.decision_dates }} 个决策日（最终名单 2026-09 才归档，更早的丢了）。
          选股台账把每天的候选池连同**已实现的前瞻收益**记了下来，样本长得多 ——
          但它不扣成本、不建模成交，是"决策质量记录"，不是账户业绩。
        </p>
        <div class="table-scroll" v-if="forwardRows.length">
          <table class="data-table">
            <thead><tr><th>口径</th><th>决策日</th><th>区间</th><th>每期等权</th>
              <th>胜率</th><th>累计(不扣成本)</th><th>基准累计</th><th>最大回撤</th></tr></thead>
            <tbody>
              <tr v-for="row in forwardRows" :key="row.source">
                <td class="mono">{{ row.source }}</td>
                <td>{{ row.decision_dates }}</td>
                <td class="mono tiny">{{ row.window[0] }} ~ {{ row.window[1] }}</td>
                <td :class="tone(row.mean_period_return)">{{ pct(row.mean_period_return) }}</td>
                <td>{{ (row.win_rate_periods * 100).toFixed(0) }}%</td>
                <td :class="tone(row.chained_total_return)">{{ pct(row.chained_total_return) }}</td>
                <td :class="tone(row.chained_total_return_bench)">{{ pct(row.chained_total_return_bench) }}</td>
                <td class="down">{{ pct(row.max_drawdown_chained) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="tiny muted" v-else>还没有前瞻记录；跑 <code>uv run python scripts/build_forward_validation.py</code></p>
      </div>

      <div class="panel" v-if="guard && guard.available">
        <h6>PK 护栏（最新决策日 {{ guard.date }}）</h6>
        <p class="tiny muted">
          复盘里唯一通过按决策日符号检验的负向特征就是 <b>rank&gt;20</b>（23/32 天更差，p=0.02），
          但实测最终名单**经常把权重给到没排名的票**。这里每天在下单前点出来（只读，不改生产权重）。
        </p>
        <div class="pill-row">
          <span class="pill" :class="guard.status === 'ok' ? 'up' : 'down'">
            状态 <b>{{ guard.status === 'ok' ? '无违规' : `${guard.violations?.length ?? 0} 条违规` }}</b>
          </span>
          <span class="pill">持仓 <b>{{ guard.n_holdings }}</b></span>
          <span class="pill">最大权重 <b>{{ pct(guard.max_weight) }}</b></span>
          <span class="pill">持仓 rank 中位数 <b>{{ guard.median_rank_of_holdings ?? '—' }}</b></span>
          <span class="pill" :class="(guard.violation_weight_share ?? 0) > 0 ? 'down' : ''">
            违规权重占比 <b>{{ pct(guard.violation_weight_share) }}</b>
          </span>
          <span class="pill" v-if="guard.history">
            历史违规 <b>{{ guard.history.violation_days }}/{{ guard.history.days }}</b> 天
          </span>
        </div>
        <div class="table-scroll" v-if="guard.violations?.length">
          <table class="data-table">
            <thead><tr><th>代码</th><th>名称</th><th>rank（上限 {{ guard.rank_cap }}）</th><th>权重</th><th>备注</th></tr></thead>
            <tbody>
              <tr v-for="v in guard.violations" :key="v.stock_code">
                <td class="mono link" @click="gotoStock(v.stock_code)">{{ v.stock_code }}</td>
                <td>{{ v.name || '—' }}</td>
                <td class="down">{{ v.rank }}</td>
                <td class="down">{{ pct(v.weight) }}</td>
                <td class="tiny muted">{{ v.note || '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="panel" v-if="reference && reference.available">
        <h6>参考账户：{{ reference.label }}</h6>
        <p class="tiny muted">
          「模拟盘」现在**绑定**到这条规则上（<code>config/cn_pipeline.paper_account.reference_variant</code> =
          <b class="mono">{{ reference.variant }}</b>）。它和上面的「生产账户」<b>执行口径完全一样</b>
          （同一引擎 + 同一份执行假设），区别只在**选股名单**：
          生产账户用 <code>paper_selection_history.csv</code>（含归因回填的等权兜底日），
          参考账户用影子组合的 <code>{{ reference.history }}</code>。两个净值不要直接比大小。
        </p>
        <div class="pill-row">
          <span class="pill">规则 <b>{{ reference.label }}</b></span>
          <span class="pill">首决策日 <b>{{ reference.first_decision }}</b></span>
          <span class="pill">期末净值 <b>{{ money(reference.final_nav) }}</b></span>
          <span class="pill">最大回撤 <b class="down">{{ pct(reference.max_drawdown) }}</b></span>
          <span class="pill">成交 <b>{{ reference.n_fills }}</b> 笔</span>
          <span class="pill" v-if="reference.consistency?.vs_shadow_same_variant"
                :class="reference.consistency.vs_shadow_same_variant.identical ? 'up' : 'down'">
            与同名变体 <b>{{ reference.consistency.vs_shadow_same_variant.identical ? '逐位一致' : '不一致' }}</b>
            （最大差 {{ reference.consistency.vs_shadow_same_variant.max_abs_nav_diff }}）
          </span>
        </div>
        <h6 class="mt">参考账户净值</h6>
        <SvgCurve :series="referenceSeries" :height="240" />
        <h6 class="mt">参考账户当前持仓（{{ reference.positions.length }} 只）</h6>
        <div class="table-scroll">
          <table class="data-table">
            <thead><tr><th>日期</th><th>代码</th><th>股数</th><th>市值</th><th>收盘</th></tr></thead>
            <tbody>
              <tr v-for="(p, i) in reference.positions" :key="i">
                <td class="mono">{{ p.asof_date }}</td>
                <td class="mono link" @click="gotoStock(p.stock_code)">{{ p.stock_code }}</td>
                <td>{{ p.quantity }}</td>
                <td>{{ money(p.market_value) }}</td>
                <td>{{ p.close }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="tiny muted mt">
          要照着买：同花顺导出里已经有 <code>reference</code> 源
          （<code>output/results_cn/ths_watchlist/latest_reference_code.txt</code>），导入后就是这个持仓清单。
        </p>
      </div>

      <div class="panel" v-if="shadow && shadow.available">
        <h6>决策逻辑对照（影子组合：同一执行假设、同一资金）</h6>
        <p class="tiny muted">
          现行口径之外并列跑三条**对照规则**（名单从选股台账重建，历史回填到 2025-09）：
          把某条规则换个写法，长期会差多少 —— 没有对照就只能看单条曲线的噪音。
          <span v-if="shadow.caveats" class="warn-text">⚠️ {{ shadow.caveats }}</span>
        </p>
        <div class="table-scroll">
          <table class="data-table">
            <thead><tr><th>口径</th><th>决策日</th><th>区间</th><th>期末净值</th><th>区间收益</th>
              <th>vs 现行</th><th>最大回撤</th><th>年化波动</th><th>成交</th></tr></thead>
            <tbody>
              <tr v-for="v in shadow.variants" :key="v.variant">
                <td>{{ v.label }}</td>
                <td>{{ v.decision_dates }}</td>
                <td class="mono tiny">{{ v.first_decision }} ~ {{ v.last_decision }}</td>
                <td>{{ money(v.final_nav) }}</td>
                <td :class="tone(v.total_return)">{{ pct(v.total_return) }}</td>
                <td :class="v.vs_production === null ? '' : tone(v.vs_production)">
                  {{ v.vs_production === null ? '—' : pct(v.vs_production) }}</td>
                <td class="down">{{ pct(v.max_drawdown) }}</td>
                <td>{{ v.ann_vol === null ? '—' : pct(v.ann_vol) }}</td>
                <td>{{ v.fills }} 笔</td>
              </tr>
            </tbody>
          </table>
        </div>
        <h6 class="mt">上线闸门（影子组合够不够硬：PROMOTE / KEEP / REJECT）</h6>
        <p class="tiny muted">
          每条候选改动跟**它自己的对照**逐期配对（同窗口），再看单票集中度与收益集中度。
          样本不够只会给 KEEP —— 宁可不动生产。
          <span v-if="promotion?.counts">当前：PROMOTE {{ promotion.counts.PROMOTE ?? 0 }} / KEEP {{ promotion.counts.KEEP ?? 0 }} / REJECT {{ promotion.counts.REJECT ?? 0 }}</span>
        </p>
        <div class="table-scroll" v-if="promotion?.rows?.length">
          <table class="data-table">
            <thead><tr><th>候选改动</th><th>对照</th><th>可比期数</th><th>更好/更差</th>
              <th>中位差</th><th>p</th><th>单票上限</th><th>判定</th></tr></thead>
            <tbody>
              <tr v-for="r in promotion.rows" :key="r.variant">
                <td>{{ r.label }}</td>
                <td class="mono tiny">{{ r.vs_baseline }}</td>
                <td>{{ r.common_periods }}</td>
                <td>{{ r.better_periods }} / {{ r.worse_periods }}</td>
                <td :class="tone(r.median_diff_vs_baseline ?? 0)">{{ pct(r.median_diff_vs_baseline) }}</td>
                <td>{{ r.sign_p ?? '—' }}</td>
                <td :class="(r.max_single_weight ?? 0) > 0.35 ? 'down' : ''">{{ pct(r.max_single_weight) }}</td>
                <td :class="r.verdict === 'REJECT' ? 'down' : (r.verdict === 'PROMOTE' ? 'up' : '')">
                  {{ r.verdict }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <h6 class="mt">对照净值曲线</h6>
        <SvgCurve :series="shadowSeries" :height="240" />
      </div>

      <div class="panel">
        <h6>净值曲线（阴影 = 回撤）</h6>
        <SvgCurve :series="navSeries" :height="240" />
        <h6 class="mt">当前持仓（{{ positions.length }} 行，展示最近 {{ data.positions.length }} 条）</h6>
        <div class="table-scroll">
          <table class="data-table">
            <thead><tr><th>日期</th><th>代码</th><th>股数</th><th>市值</th><th>收盘</th></tr></thead>
            <tbody>
              <tr v-for="(p, i) in positions" :key="i">
                <td class="mono">{{ p.asof_date }}</td>
                <td class="mono link" @click="gotoStock(p.stock_code)">{{ p.stock_code }}</td>
                <td>{{ p.quantity }}</td><td>{{ money(p.market_value) }}</td><td>{{ p.close }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="panel">
        <h6>成交流水（{{ data.fills.length }} 笔，T+1 执行）</h6>
        <div class="table-scroll">
          <table class="data-table">
            <thead><tr><th>决策日</th><th>执行日</th><th>代码</th><th>方向</th><th>股数</th>
              <th>成交价</th><th>佣金</th></tr></thead>
            <tbody>
              <tr v-for="(f, i) in fills" :key="i">
                <td class="mono">{{ f.decision_time }}</td>
                <td class="mono">{{ f.executable_from }}</td>
                <td class="mono link" @click="gotoStock(f.stock_code)">{{ f.stock_code }}</td>
                <td :class="f.side === 'buy' ? 'up' : 'down'">{{ f.side === 'buy' ? '买入' : '卖出' }}</td>
                <td>{{ f.quantity }}</td><td>{{ f.price }}</td><td>{{ f.commission }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { getForwardValidation, getPaperStatus } from '@/api/client'
import type { ForwardValidation, PaperStatus } from '@/types/api'
import PageHeader from '@/components/ui/PageHeader.vue'
import StateBlock from '@/components/ui/StateBlock.vue'
import StatTile from '@/components/ui/StatTile.vue'
import SvgCurve from '@/components/ui/SvgCurve.vue'

const router = useRouter()
const data = ref<PaperStatus | null>(null)
const forward = ref<ForwardValidation | null>(null)
const error = ref('')
async function reload() {
  try { data.value = await getPaperStatus(); error.value = '' } catch (e) { error.value = String(e) }
  // 长样本口径单独取：它属于"另一条口径"，取不到也不该让整页失败
  try { forward.value = await getForwardValidation() } catch { forward.value = null }
}
onMounted(reload)
function gotoStock(code: string) { if (code) router.push({ path: '/kline', query: { code } }) }

function md(t: string) {
  return String(t || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
}
function money(v: number | null | undefined) {
  return v === null || v === undefined || Number.isNaN(Number(v)) ? '—'
    : `${Math.round(Number(v)).toLocaleString('zh-CN')} 元`
}
function pct(v: number | null | undefined) {
  return v === null || v === undefined || Number.isNaN(Number(v)) ? '—' : `${(Number(v) * 100).toFixed(2)}%`
}
function tone(v: number) { return Number(v) >= 0 ? 'up' : 'down' }

// final_nav 可能是 null（产物缺失）；显式兜底，避免模板里做可空运算
const finalNav = computed<number | null>(() => data.value?.account.final_nav ?? null)
const totalReturn = computed<number | null>(
  () => (finalNav.value === null ? null : finalNav.value / 45000 - 1))

const navSeries = computed(() => [{
  name: '模拟盘净值', color: '#118ab2',
  points: (data.value?.curve ?? []).map((c) => Number(c.nav)),
}])
const fills = computed(() => [...(data.value?.fills ?? [])].reverse())
const positions = computed(() => [...(data.value?.positions ?? [])].reverse())

const assumptions = computed(() => data.value?.assumptions ?? null)
const parityOk = computed(() => data.value?.parity?.available === true && (data.value?.parity?.differ ?? 1) === 0)
const fillRefLabel = computed(() => {
  const ref = assumptions.value?.fill_reference?.paper_account
  return ref === 'next_session_open' ? 'T+1 开盘' : (ref || '—')
})
const shadow = computed(() => data.value?.shadow ?? null)
const guard = computed(() => data.value?.guard ?? null)
const promotion = computed(() => data.value?.promotion ?? null)
const reference = computed(() => data.value?.reference ?? null)
const referenceSeries = computed(() => [{
  name: reference.value?.label ?? '参考账户', color: '#2a9d8f',
  points: (reference.value?.curve ?? []).map((c) => Number(c.nav)),
}])
// 对照曲线：把各变体按日期对齐后各自归一（起点=1），否则 start capital 会把曲线压平
const shadowSeries = computed(() => {
  const curves = shadow.value?.curves ?? {}
  const colors = ['#118ab2', '#e07a5f', '#2a9d8f', '#8d99ae']
  return Object.entries(curves).map(([name, pts], i) => {
    const base = pts.length ? Number(pts[0].nav) : 1
    return {
      name: shadow.value?.variants.find((v) => v.variant === name)?.label ?? name,
      color: colors[i % colors.length],
      points: pts.map((p) => Number(p.nav) / base),
    }
  }).filter((s) => s.points.length > 1)
})

// 决策日多的排前面（长样本优先展示），过滤掉没有数据的口径
const forwardRows = computed(() => {
  const sources = forward.value?.sources ?? {}
  return Object.values(sources)
    .filter((s) => s && s.decision_dates > 0)
    .sort((a, b) => b.decision_dates - a.decision_dates)
})
</script>

<style scoped>
.explain ul { margin: 0; padding-left: 18px; }
.explain li { font-size: var(--fs-sm); line-height: 1.75; margin-bottom: 4px; }
.explain b { color: var(--accent-gold); }
.muted { color: var(--text-dim); }
code { font-family: var(--font-mono, monospace); font-size: 0.92em; }
.mt { margin-top: var(--sp-3); }
</style>
