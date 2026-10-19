<template>
  <div class="app-layout">
    <nav class="app-nav">
      <div class="nav-brand">
        <span class="brand-mark">Q</span>
        <span class="brand-text">A 股量化研究系统</span>
      </div>

      <div class="nav-groups">
        <div v-for="g in navGroups" :key="g.label" class="nav-group">
          <span class="nav-group-label">{{ g.label }}</span>
          <div class="nav-links">
            <router-link v-for="l in g.links" :key="l.to" :to="l.to" active-class="active">
              {{ l.label }}
            </router-link>
          </div>
        </div>
      </div>

      <div class="nav-status" :title="healthTitle">
        <span class="dot" :class="healthClass" />
        <span class="tiny">{{ healthText }}</span>
        <!-- 构建戳：新增功能"前端看不到"时，第一件事是确认浏览器跑的是哪一版构建 -->
        <span class="tiny dim build-stamp" v-if="buildStamp">build {{ buildStamp }}</span>
      </div>
    </nav>

    <!-- 构建产物自检失败时会在 /health 里带出来；这正是 2026-10-08 白屏事故的告警位 -->
    <div v-if="frontendWarning" class="global-banner">
      <b>前端资源自检失败：</b>入口 index.html 引用的 {{ health?.frontend?.missing?.join('、') }} 不存在。
      浏览器会白屏。修复：<code>cd frontend &amp;&amp; bun run build</code>，然后硬刷新（Cmd+Shift+R）。
    </div>

    <main class="app-main">
      <router-view v-slot="{ Component }">
        <Suspense>
          <component :is="Component" />
          <template #fallback>
            <div class="page"><div class="state-block"><div class="spinner" /><div>加载页面…</div></div></div>
          </template>
        </Suspense>
      </router-view>
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { getHealth, type HealthResponse } from '@/api/health'

// 导航按**工作流**分组，而不是平铺 7 个入口：
// 用户每天的动作是「看今天的票 → 回溯历史 → 查图表 → 跑流水线」，
// 分组后一眼能看出每类页面属于哪一步。
const navGroups = [
  {
    label: '选股',
    links: [
      { to: '/', label: '选股结果' },
      { to: '/calendar', label: '选股日历' },
    ],
  },
  {
    label: '研究',
    links: [
      { to: '/agent', label: '投研执行台' },
      { to: '/strategies', label: '策略池' },
      { to: '/factors', label: '因子池' },
      { to: '/factor-ic', label: '因子 IC' },
      { to: '/attribution', label: '选股归因' },
    ],
  },
  {
    label: '数据',
    links: [
      { to: '/pipeline', label: '数据流水线' },
      { to: '/automation', label: '自动化进度' },
    ],
  },
  {
    label: '图表 / 回测',
    links: [
      { to: '/kline', label: 'K 线图表' },
      { to: '/performance', label: '回测绩效' },
      { to: '/paper', label: '模拟盘' },
      { to: '/live', label: '实盘镜像' },
    ],
  },
  // 行情：每天盯盘要看的三个入口（谁在涨跌/谁在吸金 → 连板情绪 → 个股分时）
  {
    label: '行情',
    links: [
      { to: '/market', label: '大盘行情' },
      { to: '/rankings', label: '涨跌/资金排行' },
      { to: '/industry', label: '行业热度' },
      { to: '/limitup', label: '连板统计' },
      { to: '/intraday', label: '分时行情' },
    ],
  },
]

const health = ref<HealthResponse | null>(null)
const healthErr = ref('')

onMounted(async () => {
  try {
    health.value = await getHealth()
  } catch (e) {
    healthErr.value = String(e)
  }
})

const frontendWarning = computed(() => Boolean(health.value?.frontend && !health.value.frontend.ok))
const healthClass = computed(() => (healthErr.value ? 'bad' : frontendWarning.value ? 'warn' : 'ok'))
const healthText = computed(() => (healthErr.value ? '后端离线' : frontendWarning.value ? '前端资源缺失' : '服务正常'))
const buildStamp = computed(() => {
  const built = health.value?.frontend?.built_at
  const entry = health.value?.frontend?.entry || ''
  const hash = entry ? entry.replace(/^.*index-/, '').replace(/\.js$/, '') : ''
  return built ? `${built.slice(5, 16)}${hash ? ` · ${hash.slice(0, 8)}` : ''}` : ''
})
const healthTitle = computed(() =>
  healthErr.value
    ? `GET /health 失败：${healthErr.value}`
    : `dist=${health.value?.frontend?.dist ?? '—'}｜构建 ${health.value?.frontend?.built_at ?? '—'}` +
      `｜入口 ${health.value?.frontend?.entry ?? '—'}｜引用 ${health.value?.frontend?.refs?.length ?? 0} 个资源` +
      `｜缺失 ${health.value?.frontend?.missing?.length ?? 0} 个`,
)
</script>

<style scoped>
.app-layout { min-height: 100vh; display: flex; flex-direction: column; }

.app-nav {
  position: sticky; top: 0; z-index: var(--z-nav);
  display: flex; align-items: center; gap: var(--sp-5);
  padding: var(--sp-2) var(--sp-5);
  background: rgba(17, 22, 29, 0.94);
  backdrop-filter: blur(8px);
  border-bottom: 1px solid var(--border-color);
  flex-wrap: wrap;
}
.nav-brand { display: flex; align-items: center; gap: var(--sp-2); flex-shrink: 0; }
.brand-mark {
  width: 22px; height: 22px; border-radius: var(--r-2);
  background: var(--accent-gold); color: #23262e;
  display: grid; place-items: center; font-weight: 800; font-size: var(--fs-sm);
}
.brand-text { font-size: var(--fs-lg); font-weight: 700; }

.nav-groups { display: flex; gap: var(--sp-5); flex-wrap: wrap; align-items: center; }
.nav-group { display: flex; align-items: center; gap: var(--sp-2); }
.nav-group-label {
  font-size: var(--fs-xs); color: var(--text-dim);
  text-transform: none; letter-spacing: 0.4px;
}
.nav-links { display: flex; gap: 2px; }
.nav-links a {
  padding: 5px 12px; border-radius: var(--r-2); color: var(--text-muted);
  text-decoration: none; font-size: var(--fs-md); transition: all 0.15s; white-space: nowrap;
}
.nav-links a:hover { color: var(--text-primary); background: rgba(255, 255, 255, 0.05); }
.nav-links a.active { color: var(--accent-gold); background: rgba(255, 209, 102, 0.1); }

.nav-status { margin-left: auto; display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
.dot { width: 7px; height: 7px; border-radius: 50%; }
.dot.ok { background: var(--state-ok); }
.dot.warn { background: var(--state-warn); }
.dot.bad { background: var(--state-bad); }

.global-banner {
  max-width: var(--content-max); width: 100%; margin: var(--sp-3) auto 0;
  padding: var(--sp-2) var(--sp-4);
  background: rgba(239, 71, 111, 0.1); border: 1px solid var(--state-bad);
  border-radius: var(--r-3); color: var(--text-primary); font-size: var(--fs-md);
}

.app-main { flex: 1; padding: var(--sp-4) var(--sp-5) var(--sp-6); }

.build-stamp { font-family: var(--font-mono); }

@media (max-width: 1100px) {
  .app-nav { gap: var(--sp-3); padding: var(--sp-2) var(--sp-3); }
  .build-stamp { display: none; }
  .nav-group-label { display: none; }
  .app-main { padding: var(--sp-3); }
}

/* 手机横屏（宽 ~844 / 高 ~390）：横屏宽度超过 720px 命中不了上面的手机断点，
   而高度只有 390px —— 实测导航换行会占掉 81px（21% 屏高）。
   注意：这些规则必须在**本文件的 scoped 块里**，不能写进 main.css ——
   `.app-nav` 的 `flex-wrap: wrap` 就在下面 scoped 块里，选择器带 [data-v-xxx] 优先级更高，
   写在全局会被压过去（第一版就是这么失效的）。 */
@media (max-height: 500px) and (max-width: 1100px) {
  .app-nav {
    flex-wrap: nowrap;
    align-items: center;
    gap: var(--sp-2);
    padding: var(--sp-1) 0;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    scrollbar-width: none;
  }
  .app-nav::-webkit-scrollbar { display: none; }
  .nav-brand { padding-left: var(--sp-3); }
  .nav-groups { flex-wrap: nowrap; gap: var(--sp-2); }
  .nav-group { flex: 0 0 auto; }
  .nav-links { flex-wrap: nowrap; }
  .nav-links a { padding: 4px 9px; font-size: var(--fs-xs); }
  .nav-status { flex: 0 0 auto; padding-right: var(--sp-3); }
}

/* 手机竖屏：导航从"会换行成好几行"改成"一行横向滚动"——8 个入口在 375px 上换行会占掉半屏 */
@media (max-width: 720px) {
  .app-nav {
    flex-wrap: nowrap;
    align-items: center;
    gap: var(--sp-3);
    padding: var(--sp-2) 0;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    scrollbar-width: none;
  }
  .app-nav::-webkit-scrollbar { display: none; }
  .nav-brand { padding-left: var(--sp-3); }
  .nav-groups { flex-wrap: nowrap; gap: var(--sp-3); }
  .nav-group { flex: 0 0 auto; }
  .nav-links { flex-wrap: nowrap; }
  .nav-links a { padding: 7px 10px; font-size: var(--fs-sm); }
  .nav-status { flex: 0 0 auto; padding-right: var(--sp-3); }
}
</style>
