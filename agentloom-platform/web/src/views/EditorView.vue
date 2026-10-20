<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRoute } from 'vue-router'
import { api, ApiError } from '../api'
import { canEdit, state } from '../state'
import { definition, type Agent, type Definition, type Resource, type Violation } from '../types'
const route = useRoute(), agent = ref<Agent>(), resources = ref<Resource[]>([]), baseline = ref('')
const tab = ref('basic'), roleKey = ref('planner'), error = ref(''), notice = ref(''), busy = ref(false)
const violations = ref<Violation[]>([]), validatedRevision = ref(0), confirmPublish = ref(false)
const executorKey = ref(''), releases = ref<any[]>([]), jobStatus = ref('')
let poll: ReturnType<typeof setInterval> | undefined
const dirty = computed(() => !!agent.value && JSON.stringify(agent.value.draft) !== baseline.value)
const currentRole = computed<Definition | undefined>(() => roleKey.value === 'planner' ? agent.value?.draft.definition : agent.value?.draft.executors[roleKey.value])
const groups = [{ key: 'tool', title: 'Tool · 工具' }, { key: 'skill', title: 'Skill · 技能' }, { key: 'mcp', title: 'MCP · 服务' }]
const models = computed(() => resources.value.filter(r => r.kind === 'model'))
const modelValue = computed({
  get: () => agent.value?.draft.model ? `${agent.value.draft.model.resource_id}:${agent.value.draft.model.version}` : '',
  set: (value: string) => {
    if (!agent.value) return
    const [resource_id, version] = value.split(':')
    agent.value.draft.model = value ? { resource_id, version: Number(version), members: [] } : null
  },
})
async function load() {
  if (!canEdit.value) { error.value = '当前角色不能编辑应用'; return }
  error.value = ''
  try {
    const [app, catalog] = await Promise.all([
      api<Agent>(`/agents/${route.params.id}`), api<{items: Resource[]}>(`/spaces/${state.spaceId}/resources`),
    ])
    agent.value = app; resources.value = catalog.items; baseline.value = JSON.stringify(app.draft)
    validatedRevision.value = 0; roleKey.value = 'planner'
    releases.value = (await api<{items: any[]}>(`/agents/${app.id}/releases`)).items
    if (app.lifecycle === 'publishing') startPolling()
  } catch (e) { error.value = (e as Error).message }
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
  if (!agent.value) return false
  busy.value = true; error.value = ''; notice.value = ''
  try {
    agent.value = await api<Agent>(`/agents/${agent.value.id}/draft`, 'PUT', agent.value.draft, agent.value.revision)
    baseline.value = JSON.stringify(agent.value.draft); validatedRevision.value = 0; violations.value = []; notice.value = '草稿已保存'
    return true
  } catch (e) { error.value = (e as Error).message; return false } finally { busy.value = false }
}
async function validate() {
  if (dirty.value && !await save()) return
  busy.value = true; error.value = ''
  try {
    const report = await api<{ valid: boolean; violations: Violation[] }>(`/agents/${agent.value!.id}/validate`, 'POST', { draft_revision: agent.value!.revision })
    violations.value = report.violations; validatedRevision.value = report.valid ? agent.value!.revision : 0
    notice.value = report.valid ? '配置校验通过' : ''
  } catch (e) { error.value = (e as Error).message } finally { busy.value = false }
}
async function publish() {
  if (!agent.value) return
  busy.value = true; error.value = ''
  try {
    const receipt = await api<{job_id: string; status: string}>(`/agents/${agent.value.id}/releases`, 'POST',
      { draft_revision: agent.value.revision }, agent.value.revision)
    confirmPublish.value = false; agent.value.lifecycle = 'publishing'; jobStatus.value = receipt.status
    startPolling(receipt.job_id)
  } catch (e) {
    error.value = (e as Error).message
    if (e instanceof ApiError) violations.value = e.violations
  } finally { busy.value = false }
}
function startPolling(jobId?: string) {
  clearInterval(poll)
  poll = setInterval(async () => {
    if (!agent.value) return
    try {
      if (jobId) jobStatus.value = (await api<{status: string}>(`/release-jobs/${jobId}`)).status
      const latest = await api<Agent>(`/agents/${agent.value.id}`)
      agent.value.lifecycle = latest.lifecycle; agent.value.release_id = latest.release_id; agent.value.release_version = latest.release_version
      if (latest.lifecycle === 'active') {
        clearInterval(poll); notice.value = '新版本已发布'
        releases.value = (await api<{items: any[]}>(`/agents/${latest.id}/releases`)).items
      }
    } catch (e) { error.value = (e as Error).message; clearInterval(poll) }
  }, 2000)
}
watch(() => route.params.id, load, { immediate: true })
onBeforeRouteLeave(() => !dirty.value || window.confirm('草稿有未保存修改，确定离开？'))
onBeforeUnmount(() => clearInterval(poll))
</script>
<template>
  <p v-if="error" class="error" role="alert">{{ error }} <button v-if="error.includes('草稿已被修改')" @click="load">重新加载草稿</button></p>
  <template v-if="agent && canEdit">
    <div class="page-head"><div><h1>{{ agent.name }}</h1><p class="muted">{{ agent.mode === 'react' ? 'ReAct' : 'Plan' }} 应用配置 · 执行模式创建后固定</p></div><RouterLink class="button" to="/apps">返回应用</RouterLink></div>
    <div class="editor-layout"><section class="panel">
      <div class="row spread"><span class="badge">草稿 r{{ agent.revision }}</span><span class="muted">{{ dirty ? '有未保存修改' : '草稿已保存' }}</span></div>
      <nav class="tabs" aria-label="配置分组"><button v-for="item in [['basic','基础配置'],['capabilities','能力配置'],['environment','运行环境'],['releases','发布记录']]" :key="item[0]" :class="{ active: tab === item[0] }" @click="tab = item[0]">{{ item[1] }}</button></nav>
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
    </section>
    <aside class="panel publish-panel"><h2>发布与使用</h2><p class="muted">保存草稿，通过校验后发布给空间成员。</p><dl><dt>当前发布</dt><dd>{{ agent.release_version ? `v${agent.release_version}` : '尚未发布' }}</dd><dt>运行模式</dt><dd>{{ agent.mode === 'react' ? 'ReAct' : 'Plan' }}</dd></dl>
      <p v-if="notice" class="success" role="status">{{ notice }}</p>
      <ul v-if="violations.length" class="violations"><li v-for="v in violations" :key="v.path + v.code">{{ v.message }}<small>{{ v.path }}</small></li></ul>
      <p v-if="agent.lifecycle === 'publishing'" class="callout">{{ jobStatus === 'needs_attention' ? '存在结果未确认的工具操作，需要管理员核对后才能激活新版本。' : '正在等待旧任务结束和工作区清理。请保持 Worker 与维护服务运行。' }}</p>
      <div class="stack"><button :disabled="busy || !dirty" @click="save">保存草稿</button><button :disabled="busy" @click="validate">校验配置</button><button class="primary" :disabled="busy || dirty || validatedRevision !== agent.revision || agent.lifecycle !== 'active'" @click="confirmPublish = true">发布…</button></div>
    </aside></div>
    <div v-if="confirmPublish" class="modal-backdrop"><section class="modal" role="dialog" aria-modal="true" aria-labelledby="publish-title"><h2 id="publish-title">发布 {{ agent.name }}</h2><p>将固定草稿 r{{ agent.revision }} 的模型、提示词、角色资源和运行环境。</p><p class="callout">发布会停止该应用的旧版本活动任务，确认停止与清理后才切换版本。期间暂不接收新提问。</p><div class="row end"><button :disabled="busy" @click="confirmPublish = false">取消</button><button class="primary" :disabled="busy" @click="publish">确认发布</button></div></section></div>
  </template>
</template>
