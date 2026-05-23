<template>
  <div class="manual-input">
    <input
      v-model="code"
      placeholder="如: 00700"
      maxlength="5"
      @keydown.enter="submit"
    />
    <button @click="submit">查询</button>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'

const emit = defineEmits<{ submit: [code: string] }>()
const code = ref('')

function submit() {
  const val = code.value.trim()
  if (val) {
    emit('submit', val.padStart(5, '0'))
    code.value = ''
  }
}
</script>

<style scoped>
.manual-input {
  display: flex;
  gap: 4px;
}
.manual-input input {
  flex: 1;
  padding: 6px 10px;
  background: var(--bg-input);
  border: 1px solid var(--border-color);
  border-radius: 4px;
  color: var(--text-primary);
  outline: none;
  min-width: 0;
}
.manual-input input:focus {
  border-color: var(--accent-blue);
}
.manual-input button {
  padding: 6px 12px;
  background: var(--accent-blue);
  color: #fff;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  white-space: nowrap;
  font-size: 13px;
}
.manual-input button:hover {
  opacity: 0.9;
}
</style>
