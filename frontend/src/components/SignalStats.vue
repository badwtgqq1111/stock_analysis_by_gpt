<template>
  <div class="signal-stats" v-if="stats">
    <span class="label">LightGBM 信号回测:</span>
    <span class="stats-text">
      买入 {{ stats.buy_count }} 次 | 卖出 {{ stats.sell_count }} 次 |
      <template v-if="stats.paired_trades > 0">
        配对 {{ stats.paired_trades }} 笔 |
        平均收益 {{ stats.avg_return }}% |
        胜率 {{ stats.win_rate }}% |
        最大盈利 {{ stats.max_win }}% |
        最大亏损 {{ stats.max_loss }}%
      </template>
      <template v-else>
        暂无完整配对交易
      </template>
    </span>
  </div>
</template>

<script setup lang="ts">
import type { SignalStats } from '@/types/api'

defineProps<{ stats: SignalStats | null }>()
</script>

<style scoped>
.signal-stats {
  padding: 8px 14px;
  border-radius: 6px;
  background: rgba(17, 157, 164, 0.12);
  border: 1px solid rgba(17, 157, 164, 0.25);
  font-size: 12px;
  line-height: 1.6;
}
.label {
  font-weight: 600;
  margin-right: 4px;
}
.stats-text {
  color: var(--text-muted);
}
</style>
