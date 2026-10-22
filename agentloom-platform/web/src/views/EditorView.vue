<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute } from 'vue-router'
import { ArrowLeft, Save } from 'lucide-vue-next'
import DraftPlayground from '../components/DraftPlayground.vue'
import '../editor.css'
import { api, ApiError } from '../api'
import { canEdit, state } from '../state'
import { definition, type Agent, type Definition, type Resource, type Violation } from '../types'
const route = useRoute(), agent = ref<Agent>(), resources = ref<Resource[]>([]), baseline = ref('')
const tab = ref('basic'), roleKey = ref('planner'), error = ref(''), notice = ref(''), busy = ref(false)
const violations = ref<Violation[]>([]), validatedRevision = ref(0), confirmPublish = ref(false)
const executorKey = ref(''), releases = ref<any[]>([]), jobStatus = ref('')
const mobilePane = ref('edit')
let poll: ReturnType<typeof setInterval> | undefined
let loadGeneration = 0
const dirty = computed(() => !!agent.value && JSON.stringify(agent.value.draft) !== baseline.value)
const currentRole = computed<Definition | undefined>(() => roleKey.value === 'planner' ? agent.value?.draft.definition : agent.value?.draft.executors[roleKey.value])
const groups = [{ key: 'tool', title: 'Tool · 工具' }, { key: 'skill', title: 'Skill · 技能' }, { key: 'mcp', title: 'MCP · 服务' }]
const models = computed(() => resources.value.filter(r => r.kind === 'model'))
const modelName = computed(() => {
  const binding = agent.value?.draft.model, resource = models.value.find(item => item.id === binding?.resource_id)
  return resource?.versions.find(item => item.version === binding?.version)?.spec.model_id || resource?.name || ''
})
const modelValue = computed({
  get: () => agent.value?.draft.model ? `${agent.value.draft.model.resource_id}:${agent.value.draft.model.version}` : '',
  set: (value: string) => {
    if (!agent.value) return
    const [resource_id, version] = value.split(':')
    agent.value.draft.model = value ? { resource_id, version: Number(version), members: [] } : null
  },
})
async function load() {
  const generation = ++loadGeneration, id = String(route.params.id)
  clearInterval(poll)
  if (agent.value?.id !== id) agent.value = undefined
  confirmPublish.value = false; notice.value = ''; jobStatus.value = ''; releases.value = []; violations.value = []
  if (!canEdit.value) { error.value = '当前角色不能编辑应用'; return }
  error.value = ''
  try {
    const [app, catalog] = await Promise.all([
      api<Agent>(`/agents/${id}`), api<{items: Resource[]}>(`/spaces/${state.spaceId}/resources`),
    ])
    if (generation !== loadGeneration) return
    agent.value = app; resources.value = catalog.items; baseline.value = JSON.stringify(app.draft)
    validatedRevision.value = 0; roleKey.value = 'planner'
    const history = await api<{items: any[]}>(`/agents/${app.id}/releases`)
    if (generation !== loadGeneration) return
    releases.value = history.items
    if (app.lifecycle === 'publishing') startPolling()
  } catch (e) { if (generation === loadGeneration) error.value = (e as Error).message }
}
function binding(resource: Resource) { return currentRole.value?.capabilities.find(b => b.resource_id === resource.id) }
function currentVersion(resource: Resource) {
  return resource.versions.find(v => v.version === binding(resource)?.version) || resource.versions.find(v => v.status === 'ready') || resource.versions[0]
}
function toggle(resource: Resource, checked: boolean) {
  if (!currentRole.value) return
  if (!checked) currentRole.value.capabilities = currentRole.value.capabilities.filter(b => b.resource_id !== resource.id)
  else currentRole.value.capabilities.push({ resource_id: resource.id, version: currentVersion(resource).version, members: [] })
}
function setVersion(resource: Resource, event: Event) {
  const chosen = binding(resource)
  if (chosen) { chosen.version = Number((event.target as HTMLSelectElement).value); chosen.members = [] }
}
function toggleMember(resource: Resource, name: string, checked: boolean) {
  const chosen = binding(resource)
  if (chosen) chosen.members = checked ? [...chosen.members, name] : chosen.members.filter(m => m !== name)
}
function addExecutor() {
  const key = executorKey.value.trim()
  if (!/^[A-Za-z_][A-Za-z0-9_-]{0,63}$/.test(key) || /^(planner|main|system)/.test(key)) { error.value = '执行器标识格式不正确或使用了保留前缀'; return }
  if (!agent.value || agent.value.draft.executors[key]) { error.value = '执行器标识已存在'; return }
  agent.value.draft.executors[key] = definition(); executorKey.value = ''; error.value = ''
}
async function save() {
  if (!agent.value || busy.value) return false
  const current = agent.value, submitted = JSON.stringify(current.draft)
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const saved = await api<Agent>(`/agents/${current.id}/draft`, 'PUT', JSON.parse(submitted), current.revision)
    if (agent.value !== current) return false
    const latestDraft = JSON.stringify(current.draft) === submitted ? saved.draft : current.draft
    agent.value = { ...saved, draft: latestDraft }
    baseline.value = JSON.stringify(saved.draft); validatedRevision.value = 0; violations.value = []
    notice.value = dirty.value ? '已保存提交的版本，后续修改尚未保存' : '草稿已保存'
    return true
  } catch (e) { if (agent.value === current) error.value = (e as Error).message; return false } finally { busy.value = false }
}
async function validate() {
  if (busy.value || !agent.value) return
  if (dirty.value && !await save()) return
  if (dirty.value) { notice.value = '有新的未保存修改，请保存后再校验'; return }
  const current = agent.value, revision = current.revision
  busy.value = true; error.value = ''
  try {
    const report = await api<{ valid: boolean; violations: Violation[] }>(`/agents/${current.id}/validate`, 'POST', { draft_revision: revision })
    if (agent.value !== current) return
    violations.value = report.violations; validatedRevision.value = report.valid ? revision : 0
    notice.value = report.valid ? (dirty.value ? '已保存版本校验通过，当前修改尚未校验' : '配置校验通过') : ''
  } catch (e) { if (agent.value === current) error.value = (e as Error).message } finally { busy.value = false }
}
async function prepareTest() {
  if (!agent.value || busy.value || agent.value.lifecycle !== 'active') return null
  const id = agent.value.id
  if (dirty.value && !await save()) return null
  if (agent.value?.id !== id) return null
  if (dirty.value) { notice.value = '还有新的修改尚未保存，请再次发送测试'; return null }
  return agent.value.revision
}
async function publish() {
  if (!agent.value || busy.value || dirty.value || validatedRevision.value !== agent.value.revision) return
  const current = agent.value
  busy.value = true; error.value = ''
  try {
    const receipt = await api<{job_id: string; status: string}>(`/agents/${agent.value.id}/releases`, 'POST',
      { draft_revision: agent.value.revision }, agent.value.revision)
    if (agent.value !== current) return
    confirmPublish.value = false; agent.value.lifecycle = 'publishing'; jobStatus.value = receipt.status
    startPolling(receipt.job_id)
  } catch (e) {
    if (agent.value === current) {
      error.value = (e as Error).message
      if (e instanceof ApiError) violations.value = e.violations
    }
  } finally { busy.value = false }
}
function startPolling(jobId?: string) {
  clearInterval(poll)
  poll = setInterval(async () => {
    if (!agent.value) return
    const current = agent.value
    try {
      const job = jobId ? await api<{status: string}>(`/release-jobs/${jobId}`) : undefined
      if (agent.value !== current) return
      if (job) jobStatus.value = job.status
      const latest = await api<Agent>(`/agents/${current.id}`)
      if (agent.value !== current) return
      agent.value.lifecycle = latest.lifecycle; agent.value.release_id = latest.release_id; agent.value.release_version = latest.release_version
      if (latest.lifecycle === 'active') {
        clearInterval(poll); notice.value = '新版本已发布'
        const history = await api<{items: any[]}>(`/agents/${latest.id}/releases`)
        if (agent.value === current) releases.value = history.items
      }
    } catch (e) { if (agent.value === current) { error.value = (e as Error).message; clearInterval(poll) } }
  }, 2000)
}
watch(() => route.params.id, load, { immediate: true })
const confirmLeave = () => !dirty.value || window.confirm('草稿有未保存修改，确定离开？')
onBeforeRouteLeave(confirmLeave)
onBeforeRouteUpdate((to, from) => to.params.id === from.params.id || confirmLeave())
function beforeUnload(event: BeforeUnloadEvent) {
  if (dirty.value) { event.preventDefault(); event.returnValue = '' }
}
window.addEventListener('beforeunload', beforeUnload)
onBeforeUnmount(() => { ++loadGeneration; clearInterval(poll); window.removeEventListener('beforeunload', beforeUnload) })
</script>
<template>
  <div v-if="agent && canEdit" class="editor-workspace" :data-mobile-pane="mobilePane">
    <nav class="editor-mobile-switch" aria-label="编辑与测试"><button :class="{ active: mobilePane === 'edit' }" @click="mobilePane = 'edit'">编辑配置</button><button :class="{ active: mobilePane === 'test' }" @click="mobilePane = 'test'">测试</button></nav>
    <section class="editor-config" aria-label="应用配置">
      <header class="editor-heading"><RouterLink class="icon-button button" to="/apps" aria-label="返回应用" title="返回应用"><ArrowLeft :size="18" /></RouterLink><h1 :title="agent.name">{{ agent.name }}</h1><span class="badge">{{ agent.mode === 'react' ? 'ReAct' : 'Plan' }}</span></header>
      <nav class="editor-tabs" aria-label="配置分组"><button v-for="item in [['basic','基础配置'],['capabilities','能力配置'],['environment','运行环境'],['releases','发布记录']]" :key="item[0]" :class="{ active: tab === item[0] }" @click="tab = item[0]">{{ item[1] }}</button></nav>
      <div class="editor-scroll">
      <p v-if="error" class="error" role="alert">{{ error }} <button v-if="error.includes('草稿已被修改')" @click="load">重新加载草稿</button></p>
      <p v-if="notice" class="success" role="status">{{ notice }}</p>
      <ul v-if="violations.length" class="violations"><li v-for="v in violations" :key="v.path + v.code">{{ v.message }}<small>{{ v.path }}</small></li></ul>
      <p v-if="agent.lifecycle === 'publishing'" class="callout">{{ jobStatus === 'needs_attention' ? '存在结果未确认的工具操作，需要管理员核对后才能完成发布。' : '正在等待旧任务停止与清理，完成后将启用新版本。' }}</p>
      <template v-if="tab === 'basic'">
        <h2>基本信息</h2><label>应用描述（选填）<textarea v-model="agent.draft.description" maxlength="2000" rows="3"></textarea></label>
        <h2>模型与行为</h2><label>模型连接<select v-model="modelValue"><option value="">请选择模型连接</option><template v-for="model in models" :key="model.id"><option v-for="version in model.versions" :key="version.version" :value="`${model.id}:${version.version}`">{{ model.name }} · v{{ version.version }}</option></template></select></label>
        <RouterLink v-if="!models.length" class="text-link" to="/resources/model">先添加模型连接</RouterLink>
        <label>{{ agent.mode === 'react' ? '系统提示词' : 'Planner 系统提示词' }}<textarea v-model="agent.draft.definition.prompt" rows="7" maxlength="30000" placeholder="说明任务目标、工作方式与结果要求"></textarea></label>
        <template v-if="agent.mode === 'plan_executor'">
          <h2>专业执行器</h2><p class="muted">任务按计划顺序执行。每位执行器独立配置职责和能力，共用应用模型与工作区。</p>
          <div v-for="(executor, key) in agent.draft.executors" :key="key" class="resource-block">
            <div class="row spread"><code>{{ key }}</code><button class="text-link" @click="roleKey = String(key); tab = 'capabilities'">配置能力</button></div>
            <label>执行器名称<input v-model="executor.name" maxlength="100"></label>
            <label>职责说明<input v-model="executor.description" maxlength="2000"></label>
            <label>执行器提示词<textarea v-model="executor.prompt" rows="4"></textarea></label>
            <button class="danger" @click="delete agent.draft.executors[key]; roleKey = 'planner'">移除此执行器</button>
          </div>
          <div class="row"><input v-model="executorKey" placeholder="执行器标识，例如 research" aria-label="新执行器标识"><button @click="addExecutor">添加执行器</button></div>
        </template>
      </template>
      <template v-if="tab === 'capabilities' && currentRole">
        <h2>能力配置</h2>
        <label v-if="agent.mode === 'plan_executor'">配置对象<select v-model="roleKey"><option value="planner">Planner · 任务规划</option><option v-for="(executor, key) in agent.draft.executors" :key="key" :value="key">{{ executor.name || key }}</option></select></label>
        <p class="muted">只授权给当前角色。选择资源版本后，新增版本不会自动替换。</p>
        <section v-for="group in groups" :key="group.key" class="capability-group">
          <div class="row spread"><h3>{{ group.title }}</h3><RouterLink :to="`/resources/${group.key}`" class="text-link">管理{{ group.title.split(' · ')[0] }}</RouterLink></div>
          <p v-if="!resources.some(r => r.kind === group.key)" class="muted">请先在管理页添加资源。</p>
          <div v-for="resource in resources.filter(r => r.kind === group.key)" :key="resource.id" class="capability-item">
            <label class="check"><input type="checkbox" :checked="!!binding(resource)" :disabled="!binding(resource) && currentVersion(resource).status !== 'ready'"
              @change="toggle(resource, ($event.target as HTMLInputElement).checked)"><span>{{ resource.name }}<small>{{ resource.description }}{{ currentVersion(resource).status !== 'ready' ? ' · 尚不可用' : '' }}</small></span></label>
            <template v-if="binding(resource)">
              <label class="version-picker">固定版本<select :value="binding(resource)!.version" @change="setVersion(resource, $event)"><option v-for="version in resource.versions.filter(v => v.status === 'ready')" :key="version.version" :value="version.version">v{{ version.version }}</option></select></label>
              <div v-if="resource.kind === 'mcp'" class="members">
                <label v-for="member in currentVersion(resource).spec.members || []" :key="member.name" class="check"><input type="checkbox" :checked="binding(resource)!.members.includes(member.name)" @change="toggleMember(resource, member.name, ($event.target as HTMLInputElement).checked)"><span>{{ member.name }}<small>{{ member.description }}</small></span></label>
              </div>
            </template>
          </div>
        </section>
      </template>
      <template v-if="tab === 'environment'">
        <h2>执行沙箱</h2><label class="check"><input type="checkbox" disabled><span>启用执行沙箱<small>沙箱服务尚未接入，命令和脚本执行暂不可用。</small></span></label>
        <h2>文件工作区</h2><label>访问权限<select v-model="agent.draft.environment.workspace_access"><option value="read_write">可读写</option><option value="read_only">只读</option></select></label>
        <label>文件保留策略<select v-model="agent.draft.environment.retention"><option value="run">任务结束后清理</option><option value="session">在同一会话中保留</option></select></label>
        <h2>运行额度</h2><div class="form-grid"><label>最多模型调用<input v-model.number="agent.draft.execution.model_calls" type="number" min="1" max="100"></label><label>最多工具调用<input v-model.number="agent.draft.execution.tool_calls" type="number" min="1" max="200"></label><label>任务期限（秒）<input v-model.number="agent.draft.execution.timeout_seconds" type="number" min="10" max="1800"></label><label v-if="agent.mode === 'plan_executor'">最多委派任务<input v-model.number="agent.draft.execution.max_tasks" type="number" min="1" max="20"></label></div>
      </template>
      <template v-if="tab === 'releases'"><h2>发布记录</h2><p v-if="!releases.length" class="muted">还没有发布记录。</p><details v-for="release in releases" :key="release.id" class="resource-block"><summary>v{{ release.version }} · 草稿 r{{ release.draft_revision }} · {{ new Date(release.created_at * 1000).toLocaleString() }}</summary><p class="muted">固定快照 · {{ release.content_hash.slice(0, 16) }}</p><pre>{{ JSON.stringify(release.snapshot.draft, null, 2) }}</pre></details></template>
      </div>
      <footer class="editor-footer"><span class="draft-status" :class="{ unsaved: dirty }"><i></i>{{ dirty ? '有未保存修改' : `草稿 r${agent.revision} 已保存` }}</span><div class="row"><button :disabled="busy || !dirty" @click="save"><Save :size="14" />保存草稿</button><button :disabled="busy" @click="validate">校验配置</button><button class="primary" :disabled="busy || dirty || validatedRevision !== agent.revision || agent.lifecycle !== 'active'" @click="confirmPublish = true">发布…</button></div></footer>
    </section>
    <DraftPlayground :key="agent.id" :agent-id="agent.id" :revision="agent.revision" :dirty="dirty" :disabled="busy || agent.lifecycle !== 'active'" :mode="agent.mode" :model-name="modelName" :prepare="prepareTest" @violations="violations = $event" />
    <div v-if="confirmPublish" class="modal-backdrop"><section class="modal" role="dialog" aria-modal="true" aria-labelledby="publish-title"><h2 id="publish-title">发布 {{ agent.name }}</h2><p>将固定草稿 r{{ agent.revision }} 的模型、提示词、角色资源和运行环境。</p><p class="callout">发布会停止该应用的旧版本活动任务，确认停止与清理后才切换版本。期间暂不接收新提问。</p><div class="row end"><button :disabled="busy" @click="confirmPublish = false">取消</button><button class="primary" :disabled="busy" @click="publish">确认发布</button></div></section></div>
  </div>
  <div v-else class="editor-loading"><RouterLink to="/apps" class="text-link">← 返回应用</RouterLink><p v-if="error" class="error" role="alert">{{ error }}</p><p v-else class="muted">正在加载应用配置…</p></div>
</template>
