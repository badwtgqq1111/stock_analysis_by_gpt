import { createRouter, createWebHashHistory } from 'vue-router'

const routes = [
  { path: '/', name: 'selection', component: () => import('@/pages/StockSelection.vue') },
  { path: '/factor-ic', name: 'factor-ic', component: () => import('@/pages/FactorIC.vue') },
  { path: '/kline', name: 'kline', component: () => import('@/pages/KlinePage.vue') },
  // 旧「组合回测」页读的是港股遗留数据源（lightgbm_top10_*.csv），只 5 个指标，
  // 与「回测绩效」口径冲突 → 从导航下线，老书签重定向过去，避免继续误导。
  { path: '/portfolio', redirect: '/performance' },
  { path: '/factor-mining', name: 'factor-mining', component: () => import('@/pages/FactorMining.vue') },
  { path: '/pipeline', name: 'pipeline', component: () => import('@/pages/Pipeline.vue') },
  { path: '/calendar', name: 'calendar', component: () => import('@/pages/SelectionCalendar.vue') },
  { path: '/automation', name: 'automation', component: () => import('@/pages/Automation.vue') },
  { path: '/performance', name: 'performance', component: () => import('@/pages/Performance.vue') },
  { path: '/paper', name: 'paper', component: () => import('@/pages/PaperTrading.vue') },
  { path: '/attribution', name: 'attribution', component: () => import('@/pages/Attribution.vue') },
  // 行情（2026-10-09 新增）：每日/周 涨跌与资金流排行、连板梯队、当日分时实时
  { path: '/rankings', name: 'rankings', component: () => import('@/pages/Rankings.vue') },
  { path: '/limitup', name: 'limitup', component: () => import('@/pages/LimitUp.vue') },
  { path: '/intraday', name: 'intraday', component: () => import('@/pages/Intraday.vue') },
]

export default createRouter({
  history: createWebHashHistory(),
  routes,
})
