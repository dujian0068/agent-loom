<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Bot, Plus } from 'lucide-vue-next'
import { api } from '../api'
import { state, canEdit } from '../state'
import type { Agent } from '../types'
const router = useRouter(), agents = ref<Agent[]>([]), search = ref(''), loading = ref(true)
const error = ref(''), creating = ref(false), busy = ref(false)
const name = ref(''), description = ref(''), mode = ref('')
const filtered = computed(() => agents.value.filter(a => (a.name + a.description).includes(search.value)))
async function load() {
  try { agents.value = (await api<{items: Agent[]}>(`/spaces/${state.spaceId}/agents`)).items }
  catch (e) { error.value = (e as Error).message } finally { loading.value = false }
}
async function create() {
  if (busy.value) return
  if (!name.value.trim()) { error.value = '请填写应用名称'; return }
  if (!['react', 'plan_executor'].includes(mode.value)) { error.value = '请选择 ReAct 或 Plan 执行模式'; return }
  busy.value = true; error.value = ''
  try {
    const agent = await api<Agent>(`/spaces/${state.spaceId}/agents`, 'POST', { name: name.value, description: description.value, mode: mode.value })
    creating.value = false; await router.push(`/apps/${agent.id}/config`)
  } catch (e) { error.value = (e as Error).message } finally { busy.value = false }
}
async function useAgent(agent: Agent) {
  busy.value = true
  try {
    const session = await api<{id: string}>(`/agents/${agent.id}/sessions`, 'POST')
    await router.push(`/sessions/${session.id}`)
  } catch (e) { error.value = (e as Error).message } finally { busy.value = false }
}
onMounted(load)
</script>
<template>
  <div class="page-head"><div><h1>团队应用</h1><p class="muted">配置、发布并使用团队的 Agent，每位成员的会话独立保存。</p></div>
    <button v-if="canEdit" class="primary" @click="creating = true"><Plus :size="17" />新建应用</button></div>
  <p v-if="error && !creating" class="error" role="alert">{{ error }}</p>
  <div class="toolbar"><input v-model="search" type="search" placeholder="搜索应用名称或描述" aria-label="搜索应用"><span class="muted">{{ agents.length }} 个应用 · 空间共享</span></div>
  <p v-if="loading" class="muted">正在读取应用…</p>
  <div v-else-if="!filtered.length" class="empty"><Bot :size="32" /><h2>{{ search ? '没有匹配的应用' : '创建团队的第一个 Agent' }}</h2>
    <p class="muted">{{ canEdit ? '先准备模型和资源，再配置并发布应用。' : '管理员发布应用后，会出现在这里。' }}</p></div>
  <div v-else class="app-grid">
    <article v-for="agent in filtered" :key="agent.id" class="app-card" :aria-label="agent.name">
      <div class="row spread"><span class="app-icon"><Bot :size="23" /></span><span class="badge">{{ agent.lifecycle === 'publishing' ? '发布处理中' : agent.release_version ? `已发布 v${agent.release_version}` : '未发布' }}</span></div>
      <h2>{{ agent.name }}</h2><p class="description muted">{{ agent.description || '暂无描述' }}</p>
      <div class="row"><span class="badge">{{ agent.mode === 'react' ? 'ReAct' : 'Plan' }}</span><span class="muted">草稿 r{{ agent.revision }}</span></div>
      <footer class="row spread"><RouterLink v-if="canEdit" :to="`/apps/${agent.id}/config`" class="button">配置</RouterLink>
        <button class="primary" :disabled="!agent.release_id || agent.lifecycle !== 'active' || busy" @click="useAgent(agent)">使用</button></footer>
    </article>
  </div>
  <div v-if="creating" class="modal-backdrop" @click.self="!busy && (creating = false)">
    <form class="modal" role="dialog" aria-modal="true" aria-labelledby="create-title" @submit.prevent="create">
      <h2 id="create-title">新建应用</h2>
      <label>应用名称<input v-model="name" required maxlength="100" autofocus placeholder="例如：研发周报助手"></label>
      <fieldset><legend>执行模式（必选）</legend><div class="mode-grid">
        <label class="choice"><input v-model="mode" type="radio" name="mode" value="react" required><span><strong>ReAct</strong><small>主 Agent 自主调用所选工具完成任务。</small></span></label>
        <label class="choice"><input v-model="mode" type="radio" name="mode" value="plan_executor" required><span><strong>Plan</strong><small>Planner 拆分任务，专业执行器协作完成。</small></span></label>
      </div><small class="muted">创建后执行模式不可更改。</small></fieldset>
      <label>应用描述（选填）<textarea v-model="description" maxlength="2000" rows="3" placeholder="说明应用的用途与适用场景"></textarea></label>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <div class="row end"><button type="button" :disabled="busy" @click="creating = false">取消</button><button class="primary" :disabled="busy">{{ busy ? '创建中…' : '创建应用' }}</button></div>
    </form>
  </div>
</template>
