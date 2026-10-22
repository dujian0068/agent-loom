<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Send, Square, MessageSquare } from 'lucide-vue-next'
import { api, apiBase } from '../api'
import { state } from '../state'
import { terminal, type History } from '../types'
const route = useRoute(), router = useRouter(), sessions = ref<{ id: string; title: string; app_name: string }[]>([])
const history = ref<History>(), content = ref(''), busy = ref(false), error = ref(''), connection = ref('')
const progress = ref<{ id: number; kind: string; value: any }[]>([])
const currentProgress = computed(() => {
  const latest = new Map<string, typeof progress.value[number]>()
  for (const item of progress.value) {
    const key = item.kind === 'tool' ? `tool:${item.value.call_id}` : item.kind === 'todos' ? `todos:${item.value.role}` : item.kind
    latest.set(key, item)
  }
  return [...latest.values()].sort((a, b) => a.id - b.id)
})
const run = computed(() => history.value?.runs.at(-1))
const active = computed(() => !terminal(run.value))
const labels: Record<string, string> = { queued: '排队中', running: '执行中', stopping: '停止处理中', completed: '已完成', failed: '执行失败', cancelled: '已停止' }
let source: EventSource | undefined, connectedRun = '', lastMessage = { id: '', content: '' }
async function loadSessions() {
  sessions.value = (await api<{items: typeof sessions.value}>(`/spaces/${state.spaceId}/sessions`)).items
}
async function loadHistory() {
  if (!route.params.id) return
  const result = await api<History>(`/sessions/${route.params.id}/history`)
  if (result.id !== route.params.id) return
  history.value = result
  if (run.value) connect(run.value.id)
  else { source?.close(); source = undefined; connectedRun = ''; connection.value = '' }
}
function connect(runId: string) {
  if (connectedRun === runId) return
  source?.close(); connectedRun = runId; progress.value = []
  source = new EventSource(`${apiBase}/runs/${runId}/events`, { withCredentials: true })
  source.onopen = () => { connection.value = '实时更新已连接' }
  source.onerror = () => { connection.value = '连接已断开，正在重连。任务可能仍在继续。' }
  for (const kind of ['status', 'input', 'plan', 'todos', 'tool']) {
    source.addEventListener(kind, event => {
      const message = event as MessageEvent
      const id = Number(message.lastEventId)
      if (!progress.value.some(p => p.id === id)) progress.value.push({ id, kind, value: JSON.parse(message.data) })
      if (['status', 'input', 'tool'].includes(kind)) loadHistory().catch(e => { error.value = e.message })
    })
  }
  source.addEventListener('finished', () => { source?.close(); source = undefined; connection.value = ''; loadHistory().catch(e => { error.value = e.message }) })
  source.addEventListener('revoked', () => { source?.close(); history.value = undefined; progress.value = []; error.value = '登录已失效或访问权限已变更，请重新登录。' })
}
async function send() {
  if (!content.value.trim() || busy.value || active.value) return
  busy.value = true; error.value = ''
  if (lastMessage.content !== content.value.trim()) lastMessage = { id: crypto.randomUUID(), content: content.value.trim() }
  try {
    await api(`/sessions/${route.params.id}/inputs`, 'POST', { client_message_id: lastMessage.id, content: lastMessage.content })
    content.value = ''; lastMessage = { id: '', content: '' }; await loadHistory(); await loadSessions()
  } catch (e) { error.value = (e as Error).message } finally { busy.value = false }
}
async function stop() {
  if (!run.value) return
  busy.value = true
  try { await api(`/runs/${run.value.id}/stop`, 'POST'); await loadHistory() }
  catch (e) { error.value = (e as Error).message } finally { busy.value = false }
}
watch(() => route.params.id, async () => {
  source?.close(); source = undefined; connectedRun = ''; progress.value = []; history.value = undefined; error.value = ''
  try { await loadSessions(); await loadHistory() } catch (e) { error.value = (e as Error).message }
}, { immediate: true })
onBeforeUnmount(() => source?.close())
</script>
<template>
  <div class="page-head"><div><h1>{{ history?.app_name || '我的会话' }}</h1><p class="muted">会话仅你可见。离开页面不会停止正在运行的任务。</p></div><RouterLink to="/apps" class="button">选择应用</RouterLink></div>
  <p v-if="error" class="error" role="alert">{{ error }}</p>
  <template v-if="!route.params.id">
    <div v-if="!sessions.length" class="empty"><MessageSquare :size="32" /><h2>还没有会话</h2><p class="muted">在应用列表点击“使用”，开始第一个任务。</p><RouterLink to="/apps" class="button primary">查看应用</RouterLink></div>
    <div v-for="session in sessions" :key="session.id" class="session-row"><RouterLink :to="`/sessions/${session.id}`"><strong>{{ session.title }}</strong><small>{{ session.app_name }}</small></RouterLink></div>
  </template>
  <div v-else-if="history" class="chat-layout">
    <section class="panel conversation">
      <label v-if="sessions.length > 1">切换会话<select :value="route.params.id" @change="router.push(`/sessions/${($event.target as HTMLSelectElement).value}`)"><option v-for="session in sessions" :key="session.id" :value="session.id">{{ session.title }} · {{ session.app_name }}</option></select></label>
      <div class="messages" aria-live="polite">
        <div v-if="!history.items.length" class="empty"><h2>开始一个新任务</h2><p class="muted">描述目标、背景和期望的结果。</p></div>
        <article v-for="message in history.items" :key="message.id" class="message" :class="message.role"><div class="message-label">{{ message.role === 'user' ? '你' : 'Agent' }}<small v-if="message.status === 'submitting'">已保存 · 等待运行接收</small></div><div class="message-content">{{ message.content }}</div></article>
      </div>
      <p v-if="run?.error" class="error" role="alert">{{ run.error }}</p>
      <form class="composer" @submit.prevent="send"><label>任务内容<textarea v-model="content" rows="4" maxlength="30000" placeholder="输入你的问题或任务…" :disabled="active" @keydown.ctrl.enter.prevent="send" @keydown.meta.enter.prevent="send"></textarea></label>
        <div class="row spread"><small class="muted">{{ active ? '当前任务结束后可继续提问。运行中补充将在后续阶段开放。' : 'Ctrl / ⌘ + Enter 发送' }}</small><button v-if="active" type="button" :disabled="busy || run?.status === 'stopping'" @click="stop"><Square :size="15" />停止</button><button v-else class="primary" :disabled="busy || !content.trim()"><Send :size="15" />发送</button></div>
      </form>
    </section>
    <aside class="panel run-panel"><h2>运行过程</h2><template v-if="run"><div class="row"><span class="badge good">{{ labels[run.status] }}</span><span class="muted">{{ run.draft_revision ? `草稿测试 r${run.draft_revision}` : `发布 v${run.release_version}` }}</span></div><p class="muted">{{ connection }}</p>
      <dl><dt>模型调用</dt><dd>{{ run.model_calls }}</dd><dt>工具调用</dt><dd>{{ run.tool_calls }}</dd><dt>工作区清理</dt><dd>{{ { pending: '待处理', completed: '已完成', failed: '待重试' }[run.cleanup_state] || run.cleanup_state }}</dd></dl>
      <div v-for="item in currentProgress" :key="item.id" class="progress-item">
        <p v-if="item.kind === 'status'">{{ labels[item.value.status] }}</p>
        <p v-else-if="item.kind === 'input'">输入已接收</p>
        <p v-else-if="item.kind === 'tool'"><code>{{ item.value.name }}</code> · {{ item.value.status === 'completed' ? '成功' : item.value.status === 'failed' ? '失败' : active ? '执行中' : '结果未确认' }}<small>{{ item.value.role }}</small></p>
        <template v-else-if="item.kind === 'plan'"><h3>执行计划</h3><ol><li v-for="(task, index) in item.value.tasks" :key="index">{{ task.executor }} · {{ task.status === 'completed' ? '已完成' : '待执行' }}<small>{{ task.instruction }}</small></li></ol></template>
        <template v-else-if="item.kind === 'todos'"><h3>{{ item.value.role }} · 任务清单</h3><ul><li v-for="todo in item.value.todos" :key="todo">{{ todo }}</li></ul></template>
      </div>
      <small class="muted">这里只展示公开阶段与完整答案。</small>
    </template><p v-else class="muted">发送问题后显示任务进度。</p></aside>
  </div>
</template>
