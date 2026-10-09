<template>
  <header class="page-header">
    <div class="ph-main">
      <h4 class="page-title">{{ title }}</h4>
      <p v-if="description" class="hint" v-html="description"></p>
    </div>
    <div class="ph-actions"><slot name="actions" /></div>
  </header>
</template>

<script setup lang="ts">
defineProps<{ title: string; description?: string }>()
</script>

<style scoped>
.page-header { display: flex; align-items: flex-start; gap: 16px; margin-bottom: 12px; }
.ph-main { min-width: 0; }
.ph-main .hint { margin-bottom: 0; }
.ph-actions { margin-left: auto; display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
</style>
