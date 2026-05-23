import { createRouter, createWebHashHistory } from 'vue-router'

const routes = [
  { path: '/', name: 'selection', component: () => import('@/pages/StockSelection.vue') },
  { path: '/factor-ic', name: 'factor-ic', component: () => import('@/pages/FactorIC.vue') },
  { path: '/kline', name: 'kline', component: () => import('@/pages/KlinePage.vue') },
  { path: '/portfolio', name: 'portfolio', component: () => import('@/pages/Portfolio.vue') },
]

export default createRouter({
  history: createWebHashHistory(),
  routes,
})
