import { createRouter, createWebHashHistory } from 'vue-router'

const routes = [
  { path: '/factors', name: 'factors', component: () => import('@/pages/FactorPool.vue') },
  { path: '/', name: 'selection', component: () => import('@/pages/StockSelection.vue') },
  { path: '/factor-ic', name: 'factor-ic', component: () => import('@/pages/FactorIC.vue') },
  { path: '/kline', name: 'kline', component: () => import('@/pages/KlinePage.vue') },
  // 旧「组合回测」页读的是港股遗留数据源（lightgbm_top10_*.csv），只 5 个指标，
  // 与「回测绩效」口径冲突 → 从导航下线，老书签重定向过去，避免继续误导。
  { path: '/portfolio', redirect: '/performance' },
  // 因子挖掘已并入「投研执行台」（2026-10-11）：挖掘会话 + 因子库都在 `/agent?tab=mining`。
  // 老书签重定向过去，避免 404（与 `/portfolio` → `/performance` 同一处理）。
  { path: '/factor-mining', redirect: '/agent?tab=mining' },
  // 策略池（2026-10-10）：模型选股 / arm_select / 行业轮动 的登记处，也是新策略的入口
  { path: '/strategies', name: 'strategies', component: () => import('@/pages/StrategyPool.vue') },
  { path: '/pipeline', name: 'pipeline', component: () => import('@/pages/Pipeline.vue') },
  { path: '/calendar', name: 'calendar', component: () => import('@/pages/SelectionCalendar.vue') },
  { path: '/automation', name: 'automation', component: () => import('@/pages/Automation.vue') },
  { path: '/performance', name: 'performance', component: () => import('@/pages/Performance.vue') },
  { path: '/paper', name: 'paper', component: () => import('@/pages/PaperTrading.vue') },
  // 实盘镜像（2026-10-12）：真实持仓的 实时分时/日K + 日周月 vs 中证500 + 收益日历
  { path: '/live', name: 'live', component: () => import('@/pages/LiveAccount.vue') },
  { path: '/attribution', name: 'attribution', component: () => import('@/pages/Attribution.vue') },
  // 行情（2026-10-09 新增）：每日/周 涨跌与资金流排行、连板梯队、当日分时实时
  { path: '/rankings', name: 'rankings', component: () => import('@/pages/Rankings.vue') },
  { path: '/limitup', name: 'limitup', component: () => import('@/pages/LimitUp.vue') },
  { path: '/intraday', name: 'intraday', component: () => import('@/pages/Intraday.vue') },
  { path: '/industry', name: 'industry', component: () => import('@/pages/IndustryHeat.vue') },
  // 投研执行台（2026-10-11）：两族/四层状态 + 运行轨迹 + 可编辑的提示词（默认角色「量化领域专家」）
  { path: '/agent', name: 'agent', component: () => import('@/pages/AgentConsole.vue') },
]

export default createRouter({
  history: createWebHashHistory(),
  routes,
})
