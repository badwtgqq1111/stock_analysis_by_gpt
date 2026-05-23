<template>
  <div class="time-range-selector">
    <button
      v-for="opt in options"
      :key="opt.value"
      :class="['btn', { active: modelValue === opt.value }]"
      @click="$emit('update:modelValue', opt.value)"
    >
      {{ opt.label }}
    </button>
  </div>
</template>

<script setup lang="ts">
defineProps<{ modelValue: number }>()
defineEmits<{ 'update:modelValue': [value: number] }>()

const options = [
  { label: '90天', value: 90 },
  { label: '180天', value: 180 },
  { label: '365天', value: 365 },
  { label: '730天', value: 730 },
  { label: '全部', value: 9999 },
]
</script>

<style scoped>
.time-range-selector {
  display: flex;
  gap: 0;
}
.btn {
  padding: 5px 12px;
  border: 1px solid var(--border-color);
  background: var(--bg-input);
  color: var(--text-muted);
  cursor: pointer;
  transition: all 0.15s;
}
.btn:first-child { border-radius: 4px 0 0 4px; }
.btn:last-child { border-radius: 0 4px 4px 0; }
.btn + .btn { border-left: none; }
.btn:hover { color: var(--text-primary); background: #252a33; }
.btn.active {
  background: var(--accent-blue);
  border-color: var(--accent-blue);
  color: #fff;
}
</style>
