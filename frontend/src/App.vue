<template>
  <div class="app-layout">
    <nav>
      <div class="nav-brand">港股量化分析系统</div>
      <div class="nav-links">
        <router-link v-for="l in links" :key="l.to" :to="l.to" active-class="active">
          {{ l.label }}
        </router-link>
      </div>
    </nav>
    <main>
      <router-view />
    </main>
  </div>
</template>

<script setup lang="ts">
const links = [
  { to: '/', label: '选股结果' },
  { to: '/factor-ic', label: '因子IC分析' },
  { to: '/kline', label: 'K线图表' },
  { to: '/portfolio', label: '组合回测' },
]
</script>

<style scoped>
.app-layout {
  min-height: 100vh;
}
nav {
  display: flex;
  align-items: center;
  gap: 24px;
  padding: 10px 20px;
  background: var(--bg-card);
  border-bottom: 1px solid var(--border-color);
  position: sticky;
  top: 0;
  z-index: 10;
  margin: -16px -20px 16px -20px;
}
.nav-brand {
  font-size: 15px;
  font-weight: 700;
  color: var(--text-primary);
}
.nav-links {
  display: flex;
  gap: 4px;
}
.nav-links a {
  padding: 6px 14px;
  border-radius: 4px;
  color: var(--text-muted);
  text-decoration: none;
  font-size: 13px;
  transition: all 0.15s;
}
.nav-links a:hover {
  color: var(--text-primary);
  background: rgba(255,255,255,0.05);
}
.nav-links a.active {
  color: var(--accent-gold);
  background: rgba(255,209,102,0.1);
}
main {
  max-width: 1400px;
  margin: 0 auto;
}
</style>
