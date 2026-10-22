<template>
  <div class="page">
    <PageHeader
      title="策略池"
      description="所有选股策略的登记处：<b>生产中</b>的策略由每日自动化跑并按交易日判新鲜度，<b>候选</b>来自 LLM 因子挖掘 / agent 研究，验证通过后再升格。声明写在 <code>config/strategies.json</code>，运行时状态由 <code>scripts/sync_strategy_pool.py</code> 扫产物生成。"
    >
      <template #actions>
        <span class="tiny dim">基准交易日 <b>{{ pool?.reference_trade_date || '—' }}</b></span>
        <button class="btn-ghost" :disabled="loading" @click="reload">刷新</button>
      </template>
    </PageHeader>

    <StateBlock v-if="error" state="error" title="取不到策略池" :detail="error" :retry="reload" />
    <StateBlock v-else-if="!pool" state="loading" title="加载策略池…" />

    <template v-else>
      <div class="panel">
        <div class="pill-row">
          <span class="pill">生产策略 <b>{{ pool.totals.n_production }}</b></span>
          <span class="pill">候选 <b>{{ pool.totals.n_candidates }}</b></span>
          <span class="pill">研究证据 <b>{{ pool.totals.n_evidence }}</b></span>
          <span class="pill" :class="pool.totals.fresh ? '' : 'bad-pill'">
            新鲜度 <b>{{ pool.totals.fresh ? '全部达标 ✅' : '不达标 ' + pool.totals.stale_production.length + ' 项 ❌' }}</b>
          </span>
          <span class="pill" :class="registryOk ? '' : 'bad-pill'">
            接线 <b>{{ registryOk ? '全覆盖 ✅' : '漏接 ' + (registry?.coverage.gaps.length || 0) + ' 处 ❌' }}</b>
          </span>
          <span class="pill" v-if="negativeCount">负期望 <b class="down">{{ negativeCount }}</b></span>
          <span class="pill dim tiny">配置更新 {{ pool.config_updated_at || '—' }} · 快照 {{ pool.generated_at }}</span>
        </div>
        <p class="hint tiny" v-if="pool.totals.stale_production.length">
          落后的策略：{{ pool.totals.stale_production.join('、') }} —— 每日自动化末尾会跑
          <code>sync_strategy_pool.py --strict</code>，不达标的会写进运行日志并告警。
        </p>
        <p class="hint tiny">
          左侧点策略看详情。统一对比基准：中证500（000905.SH） · 绩效快照 {{ pool.performance_generated_at || '待生成' }}。
          <b>负期望</b>（样本 ≥{{ RULES.minObsForJudgement }} 且日度 t ≤ −{{ RULES.significantT }}）的策略默认折叠曲线，
          只保留结论；<b>正期望</b>的展示与中证500 的对比。
        </p>
        <!-- 每日名单刷新契约：每条策略今天有没有更新名单，在这里点名（不再靠翻 output 目录） -->
        <StrategyListsStatusBar :status="pool.lists_status" />
        <p class="hint tiny">
          <b>接线（抽象层）</b>：选股结果页 / 选股日历 / K 线自选 / 同花顺导出 / AI 点评 / 每日刷新 / 体检
          <b>全部从 <code>config/strategies.json</code> 派生</b> —— 新增一条策略只写声明，这些消费端自动出现
          （自检：<code>uv run python scripts/strategy_registry.py --check</code>）。
          <template v-if="registry">
            当前 {{ registry.n_strategies }} 条策略：漏接
            <b :class="registryOk ? 'ok' : 'bad'">{{ registry.coverage.gaps.length }}</b> 处、
            按设计未接 {{ registry.coverage.by_design.length }} 处。
          </template>
        </p>
      </div>

      <div class="panel">
        <b>P2.71 策略设计：</b>决策类型与实现版本分开登记；原规则、等价因子版、组合优化版属于同一策略家族。
        <router-link to="/factors">打开因子池与版本对照</router-link>
        <select v-model="designFilter" aria-label="决策类型筛选"><option value="all">全部决策类型</option><option value="rule">规则</option><option value="factor_combination">因子组合</option><option value="hybrid">混合</option><option value="unknown">未标注</option></select>
        <select v-model="versionFilter" aria-label="实现版本筛选"><option value="all">全部实现版本</option><option value="original">原版</option><option value="equivalent_factorized">等价因子版</option><option value="optimized">优化版</option></select>
      </div>
      <!-- ── 主区：左侧策略列表 + 右侧详情 ─────────────────────────────── -->
      <div class="pool-layout">
        <aside class="pool-side">
          <div class="panel side-panel">
            <h6>
              策略列表
              <span class="dim tiny">{{ visible.length }} / {{ pool.strategies.length }}</span>
            </h6>
            <input v-model="query" class="side-search" placeholder="搜索名称 / id / 分组" />
            <div class="side-tabs">
              <button v-for="f in filterTabs" :key="f.key" class="tab"
                      :class="{ active: filter === f.key }" @click="filter = f.key">
                {{ f.label }} <span class="dim">{{ f.count }}</span>
              </button>
            </div>
            <!-- 两页统一默认按年化收益降序 -->
            <div class="side-sort">
              <span class="tiny dim">排序</span>
              <button v-for="m in sortTabs" :key="m.key" class="tab tiny"
                      :class="{ active: sortMode === m.key }" @click="sortMode = m.key">{{ m.label }}</button>
              <span class="tiny dim" :title="rankingsCaveat">
                {{ rankBasis }}
              </span>
            </div>
            <ul class="strategy-list">
              <li v-for="s in visible" :key="s.id" class="strategy-item"
                  :class="{ active: s.id === selectedId, negative: shouldCollapse(detailOf(s).curve) }"
                  @click="select(s.id)">
                <div class="si-row">
                  <span v-if="annualMode && s.annual_rank" class="rank-badge mono"
                        :class="{ gold: s.annual_rank.rank === 1, silver: s.annual_rank.rank === 2, bronze: s.annual_rank.rank === 3 }"
                        :title="`按年化收益排名：第 ${s.annual_rank.rank} / ${s.annual_rank.of} 名（${s.annual_rank.basis || ''}）｜窗口 ${s.annual_rank.window || '—'}`">
                    #{{ s.annual_rank.rank }}</span>
                  <span v-if="rankMode && s.return_rank" class="rank-badge mono"
                        :title="`按累计收益排名：第 ${s.return_rank.rank} / ${s.return_rank.of} 名｜窗口 ${s.return_rank.window || '—'}`">#{{ s.return_rank.rank }}</span>
                  <span v-if="excessMode && s.excess_rank" class="rank-badge mono"
                        :title="`按相对中证500 排名：第 ${s.excess_rank.rank} / ${s.excess_rank.of} 名｜窗口 ${s.excess_rank.window || '—'}`">
                    #{{ s.excess_rank.rank }}</span>
                  <span class="si-name">{{ s.name }}</span>
                  <span v-if="s.factor_design" class="tiny dim">{{ s.factor_design.family_id }} · {{ s.factor_design.decision_type }} / {{ s.factor_design.implementation_version }}</span>
                  <span class="tag" :class="detailOf(s).identity.groupClass">{{ s.group }}</span>
                  <span v-if="s.variant_of" class="tag dim tiny">变体 · {{ s.variant_of }}</span>
                </div>
                <div class="si-row">
                  <span class="stage-badge" :class="detailOf(s).identity.stageClass">
                    {{ detailOf(s).identity.stageLabel }}</span>
                  <span class="tag" :class="toneTag(detailOf(s).verdict.headline.tone)">
                    {{ detailOf(s).verdict.headline.label }}</span>
                  <span class="tiny dim" v-if="detailOf(s).curve.available">
                    {{ detailOf(s).curve.observations }} 观测</span>
                </div>
                <div class="si-metric mono tiny" v-if="detailOf(s).curve.available">
                  年化 <b :class="(s.annual_rank?.value ?? 0) < 0 ? 'down' : 'up'" :title="s.annual_rank_reason || s.annual_rank?.window">
                    {{ pctText(s.annual_rank?.value ?? null) }}</b>
                  <span class="dim">·</span> 累计 <b :class="(detailOf(s).curve.total ?? 0) < 0 ? 'down' : 'up'">
                    {{ pctText(detailOf(s).curve.total) }}</b>
                  <span class="dim">·</span> 相对中证500
                  <b :class="(detailOf(s).curve.excess ?? 0) < 0 ? 'down' : 'up'">
                    {{ pctText(detailOf(s).curve.excess) }}</b>
                </div>
                <div class="si-metric tiny dim" v-else>暂无曲线</div>
                <div class="si-row tiny dim" v-if="s.annual_rank_reason">{{ s.annual_rank_reason }}</div>
                <div class="si-row tiny dim">
                  最新 {{ detailOf(s).freshness.latestDate || '—' }}
                  <span v-if="detailOf(s).freshness.lagDays">（落后 {{ detailOf(s).freshness.lagDays }} 天）</span>
                  · 名单 {{ detailOf(s).freshness.nPicks ?? '—' }} 只
                  · 快照 {{ detailOf(s).freshness.nSnapshots }}
                </div>
                <div class="si-row tiny" v-if="s.refresh">
                  <span class="tag tiny" :class="refreshTone(s.refresh.status)" :title="s.refresh.reason">
                    {{ refreshIcon(s.refresh.status) }}
                    {{ s.refresh.codes_changed === false ? '已重跑 · 名单未变' : '名单刷新 ' + refreshLabel(s.refresh.status) }}
                  </span>
                  <span class="dim">{{ s.refresh.date_after || '—' }}</span>
                </div>
              </li>
              <li v-if="!visible.length" class="dim tiny" style="padding:8px">没有匹配的策略</li>
            </ul>
          </div>
        </aside>

        <section class="pool-main" v-if="current">
          <!-- ① 概览：身份 + 判定 -->
          <div class="panel detail-head">
            <div class="dh-top">
              <h5>{{ current.identity.name }}</h5>
              <span class="tag" :class="current.identity.groupClass">{{ current.identity.group }}</span>
              <span v-if="entryById(current.identity.id)?.variant_of" class="tag dim tiny">
                变体 · {{ entryById(current.identity.id)?.variant_of }}
              </span>
              <span class="stage-badge" :class="current.identity.stageClass">{{ current.identity.stageLabel }}</span>
              <span class="tag" :class="toneTag(current.verdict.headline.tone)">
                {{ current.verdict.headline.label }}</span>
              <span class="dh-why tiny dim">{{ current.verdict.headline.detail }}</span>
            </div>
            <p class="dh-summary">{{ current.identity.summary }}</p>
            <div class="dh-meta tiny dim mono">
              id {{ current.identity.id }} · kind {{ current.identity.kind }} · owner {{ current.identity.owner }}
              · 曲线口径 {{ current.curve.kindLabel }}
              <template v-if="current.curve.available">
                （{{ current.curve.startDate }} ~ {{ current.curve.endDate }}）</template>
            </div>
            <div class="dh-actions">
              <a v-if="entryById(current.identity.id)?.selection_source" class="btn-ghost btn-sm" :href="selectionLink(entryById(current.identity.id))">看最新名单 →</a>
              <a class="btn-ghost btn-sm" href="#/calendar">选股日历 →</a>
            </div>
          </div>

          <!-- ② 公共指标（所有策略同一套字段） -->
          <div class="panel">
            <h6>
              公共指标
              <span class="dim tiny" v-if="current.curve.available">
                {{ current.curve.kindLabel }} · {{ current.curve.observations }} 个收益观测 · 基准中证500
              </span>
              <span class="dim tiny" v-else>暂无曲线（缺收益数据）</span>
            </h6>
            <div class="tile-grid">
              <div class="tile" v-for="t in metricTiles" :key="t.label">
                <label>{{ t.label }}</label>
                <b class="mono" :class="t.tone">{{ t.value }}</b>
                <span class="tiny dim">{{ t.sub }}</span>
              </div>
            </div>
          </div>

          <!-- ③ 优劣：只由数据规则得出 -->
          <div class="panel">
            <h6>
              优劣
              <span class="dim tiny">判定阈值：样本 ≥{{ RULES.minObsForJudgement }} 才判方向、≥{{ RULES.minObsForConfidence }} 算充足；|日度 t| ≥{{ RULES.significantT }} 视为有方向性证据（正式上线判定走 promotion_check.py）</span>
            </h6>
            <div class="verdict-grid">
              <div class="vcol">
                <div class="vcol-head ok">✅ 优势 <span class="dim tiny">{{ current.verdict.pros.length }}</span></div>
                <ul>
                  <li v-for="p in current.verdict.pros" :key="p.key">
                    <span class="tag ok tiny">{{ p.label }}</span>
                    <span class="tiny">{{ p.detail }}</span>
                  </li>
                  <li v-if="!current.verdict.pros.length" class="dim tiny">（暂无）</li>
                </ul>
              </div>
              <div class="vcol">
                <div class="vcol-head bad">⚠️ 风险 / 不足 <span class="dim tiny">{{ current.verdict.cons.length }}</span></div>
                <ul>
                  <li v-for="c in current.verdict.cons" :key="c.key">
                    <span class="tag tiny" :class="toneTag(c.tone)">{{ c.label }}</span>
                    <span class="tiny">{{ c.detail }}</span>
                  </li>
                  <li v-if="!current.verdict.cons.length" class="dim tiny">（暂无）</li>
                </ul>
              </div>
            </div>
          </div>

          <!-- ④ 单策略：与中证500 对比（正期望默认展开；负期望折叠成一行） -->
          <div class="panel">
            <h6>
              与中证500 对比
              <span class="dim tiny" v-if="curveCollapsed">负期望 → 曲线默认折叠（结论见上）</span>
              <button v-if="current.curve.available" class="btn-ghost btn-sm" style="margin-left:auto"
                      @click="curveCollapsed = !curveCollapsed">
                {{ curveCollapsed ? '展开曲线' : '收起曲线' }}
              </button>
            </h6>
            <div v-if="curveCollapsed" class="collapsed-row">
              <span class="tiny dim">累计</span>
              <b class="mono down">{{ pctText(current.curve.total) }}</b>
              <span class="tiny dim">相对中证500</span>
              <b class="mono down">{{ pctText(current.curve.excess) }}</b>
              <span class="tiny" v-if="current.curve.t !== null">
                日度 t <b class="mono down">{{ current.curve.t.toFixed(2) }}</b></span>
              <span class="tiny dim">曲线与指标只用于说明「这条规则不可上线」。</span>
            </div>
            <StrategyPerformance v-else :data="entryById(current.identity.id)?.performance" />
          </div>

          <!-- ⑤ 多策略对比曲线 -->
          <div class="panel">
            <h6>
              多策略对比
              <span class="dim tiny">共同窗口内归一到 1；口径逐条标注（毛收益 / 含成本 / 逐笔模拟）</span>
            </h6>
            <div class="pick-row">
              <button v-for="s in curveEntries" :key="s.id" class="btn-ghost btn-sm"
                      :class="{ on: compareIds.includes(s.id) }" @click="toggleCompare(s.id)">
                {{ s.name }}
              </button>
            </div>
            <StrategyCurveCompare :series="compareSeries" />
          </div>

          <!-- ⑥ 配置与产物 -->
          <div class="panel">
            <h6>配置与产物 <span class="dim tiny">声明在 config/strategies.json，运行时状态由产物扫描得出</span></h6>
            <div class="kv-grid">
              <div class="kv"><label>入口命令</label><code class="sc-code">{{ current.config.entry || '—' }}</code></div>
              <div class="kv"><label>调度</label><span class="tiny">{{ current.config.schedule || '—' }}</span></div>
              <div class="kv"><label>台账来源</label>
                <span>
                  <span v-for="src in current.config.ledgerSources" :key="src" class="tag dim tiny">{{ src }}</span>
                  <span v-if="!current.config.ledgerSources.length" class="dim tiny">—</span>
                </span>
              </div>
              <div class="kv"><label>选股结果页来源</label>
                <span class="mono tiny">{{ current.config.selectionSource || '—' }}</span></div>
              <div class="kv"><label>历史目录</label><span class="mono tiny">{{ current.freshness.historyDir || '—' }}</span></div>
              <div class="kv"><label>新鲜度</label>
                <span class="tiny">
                  <b :class="current.freshness.fresh ? 'ok' : 'bad'">
                    {{ current.freshness.fresh ? '达标' : '不达标' }}</b>
                  · 最新交易日 {{ current.freshness.latestDate || '—' }}
                  <template v-if="current.freshness.lagDays">（落后 {{ current.freshness.lagDays }} 天）</template>
                </span>
              </div>
              <div class="kv"><label>每日名单刷新</label>
                <span class="tiny" v-if="refreshOf(current.identity.id)">
                  <span class="tag tiny" :class="refreshTone(refreshOf(current.identity.id)!.status)">
                    {{ refreshIcon(refreshOf(current.identity.id)!.status) }}
                    {{ refreshOf(current.identity.id)!.codes_changed === false
                      ? '已重跑 · 名单未变'
                      : refreshLabel(refreshOf(current.identity.id)!.status) }}</span>
                  · 名单 {{ refreshOf(current.identity.id)!.date_after || '—' }}
                  · 历史 {{ refreshOf(current.identity.id)!.history_after || '—' }}
                  · {{ refreshOf(current.identity.id)!.rows ?? '—' }} 只
                  · {{ refreshOf(current.identity.id)!.elapsed_seconds ?? 0 }}s
                  <span class="dim">{{ refreshOf(current.identity.id)!.reason }}</span>
                </span>
                <span class="tiny dim" v-else>未登记刷新状态（跑
                  <code>uv run python scripts/refresh_strategy_lists.py --print</code>）</span>
              </div>
            </div>
            <div class="artifact-list" style="margin-top:6px">
              <span v-for="a in current.freshness.artifacts" :key="a.path" class="artifact"
                    :class="{ missing: !a.exists }" :title="a.path">
                {{ a.label }} <span class="dim tiny">{{ a.latestDate || (a.exists ? '存在' : '缺失') }}</span>
              </span>
              <span v-if="!current.freshness.artifacts.length" class="dim tiny">（未登记产物）</span>
            </div>
          </div>

          <!-- ⑦ 接线（抽象层）：这条策略接到了哪些页面/流程、靠什么接的 -->
          <div class="panel">
            <h6>
              接线（抽象层）
              <span class="dim tiny">这条策略在系统里的"行为"都在这里：声明写在 config/strategies.json，
                消费端从注册表派生（新增策略不用改代码）</span>
            </h6>
            <table class="data-table">
              <thead><tr><th class="left">消费端</th><th class="left">状态</th><th class="left">怎么接的</th></tr></thead>
              <tbody>
                <tr v-for="row in wiringRows" :key="row.key">
                  <td class="left"><b>{{ row.label }}</b><div class="tiny dim mono">{{ row.key }}</div></td>
                  <td class="left">
                    <span class="tag tiny" :class="row.wired ? 'ok' : 'dim'">
                      {{ row.wired ? '✅ 已接' : '➖ 未接' }}</span>
                  </td>
                  <td class="left tiny">{{ row.how || '（按设计不接：无独立名单 / 声明 skip / 无台账来源）' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>
      </div>

      <!-- ── 候选策略（登记表）────────────────────────────────────────── -->
      <div class="panel">
        <h6>
          候选策略（agent / LLM 挖掘产出，需人工升格才会进生产）
          <span class="dim tiny">{{ pool.candidates.length }} 个</span>
        </h6>
        <div v-if="!pool.candidates.length" class="empty">
          还没有候选。跑一次「因子挖掘」页的挖掘，或在研究里产出结论后用
          <code>uv run python scripts/sync_strategy_pool.py --register cand.json</code> 登记。
        </div>
        <table v-else class="data-table">
          <thead>
            <tr><th class="left">候选</th><th class="left">来源</th><th class="left">阶段</th>
              <th class="left">说明</th><th class="left">指标</th><th class="left">更新时间</th>
              <th class="left">操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="c in pool.candidates" :key="c.id">
              <td class="left">
                <a v-if="c.selection_source" class="btn-ghost btn-sm" :href="`#/?source=${c.selection_source}`">看每日名单 →</a>
                <b>{{ c.name }}</b>
                <div class="tiny dim mono">{{ c.id }}</div>
              </td>
              <td class="left tiny">{{ c.origin || '—' }}</td>
              <td class="left"><span class="stage-badge" :class="'st-' + c.stage">{{ c.stage_label }}</span></td>
              <td class="left multiline">{{ c.summary || '—' }}</td>
              <td class="left tiny mono">
                <div v-for="(v, k) in c.metrics" :key="k">{{ k }} = {{ v }}</div>
                <span v-if="!Object.keys(c.metrics || {}).length" class="dim">—</span>
              </td>
              <td class="left tiny dim">{{ c.updated_at || '—' }}</td>
              <td class="left">
                <button class="btn-ghost btn-sm" :disabled="busy === c.id"
                        @click="changeStage(c.id, 'research')">升为研究中</button>
                <button class="btn-ghost btn-sm" :disabled="busy === c.id"
                        @click="changeStage(c.id, 'retired')">下线</button>
              </td>
            </tr>
          </tbody>
        </table>
        <p class="hint tiny mt" v-if="stageHint">{{ stageHint }}</p>
      </div>

      <!-- 风险研究是策略的辅助信息，放在策略主区与对比之后。 -->
      <VolatilityRiskPanel />

      <!-- ── 研究证据 ──────────────────────────────────────────────── -->
      <div class="panel">
        <h6>
          研究证据（<code>output/research/*/REPORT.md</code>）
          <span class="dim tiny">策略的来源出处；不是策略本身（不产生每日名单）</span>
        </h6>
        <ul class="evidence">
          <li v-for="e in pool.evidence.slice(0, 12)" :key="e.key">
            <span class="ev-title">{{ e.title }}</span>
            <span class="ev-path mono tiny dim">{{ e.path }}</span>
            <span class="ev-time tiny dim">{{ e.mtime }}</span>
          </li>
        </ul>
        <p class="hint tiny mt" v-if="pool.evidence.length > 12">
          只列最近 12 份，共 {{ pool.evidence.length }} 份。
        </p>
      </div>

      <div class="panel">
        <h6>口径说明</h6>
        <ul class="hint tiny">
          <li v-for="n in pool.notes || []" :key="n">{{ n }}</li>
          <li>「最新交易日」取自产物里的日期列 / 历史快照文件名，<b>不看文件 mtime</b>（否则重跑一次旧产物也会显示成今天）。</li>
        </ul>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { getStrategyPool, getStrategyRegistry, setStrategyStage } from '@/api/client'
import type { StrategyPoolEntry, StrategyPoolResponse, StrategyRegistryResponse } from '@/types/api'
import PageHeader from '@/components/ui/PageHeader.vue'
import StateBlock from '@/components/ui/StateBlock.vue'
import StrategyPerformance from '@/components/StrategyPerformance.vue'
import StrategyCurveCompare from '@/components/StrategyCurveCompare.vue'
import StrategyListsStatusBar from '@/components/StrategyListsStatusBar.vue'
import VolatilityRiskPanel from '@/components/VolatilityRiskPanel.vue'
import { annualRankOrder } from '@/components/strategyRanking'
import type { CompareSeries } from '@/components/StrategyCurveCompare.vue'
import type { StrategyListRefresh } from '@/types/api'
import { RULES, pctText, primaryCurve, shouldCollapse, strategyDetail, type StrategyDetail } from '@/components/strategyDetail'

const pool = ref<StrategyPoolResponse | null>(null)
const error = ref('')
const loading = ref(false)
const busy = ref('')
const stageHint = ref('')
const selectedId = ref('')
const query = ref('')
const filter = ref<'all' | 'production' | 'candidate' | 'positive' | 'negative'>('all')
const compareIds = ref<string[]>([])
const curveCollapsed = ref(false)
const registry = ref<StrategyRegistryResponse | null>(null)

/** 接线自检：有"意外的漏接线"（gaps）或声明层问题即不达标；按设计未接不算问题。 */
const registryOk = computed(() =>
  !registry.value || (!registry.value.coverage.gaps.length && !registry.value.problems.length))

const CONSUMER_LABEL: Record<string, string> = {
  selection_page: '选股结果页', selection_calendar: '选股日历', kline_watchlist: 'K 线自选',
  ths_export: '同花顺导出', ai_picks: '逐股 AI 点评', daily_refresh: '每日名单刷新',
  web_freshness: '新鲜度体检', strategy_pool: '策略池', curve: '中证500 对比曲线',
}

/** 当前选中策略的接线行（顺序固定，便于横向对比） */
const wiringRows = computed(() => {
  const wiring = current.value ? entryById(current.value.identity.id)?.wiring : null
  if (!wiring) return []
  return Object.entries(CONSUMER_LABEL)
    .filter(([key]) => key in wiring)
    .map(([key, label]) => ({ key, label, wired: Boolean(wiring[key]?.wired), how: String(wiring[key]?.how ?? '') }))
})

/** 公共显示项：一套函数给所有策略算出同一份结构（身份/新鲜度/曲线/配置/优劣） */
function detailOf(entry: StrategyPoolEntry): StrategyDetail {
  return strategyDetail(entry)
}
const details = computed<Record<string, StrategyDetail>>(() => {
  const out: Record<string, StrategyDetail> = {}
  for (const item of pool.value?.strategies || []) out[item.id] = detailOf(item)
  return out
})
function strategyDetailCollapsed(d: StrategyDetail): boolean {
  const curve = d.curve
  if (!curve.available || curve.observations < RULES.minObsForJudgement || !curve.negative) return false
  return curve.t === null || curve.t <= -RULES.significantT
}

const filterTabs = computed(() => {
  const items = pool.value?.strategies || []
  const counts = {
    all: items.length,
    production: items.filter((s) => s.stage === 'production').length,
    candidate: items.filter((s) => s.stage === 'candidate').length,
    positive: items.filter((s) => (details.value[s.id]?.curve.total ?? 0) > 0).length,
    negative: items.filter((s) => (details.value[s.id]?.curve.negative ?? false)).length,
  }
  return [
    { key: 'all' as const, label: '全部', count: counts.all },
    { key: 'production' as const, label: '生产', count: counts.production },
    { key: 'candidate' as const, label: '候选', count: counts.candidate },
    { key: 'positive' as const, label: '正期望', count: counts.positive },
    { key: 'negative' as const, label: '负期望', count: counts.negative },
  ]
})

// 与 K 线自选策略分组保持一致：年化主榜为默认。
const sortMode = ref<'annual' | 'return' | 'excess' | 'pool'>('annual')
const sortTabs = [
  { key: 'annual' as const, label: '年化收益排名' },
  { key: 'return' as const, label: '累计收益排名' },
  { key: 'excess' as const, label: '相对中证500' },
  { key: 'pool' as const, label: '池内顺序' },
]
const annualMode = computed(() => sortMode.value === 'annual')
const rankMode = computed(() => sortMode.value === 'return')
const excessMode = computed(() => sortMode.value === 'excess')
const rankBasis = computed(() => {
  if (sortMode.value === 'annual') return pool.value?.rankings?.basis || '年化收益降序（至少252个日收益）'
  if (sortMode.value === 'return') return '累计收益降序（各策略窗口不同）'
  if (sortMode.value === 'excess') return '相对中证500超额降序（各策略窗口不同）'
  return '登记表顺序'
})
const rankingsCaveat = computed(() => pool.value?.rankings?.caveat || '')

const designFilter = ref('all')
const versionFilter = ref('all')
const visible = computed(() => {
  const q = query.value.trim().toLowerCase()
  const rows = (pool.value?.strategies || []).filter((s) => {
    const d = details.value[s.id]
    if (designFilter.value !== 'all' && (s.factor_design?.decision_type || 'unknown') !== designFilter.value) return false
    if (versionFilter.value !== 'all' && s.factor_design?.implementation_version !== versionFilter.value) return false
    if (filter.value === 'production' && s.stage !== 'production') return false
    if (filter.value === 'candidate' && s.stage !== 'candidate') return false
    if (filter.value === 'positive' && !((d?.curve.total ?? 0) > 0)) return false
    if (filter.value === 'negative' && !(d?.curve.negative ?? false)) return false
    if (!q) return true
    return `${s.name} ${s.id} ${s.group}`.toLowerCase().includes(q)
  })
  if (sortMode.value === 'annual') {
    return [...rows].sort((a, b) => annualRankOrder(a.annual_rank?.rank, b.annual_rank?.rank, a.id, b.id))
  }
  if (sortMode.value === 'return') {
    return [...rows].sort((a, b) => (a.return_rank?.rank ?? 999) - (b.return_rank?.rank ?? 999))
  }
  if (sortMode.value === 'excess') {
    return [...rows].sort((a, b) => (a.excess_rank?.rank ?? 999) - (b.excess_rank?.rank ?? 999))
  }
  return rows
})

const current = computed(() => details.value[selectedId.value] || null)
const negativeCount = computed(() =>
  (pool.value?.strategies || []).filter((s) => details.value[s.id]?.curve.negative).length)

function entryById(id: string): StrategyPoolEntry | undefined {
  return (pool.value?.strategies || []).find((s) => s.id === id)
}

/** 这条策略今天的名单刷新结果（来自每日名单刷新契约；缺失返回 undefined） */
function refreshOf(id: string): StrategyListRefresh | null | undefined {
  return entryById(id)?.refresh
}

const REFRESH_LABEL: Record<string, string> = {
  updated: '已更新', already_fresh: '已是最新', no_signal: '当日无信号', stale: '落后',
  blocked: '依赖未就绪', timeout: '超时', failed: '失败', skipped: '按需（跳过）',
}
const REFRESH_ICON: Record<string, string> = {
  updated: '✅', already_fresh: '✅', no_signal: '➖', blocked: '⛔', stale: '⚠️', timeout: '⏱️',
  failed: '❌', skipped: '⏭️',
}
const REFRESH_TONE: Record<string, string> = {
  updated: 'ok', already_fresh: 'ok', no_signal: 'dim', blocked: 'bad', stale: 'warn', timeout: 'warn',
  failed: 'bad', skipped: 'dim',
}
const refreshLabel = (status: string) => REFRESH_LABEL[status] ?? status
const refreshIcon = (status: string) => REFRESH_ICON[status] ?? '•'
const refreshTone = (status: string) => REFRESH_TONE[status] ?? 'dim'

function select(id: string) {
  selectedId.value = id
  if (!compareIds.value.includes(id)) {
    compareIds.value = [...compareIds.value, id].slice(-4)
  }
}

function toggleCompare(id: string) {
  compareIds.value = compareIds.value.includes(id)
    ? compareIds.value.filter((x) => x !== id)
    : [...compareIds.value, id]
}

const curveEntries = computed(() =>
  (pool.value?.strategies || []).filter((s) => details.value[s.id]?.curve.available))

/** 多策略对比的数据：基准取任一曲线的 benchmark_nav（都是中证500 同窗） */
const compareSeries = computed<CompareSeries[]>(() => {
  const out: CompareSeries[] = []
  let benchmark: CompareSeries | null = null
  for (const s of curveEntries.value) {
    if (!compareIds.value.includes(s.id)) continue
    const curve = primaryCurve(s.performance)
    if (!curve?.nav?.length) continue
    out.push({ id: s.id, label: s.name, kind: curve.kind || '', dates: curve.dates || [], nav: curve.nav })
    if (!benchmark && curve.benchmark_nav?.length) {
      benchmark = { id: '__benchmark__', label: '中证500', kind: '', dates: curve.dates || [], nav: curve.benchmark_nav }
    }
  }
  return benchmark ? [benchmark, ...out] : out
})

const metricTiles = computed(() => {
  const c = current.value?.curve
  const f = current.value?.freshness
  const ok = (v: number | null, goodWhenPositive = true) =>
    v === null ? '' : (goodWhenPositive ? (v >= 0 ? 'up' : 'down') : (v >= 0 ? 'down' : 'up'))
  return [
    { label: '累计收益', value: pctText(c?.total ?? null), sub: c?.kindLabel || '无曲线', tone: ok(c?.total ?? null) },
    { label: '相对中证500', value: pctText(c?.excess ?? null), sub: '基准 000905.SH', tone: ok(c?.excess ?? null) },
    { label: '年化收益', value: pctText(c?.annual ?? null), sub: c && c.observations < RULES.minObsForConfidence ? '样本不足一年，仅参考' : '同期', tone: ok(c?.annual ?? null) },
    { label: '最大回撤', value: pctText(c?.maxDrawdown ?? null), sub: '越小越好', tone: ok(c?.maxDrawdown ?? null, false) },
    { label: '卡玛', value: c?.calmar === null || c?.calmar === undefined ? '—' : c.calmar.toFixed(3), sub: '年化 / |回撤|', tone: ok(c?.calmar ?? null) },
    { label: '夏普（无风险=0）', value: c?.sharpe === null || c?.sharpe === undefined ? '—' : c.sharpe.toFixed(3), sub: '日频年化', tone: ok(c?.sharpe ?? null) },
    { label: '日度 t', value: c?.t === null || c?.t === undefined ? '—' : c.t.toFixed(2), sub: `|t| ≥ ${RULES.significantT} 视为有方向性证据`, tone: ok(c?.t ?? null) },
    { label: '收益观测', value: c?.available ? String(c.observations) : '—', sub: c?.available ? `${c.startDate} ~ ${c.endDate}` : '无曲线', tone: '' },
    { label: '最新交易日', value: f?.latestDate || '—', sub: f?.lagDays ? `落后 ${f.lagDays} 天` : '与基准一致', tone: f?.fresh ? 'up' : 'down' },
    { label: '最新名单 / 快照', value: `${f?.nPicks ?? '—'} 只 / ${f?.nSnapshots ?? 0}`, sub: '名单条数 / 历史快照数', tone: '' },
  ]
})

function toneTag(tone: string): string {
  return tone === 'ok' ? 'ok' : tone === 'bad' ? 'bad' : tone === 'warn' ? 'warn' : 'dim'
}

function selectionLink(item?: StrategyPoolEntry): string {
  const source = item?.selection_source || ''
  return source ? `#/?source=${encodeURIComponent(source)}` : '#/'
}

async function changeStage(id: string, stage: string) {
  busy.value = id
  stageHint.value = ''
  try {
    const res = await setStrategyStage(id, stage)
    stageHint.value = res.hint || `已把 ${id} 切到 ${res.stage}`
    await reload()
  } catch (e) {
    stageHint.value = `切换失败：${e}`
  } finally {
    busy.value = ''
  }
}

async function reload() {
  loading.value = true
  try {
    pool.value = await getStrategyPool(true)
    error.value = ''
    const items = pool.value.strategies
    if (!selectedId.value || !items.some((s) => s.id === selectedId.value)) {
      // 默认详情与当前列表第一项一致，刷新时保留用户的已有选择。
      const ranked = visible.value
      selectedId.value = ranked[0]?.id || ''
    }
    if (!compareIds.value.length) {
      // 默认对比：**样本最长**的 3 条（否则生产策略只有 1~2 个观测，会把共同窗口压成两天）
      compareIds.value = items
        .map((s) => ({ id: s.id, obs: detailOf(s).curve.observations }))
        .filter((x) => x.obs > 1)
        .sort((a, b) => b.obs - a.obs)
        .slice(0, 3)
        .map((x) => x.id)
    }
  } catch (e) {
    error.value = String(e)
  } finally {
    loading.value = false
  }
}

/** 选中策略变化 → 负期望的默认折叠曲线（计算属性里不写状态，避免副作用告警） */
watch([selectedId, () => pool.value], () => {
  const entry = entryById(selectedId.value)
  curveCollapsed.value = entry ? strategyDetailCollapsed(detailOf(entry)) : false
}, { immediate: true })

async function loadRegistry() {
  try {
    registry.value = await getStrategyRegistry()
  } catch {
    registry.value = null
  }
}

onMounted(() => {
  loadRegistry()
  reload()
})
</script>

<style scoped>
/* 收益率排名的名次徽标 + 排序控件（2026-10-15） */
.side-sort {
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  padding: 4px 2px 6px;
}
.side-sort .tab { padding: 1px 6px; }
.rank-badge {
  display: inline-block; min-width: 2.2em; text-align: center;
  padding: 0 4px; border-radius: 4px; font-weight: 700;
  background: rgba(255, 255, 255, 0.08); color: #cbd5e1;
}
.rank-badge.gold { background: rgba(250, 204, 21, 0.22); color: #facc15; }
.rank-badge.silver { background: rgba(203, 213, 225, 0.22); color: #e2e8f0; }
.rank-badge.bronze { background: rgba(251, 146, 60, 0.22); color: #fb923c; }

.pool-layout { display: grid; grid-template-columns: 320px minmax(0, 1fr); gap: var(--sp-3); align-items: start; }
@media (max-width: 1100px) { .pool-layout { grid-template-columns: 1fr; } }
.pool-side { position: sticky; top: 64px; }
@media (max-width: 1100px) { .pool-side { position: static; } }
.side-panel { padding: var(--sp-2); }
.side-search { width: 100%; padding: 4px 8px; background: var(--bg-input);
  border: 1px solid var(--border-color); border-radius: var(--r-2); color: var(--text-primary);
  font-size: var(--fs-sm); margin: 6px 0; }
.side-tabs { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 6px; }
.strategy-list { list-style: none; margin: 0; padding: 0; max-height: 70vh; overflow-y: auto; }
.strategy-item { padding: 8px; border: 1px solid var(--border-color); border-radius: var(--r-2);
  margin-bottom: 6px; cursor: pointer; display: flex; flex-direction: column; gap: 3px;
  background: var(--bg-card); }
.strategy-item:hover { border-color: var(--accent-blue); }
.strategy-item.active { border-color: var(--accent-gold); background: rgba(255, 209, 102, 0.06); }
.strategy-item.negative { border-left: 3px solid var(--accent-down); }
.si-row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.si-name { font-weight: 600; font-size: var(--fs-sm); }
.si-metric { line-height: 1.5; }
.pool-main { display: flex; flex-direction: column; gap: var(--sp-3); }
.detail-head { padding: var(--sp-3); }
.dh-top { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.dh-top h5 { margin: 0; font-size: var(--fs-xl); }
.dh-why { margin-left: auto; }
.dh-summary { font-size: var(--fs-sm); color: var(--text-muted); line-height: 1.7; margin: 8px 0; }
.dh-meta { line-height: 1.6; }
.dh-actions { display: flex; gap: 6px; margin-top: 6px; }
.tile-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: var(--sp-2); }
.tile { display: flex; flex-direction: column; gap: 1px; padding: var(--sp-2);
  border: 1px solid var(--border-color); border-radius: var(--r-2); background: var(--bg-input); }
.tile label { font-size: 10px; color: var(--text-dim); letter-spacing: 0.04em; }
.tile b { font-size: var(--fs-lg); }
.verdict-grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-3); }
@media (max-width: 900px) { .verdict-grid { grid-template-columns: 1fr; } }
.vcol-head { font-weight: 600; margin-bottom: 4px; }
.vcol ul { list-style: none; margin: 0; padding: 0; }
.vcol li { display: flex; gap: 6px; align-items: baseline; padding: 3px 0; line-height: 1.5;
  border-bottom: 1px dashed var(--border-color); }
.collapsed-row { display: flex; gap: var(--sp-3); flex-wrap: wrap; align-items: baseline;
  padding: var(--sp-2); border: 1px dashed var(--border-color); border-radius: var(--r-2); }
.pick-row { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 6px; }
.pick-row .on { color: var(--accent-gold); border-color: var(--accent-gold); }
.kv-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: var(--sp-2); }
.kv { display: flex; flex-direction: column; gap: 2px; }
.kv label { font-size: 10px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.04em; }
.sc-code { display: block; font-size: var(--fs-xs); background: var(--bg-input);
  border: 1px solid var(--border-color); border-radius: var(--r-2); padding: 3px 6px;
  overflow-x: auto; white-space: nowrap; }
.stage-badge { padding: 1px 8px; border-radius: var(--r-pill); font-size: var(--fs-xs);
  border: 1px solid var(--border-color); color: var(--text-muted); }
.st-production { color: var(--accent-gold); border-color: rgba(255, 209, 102, 0.5); }
.st-research { color: var(--accent-blue); border-color: rgba(17, 138, 178, 0.5); }
.st-candidate { color: var(--accent-orange); border-color: rgba(255, 159, 28, 0.5); }
.st-retired { color: var(--text-dim); }
.evidence { list-style: none; margin: 0; padding: 0; }
.evidence li { display: flex; gap: var(--sp-2); align-items: baseline; padding: 2px 0; }
.ev-title { font-size: var(--fs-sm); }
.ev-path { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
