<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { ArrowUp, Check, FlaskConical, LoaderCircle, RotateCcw, Square, Wrench } from 'lucide-vue-next'
import { api, apiBase, ApiError } from '../api'
import { terminal, type History, type Violation } from '../types'

const props = defineProps<{
  agentId: string; revision: number; dirty: boolean; disabled: boolean; modelName: string
  mode: string; prepare: () => Promise<number | null>
}>()
const emit = defineEmits<{ violations: [value: Violation[]] }>()
type Preview = { id: string; draft_revision: number; title: string }
type Progress = { id: number; kind: string; value: any }
const preview = ref<Preview>(), history = ref<History>(), content = ref(''), error = ref('')
const busy = ref(false), loading = ref(true), revoked = ref(false), connection = ref('')
const progress = ref<Progress[]>([]), transcript = ref<HTMLElement>()
const run = computed(() => history.value?.runs.at(-1))
const active = computed(() => !terminal(run.value))
const changed = computed(() => props.dirty || !!preview.value && preview.value.draft_revision !== props.revision)
const labels: Record<string, string> = { queued: '排队中', running: '执行中', stopping: '停止中', completed: '已完成', failed: '执行失败', cancelled: '已停止' }
const latestProgress = computed(() => {
  const latest = new Map<string, Progress>()
  for (const item of progress.value) latest.set(item.kind === 'tool' ? `tool:${item.value.call_id}` : item.kind === 'todos' ? `todos:${item.value.role}` : item.kind, item)
  return [...latest.values()].sort((a, b) => a.id - b.id)
})
const tools = computed(() => latestProgress.value.filter(item => item.kind === 'tool'))
const stages = computed(() => latestProgress.value.filter(item => ['plan', 'todos'].includes(item.kind)))
let source: EventSource | undefined, connectedRun = '', poll: ReturnType<typeof setInterval> | undefined
let alive = true, epoch = 0, refreshingEpoch: number | undefined
let lastMessage = { id: '', content: '', sessionId: '' }

function disconnect() { source?.close(); source = undefined; clearInterval(poll); connectedRun = ''; connection.value = '' }
function failure(e: unknown) {
  error.value = (e as Error).message
  if (e instanceof ApiError && [401, 403, 404].includes(e.status)) revoke()
}
function revoke() {
  disconnect(); ++epoch; revoked.value = true; history.value = undefined; progress.value = []
  error.value = '登录已失效或草稿测试权限已变更，请重新登录。'
}
async function scrollEnd() { await nextTick(); transcript.value?.scrollTo?.({ top: transcript.value.scrollHeight, behavior: 'smooth' }) }
async function refresh() {
  if (!preview.value || refreshingEpoch === epoch || !alive) return
  const id = preview.value.id, current = epoch
  refreshingEpoch = current
  try {
    const value = await api<History>(`/sessions/${id}/history`)
    if (!alive || current !== epoch || preview.value?.id !== id) return
    const added = value.items.length !== history.value?.items.length
    history.value = value
    if (run.value) connect(run.value.id)
    if (terminal(run.value)) clearInterval(poll)
    if (added) await scrollEnd()
  } catch (e) { if (alive && current === epoch) failure(e) }
  finally { if (refreshingEpoch === current) refreshingEpoch = undefined }
}
function connect(id: string) {
  if (connectedRun === id) return
  disconnect(); connectedRun = id; progress.value = []
  const current = epoch
  const events = new EventSource(`${apiBase}/runs/${id}/events`, { withCredentials: true })
  source = events
  const valid = () => alive && current === epoch && connectedRun === id
  events.onopen = () => { if (valid()) connection.value = '' }
  events.onerror = () => { if (valid()) connection.value = '正在重连，任务会继续执行…' }
  for (const kind of ['status', 'plan', 'todos', 'tool']) events.addEventListener(kind, event => {
    if (!valid()) return
    const message = event as MessageEvent, seq = Number(message.lastEventId)
    if (!progress.value.some(item => item.id === seq)) progress.value.push({ id: seq, kind, value: JSON.parse(message.data) })
    if (kind === 'status' || kind === 'tool') void refresh()
  })
  events.addEventListener('finished', () => {
    if (!valid()) return
    events.close(); source = undefined; connection.value = ''; void refresh()
  })
  events.addEventListener('revoked', () => { if (valid()) revoke() })
  if (active.value) poll = setInterval(() => { void refresh() }, 2000)
}
async function send() {
  const text = content.value.trim()
  if (!text || busy.value || loading.value || active.value || props.disabled || revoked.value) return
  const current = ++epoch
  disconnect()
  busy.value = true; error.value = ''; emit('violations', [])
  try {
    const revision = await props.prepare()
    if (!alive || current !== epoch || revision === null) return
    if (!preview.value || preview.value.draft_revision !== revision) {
      const created = await api<Preview>(`/agents/${props.agentId}/previews`, 'POST', { draft_revision: revision }, revision)
      if (!alive || current !== epoch) return
      disconnect(); preview.value = created; history.value = undefined; progress.value = []
    }
    if (lastMessage.content !== text || lastMessage.sessionId !== preview.value.id) lastMessage = { id: crypto.randomUUID(), content: text, sessionId: preview.value.id }
    await api(`/sessions/${preview.value.id}/inputs`, 'POST', { client_message_id: lastMessage.id, content: text })
    if (!alive || current !== epoch) return
    content.value = ''; lastMessage = { id: '', content: '', sessionId: '' }; await refresh()
  } catch (e) {
    if (alive && current === epoch) {
      failure(e)
      if (e instanceof ApiError) emit('violations', e.violations)
    }
  } finally { if (alive) busy.value = false }
}
async function stop() {
  if (!run.value || busy.value) return
  const current = epoch
  busy.value = true; error.value = ''
  try { await api(`/runs/${run.value.id}/stop`, 'POST'); if (alive && current === epoch) await refresh() }
  catch (e) { if (alive && current === epoch) failure(e) }
  finally { if (alive) busy.value = false }
}
function newTest() {
  if (busy.value || loading.value || active.value) return
  ++epoch; disconnect(); preview.value = undefined; history.value = undefined; progress.value = []; error.value = ''
  lastMessage = { id: '', content: '', sessionId: '' }
}
onMounted(async () => {
  const current = epoch
  try {
    const result = await api<{items: Preview[]}>(`/agents/${props.agentId}/previews`)
    if (!alive || current !== epoch) return
    preview.value = result.items[0]
    await refresh()
  } catch (e) { if (alive && current === epoch) failure(e) }
  finally { if (alive) loading.value = false }
})
onBeforeUnmount(() => { alive = false; ++epoch; disconnect() })
</script>

<template>
  <aside class="draft-playground" aria-label="草稿测试">
    <header class="playground-heading">
      <div class="row"><span class="playground-symbol"><FlaskConical :size="17" /></span><h2>测试</h2><span class="badge">当前草稿</span></div>
      <button class="icon-button" title="新建测试会话" aria-label="新建测试会话" :disabled="busy || loading || active || revoked" @click="newTest"><RotateCcw :size="17" /></button>
    </header>
    <div class="playground-context"><span class="model-dot"></span><span>{{ modelName || '尚未选择模型' }}</span><span v-if="preview" class="muted">草稿 r{{ preview.draft_revision }}</span></div>
    <p v-if="changed && preview" class="preview-change" role="status">{{ active ? '配置已修改，本次继续使用启动时的草稿。' : '配置已更新，下次发送将开始新的测试会话。' }}</p>
    <div ref="transcript" class="playground-transcript" aria-live="polite">
      <div v-if="!history?.items.length" class="playground-empty">
        <div class="test-illustration"><FlaskConical :size="25" :stroke-width="1.5" /></div>
        <h3>{{ loading ? '正在加载测试记录…' : '在这里试试你的 Agent' }}</h3>
        <p>左边调整配置，右边发送任务。<br>发送时自动保存并测试当前草稿。</p>
        <span class="test-privacy">测试会话仅你可见</span>
      </div>
      <article v-for="message in history?.items || []" :key="message.id" class="test-message" :class="message.role">
        <div class="test-message-label">{{ message.role === 'user' ? '你' : 'Agent' }}</div>
        <div class="message-content">{{ message.content }}</div>
      </article>
      <div v-if="run" class="test-run">
        <div class="test-run-summary"><span :class="['run-state', run.status]"><LoaderCircle v-if="active" :size="14" class="spinning" /><Check v-else-if="run.status === 'completed'" :size="14" />{{ labels[run.status] || run.status }}</span><span>模型 {{ run.model_calls }} 次</span><span>工具 {{ run.tool_calls }} 次</span></div>
        <details v-if="stages.length || tools.length" class="test-progress" :open="active">
          <summary>运行过程 <span>{{ tools.length ? `${tools.length} 次工具调用` : '执行计划' }}</span></summary>
          <template v-for="item in stages" :key="item.id">
            <ol v-if="item.kind === 'plan'" class="test-plan"><li v-for="(task, index) in item.value.tasks" :key="index"><div class="row spread"><strong>{{ task.executor }}</strong><span class="muted">{{ task.status === 'completed' ? '已完成' : '待执行' }}</span></div><p>{{ task.instruction }}</p></li></ol>
            <div v-else class="test-todos"><strong>任务清单</strong><ul><li v-for="todo in item.value.todos" :key="todo">{{ todo }}</li></ul></div>
          </template>
          <div v-for="item in tools" :key="item.id" class="test-tool"><Wrench :size="14" /><code>{{ item.value.name }}</code><small>{{ mode === 'react' && item.value.role === 'planner' ? 'Agent' : item.value.role }}</small><span>{{ item.value.status === 'completed' ? '成功' : item.value.status === 'failed' ? '失败' : active ? '执行中' : '结果未确认' }}</span></div>
        </details>
        <p v-if="run.error" class="error" role="alert">{{ run.error }}</p>
      </div>
    </div>
    <form class="test-composer" @submit.prevent="send">
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <p v-if="connection" class="muted" role="status">{{ connection }}</p>
      <div class="test-input"><label class="sr-only" for="test-content">测试任务</label><textarea id="test-content" v-model="content" rows="3" maxlength="30000" placeholder="输入任务，测试当前配置…" :disabled="active || busy || loading || revoked" @keydown.ctrl.enter.prevent="send" @keydown.meta.enter.prevent="send"></textarea>
        <div class="row spread"><small>{{ active ? '正在执行，可以随时停止' : '⌘ / Ctrl + Enter 发送' }}</small><button v-if="active" type="button" :disabled="busy || run?.status === 'stopping'" @click="stop"><Square :size="13" />停止</button><button v-else class="primary" :disabled="busy || loading || disabled || revoked || !content.trim()" aria-label="发送测试"><LoaderCircle v-if="busy" class="spinning" :size="16" /><ArrowUp v-else :size="17" /></button></div>
      </div>
      <p class="test-footnote">测试将调用所选模型和已授权工具</p>
    </form>
  </aside>
</template>
