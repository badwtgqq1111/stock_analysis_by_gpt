<template>
  <div class="prompt-grid">
    <!-- 左：人设与操作 -->
    <section class="panel">
      <h6>
        人设
        <span class="pill tiny" :class="profile?.is_default ? 'dim' : 'ok'">
          {{ profile?.is_default ? '默认' : '已自定义' }}
        </span>
        <span class="dim tiny">{{ payload?.path }}</span>
      </h6>
      <label class="field">
        <span class="lbl">角色（默认「量化领域专家」，可改）</span>
        <input v-model="form.role" class="input" spellcheck="false" />
      </label>
      <label class="field">
        <span class="lbl">详细程度</span>
        <input v-model="form.detail_level" class="input" spellcheck="false" />
      </label>
      <div class="prompt-actions">
        <button class="btn-primary" :disabled="!dirty || saving" @click="save">保存</button>
        <button class="btn-ghost" :disabled="saving" @click="resetProfile">恢复默认</button>
        <button class="btn-ghost" @click="copyPrompt">复制</button>
        <span v-if="saving" class="dim tiny">保存中…</span>
        <span v-else-if="dirty" class="warn-text tiny">未保存</span>
        <span v-else class="dim tiny">已保存 {{ profile?.updated_at || '—' }}</span>
      </div>
      <p v-if="message" class="hint tiny">{{ message }}</p>
      <dl class="kv tiny">
        <dt>落盘</dt><dd><code>config/agent_profile.json</code></dd>
        <dt>历史版本</dt><dd>{{ payload?.history_count ?? 0 }} 份（<code>output/agent_runs/profile_history/</code>）</dd>
        <dt>恢复默认</dt><dd><code>POST /api/agent/profile/reset</code></dd>
      </dl>
      <p class="hint tiny">
        这段提示词就是 agent 的人设：默认把服务对象写成「量化领域专家」——结论先行、按族给证据
        （族 A 看单笔净期望、族 B 看秩 IC/BH-FDR）、多重检验优先、短样本不下结论、保命规则默认不采信、不给下单指令。
        改完立刻生效；改坏了可以「恢复默认」或用历史版本里的任意一份。
      </p>
      <ul class="hint tiny">
        <li>族 A（规则类策略）：证据单位是「一笔」</li>
        <li>族 B（因子流水线）：证据单位是「一个截面/一个组合」</li>
        <li>唯一共用判据：跟它自己的对照做同窗口逐期配对</li>
      </ul>
    </section>

    <!-- 右：提示词正文（大编辑区） -->
    <section class="panel">
      <h6>
        系统提示词
        <span class="pill dim tiny">{{ form.system_prompt.length }} 字</span>
        <span class="dim tiny">保存即写入上面的文件；旧版本自动备份</span>
      </h6>
      <textarea v-model="form.system_prompt" class="input prompt" spellcheck="false" />
      <p class="hint tiny">
        建议保留的四条骨架：① 按族给证据 ② 多重检验优先（写清累计试验次数与 BH-FDR）
        ③ 短样本不下结论（&lt;30 标样本不足、&lt;252 不参与年化排名）④ 不给下单指令（决策层停在模拟盘 + 自选导出）。
      </p>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { getAgentProfile, resetAgentProfile, saveAgentProfile } from '@/api/client'
import type { AgentProfile, AgentProfileResponse } from '@/types/api'

const payload = ref<AgentProfileResponse | null>(null)
const profile = computed<AgentProfile | null>(() => payload.value?.profile || null)
const form = ref({ role: '', detail_level: '', system_prompt: '' })
const edited = ref(false)
const saving = ref(false)
const message = ref('')

const dirty = computed(() => {
  const saved = profile.value
  if (!saved) return false
  return form.value.role !== saved.role
    || form.value.detail_level !== saved.detail_level
    || form.value.system_prompt !== saved.system_prompt
})

function syncForm(saved: AgentProfile) {
  form.value = { role: saved.role, detail_level: saved.detail_level, system_prompt: saved.system_prompt }
  edited.value = false
}

async function load() {
  try {
    const data = await getAgentProfile()
    payload.value = data
    // 只在用户没动过表单时才覆盖输入（否则边改边被刷新的取数冲掉）
    if (!edited.value) syncForm(data.profile)
  } catch (err) {
    message.value = `取人设失败：${err}`
  }
}

async function save() {
  saving.value = true
  message.value = ''
  try {
    const resp = await saveAgentProfile({
      role: form.value.role,
      detail_level: form.value.detail_level,
      system_prompt: form.value.system_prompt,
    })
    payload.value = { ...(payload.value as AgentProfileResponse), profile: resp.profile, history_count: resp.history_count }
    syncForm(resp.profile)
    message.value = `已保存（${resp.profile.updated_at}），旧版本已备份，共 ${resp.history_count} 份`
  } catch (err) {
    message.value = `保存失败：${err}`
  } finally {
    saving.value = false
  }
}

async function resetProfile() {
  saving.value = true
  message.value = ''
  try {
    const resp = await resetAgentProfile()
    payload.value = { ...(payload.value as AgentProfileResponse), profile: resp.profile, history_count: resp.history_count }
    syncForm(resp.profile)
    message.value = '已恢复默认（角色：量化领域专家）'
  } catch (err) {
    message.value = `恢复失败：${err}`
  } finally {
    saving.value = false
  }
}

async function copyPrompt() {
  try {
    await navigator.clipboard.writeText(form.value.system_prompt)
    message.value = '提示词已复制到剪贴板'
  } catch {
    message.value = '复制失败（浏览器拒绝了剪贴板权限）'
  }
}

// `flush: 'sync'`：赋值同步置位 edited，syncForm 末尾再还原（顺序不能反）
import { watch } from 'vue'
watch(form, () => { edited.value = true }, { deep: true, flush: 'sync' })

onMounted(load)
defineExpose({ reload: load })
</script>

<style scoped>
.prompt-grid {
  display: grid;
  grid-template-columns: 380px minmax(0, 1fr);
  gap: var(--sp-3);
  align-items: start;
}
.field { display: block; margin-bottom: var(--sp-2); }
.lbl { display: block; font-size: var(--fs-xs); color: var(--text-muted); margin-bottom: 4px; }
.input {
  width: 100%; padding: 6px 8px; background: var(--bg-input); color: var(--text-primary);
  border: 1px solid var(--border-color); border-radius: var(--r-2); font-size: var(--fs-sm);
}
.input:focus { outline: none; border-color: var(--accent-gold); }
.prompt { font-family: var(--font-mono); line-height: 1.6; min-height: 520px; resize: vertical; }
.prompt-actions { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; margin: var(--sp-2) 0; }
@media (max-width: 1180px) {
  .prompt-grid { grid-template-columns: minmax(0, 1fr); }
  .prompt { min-height: 320px; }
}
</style>
