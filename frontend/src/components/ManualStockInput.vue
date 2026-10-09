<template>
  <div class="manual-input">
    <input
      v-model="code"
      placeholder="如 00700 / 600861 / 301230.SZ"
      maxlength="10"
      @keydown.enter="submit"
    />
    <button @click="submit">查询</button>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'

const emit = defineEmits<{ submit: [code: string] }>()
const code = ref('')

const SIX = /^(sh|sz|bj)?(\d{6})(\.(sh|sz|bj))?$/i

/** 6 位数字自动补交易所后缀（6→SH、0/3→SZ、4/8→BJ）；5 位数字仍按港股补零。 */
function normalize(raw: string): string {
  const val = raw.trim()
  const match = SIX.exec(val)
  if (match) {
    const digits = match[2]
    const suffix = (match[1] || match[4] || '').toUpperCase()
    if (suffix) return `${digits}.${suffix}`
    if (digits.startsWith('6')) return `${digits}.SH`
    if (digits.startsWith(('03'))) return `${digits}.SZ`
    if (digits.startsWith('4') || digits.startsWith('8')) return `${digits}.BJ`
    return `${digits}.SZ`
  }
  return val.padStart(5, '0')
}

function submit() {
  const val = code.value.trim()
  if (val) {
    emit('submit', normalize(val))
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
