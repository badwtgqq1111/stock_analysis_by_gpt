<template>
  <div class="overlay-toggles">
    <label class="toggle" v-for="opt in options" :key="opt.value">
      <input
        type="checkbox"
        :checked="modelValue.includes(opt.value)"
        @change="toggle(opt.value)"
      />
      <span class="toggle-track"></span>
      <span class="toggle-label">{{ opt.label }}</span>
    </label>
  </div>
</template>

<script setup lang="ts">
const props = defineProps<{ modelValue: string[] }>()
const emit = defineEmits<{ 'update:modelValue': [value: string[]] }>()

const options = [
  { label: 'LightGBM 信号', value: 'signals' },
  { label: '筹码分布', value: 'chips' },
]

function toggle(val: string) {
  if (props.modelValue.includes(val)) {
    emit('update:modelValue', props.modelValue.filter(v => v !== val))
  } else {
    emit('update:modelValue', [...props.modelValue, val])
  }
}
</script>

<style scoped>
.overlay-toggles {
  display: flex;
  gap: 16px;
  align-items: center;
}
.toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  font-size: 13px;
  color: var(--text-muted);
  user-select: none;
}
.toggle input { display: none; }
.toggle-track {
  width: 32px;
  height: 18px;
  border-radius: 9px;
  background: #3a3f4b;
  position: relative;
  transition: background 0.2s;
}
.toggle-track::after {
  content: '';
  position: absolute;
  top: 2px;
  left: 2px;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: #fff;
  transition: transform 0.2s;
}
.toggle input:checked + .toggle-track {
  background: var(--accent-green);
}
.toggle input:checked + .toggle-track::after {
  transform: translateX(14px);
}
</style>
