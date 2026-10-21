<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Plus, RefreshCw, Search, Server, FolderOpen, Shield, KeyRound, X } from 'lucide-vue-next'
import { api } from '../api'
import { state, isAdmin, canEdit } from '../state'
import type { Resource, ResourceVersion } from '../types'
const route = useRoute(), resources = ref<Resource[]>([]), search = ref(''), category = ref('all')
const error = ref(''), busy = ref(false), loading = ref(true), modal = ref(false), detail = ref<Resource>()
const detailVersion = ref(1), editing = ref<Resource>(), credentialFor = ref<Resource>(), secret = ref('')
const form = ref({ name: '', description: '', registry_key: 'read_file', url: '', model_id: '', endpoint: 'https://api.openai.com/v1', skill: '' })
const skillFiles = ref<Record<string, string>>({}), memberSelection = ref<string[]>([])
const modelCredential = ref(''), modelChoices = ref<{id: string}[]>([]), modelsLoading = ref(false)
const modelsLoaded = ref(false), modelError = ref(''), manualModel = ref(false)
let catalogGeneration = 0, loadGeneration = 0, pageGeneration = 0
const kind = computed(() => String(route.params.kind))
const title = computed(() => ({ tool: 'Tool · 工具', skill: 'Skill · 技能', mcp: 'MCP · 服务', model: '模型与环境' }[kind.value] || '资源'))
const addLabel = computed(() => ({ tool: '注册工具', skill: '导入 Skill', mcp: '添加 MCP', model: '添加模型连接' }[kind.value]))
const categories: Record<string, string> = { files: '文件与工作区', execution: '命令与任务', integrations: '外部工具集成', orchestration: '子 Agent 编排' }
const readyImplementations = computed(() => [...new Map(resources.value.filter(r => r.versions[0].status === 'ready').map(r => [r.versions[0].spec.registry_key, r])).values()])
const visible = computed(() => resources.value.filter(r => (r.name + r.description + (r.versions[0].spec.registry_key || '') + (r.versions[0].spec.model_id || '') + (r.versions[0].spec.endpoint || '')).toLowerCase().includes(search.value.toLowerCase())
  && (category.value === 'all' || r.versions[0].spec.category === category.value)))
const selectedVersion = computed(() => detail.value?.versions.find(v => v.version === detailVersion.value))
const statusLabel = (v: ResourceVersion) => ({ ready: '可用', pending: '待发现', discovered: '待选择成员', unavailable: '尚未接入' }[v.status] || v.status)
const sameEndpoint = computed(() => form.value.endpoint.trim().replace(/\/+$/, '') === editing.value?.versions[0].spec.endpoint?.replace(/\/+$/, ''))
const savedCredential = computed(() => !!editing.value?.credential_configured && sameEndpoint.value)
function invalidateModels() {
  catalogGeneration++; modelChoices.value = []; modelsLoading.value = false; modelsLoaded.value = false; modelError.value = ''
}
watch([() => form.value.endpoint, modelCredential], invalidateModels, { flush: 'sync' })
async function fetchModels() {
  if (kind.value !== 'model' || !modal.value || !form.value.endpoint.trim()) return
  const generation = ++catalogGeneration
  modelsLoading.value = true; modelsLoaded.value = false; modelError.value = ''; modelChoices.value = []
  try {
    const result = await api<{items: {id: string}[]}>(`/spaces/${state.spaceId}/model-catalog`, 'POST', {
      endpoint: form.value.endpoint.trim(),
      ...(modelCredential.value ? { credential: modelCredential.value } : {}),
      ...(editing.value && sameEndpoint.value ? { resource_id: editing.value.id } : {}),
    })
    if (generation !== catalogGeneration || !modal.value) return
    modelChoices.value = result.items; modelsLoaded.value = true
    if (!manualModel.value && !result.items.some(item => item.id === form.value.model_id)) form.value.model_id = ''
  } catch (e) { if (generation === catalogGeneration && modal.value) modelError.value = (e as Error).message }
  finally { if (generation === catalogGeneration) modelsLoading.value = false }
}
function closeForm() { modal.value = false; modelCredential.value = ''; invalidateModels() }
async function load() {
  const generation = ++loadGeneration
  loading.value = true; resources.value = []
  if (!canEdit.value) { error.value = '当前角色不能访问资源管理'; loading.value = false; return }
  try {
    const result = await api<{items: Resource[]}>(`/spaces/${state.spaceId}/resources?kind=${kind.value}`)
    if (generation === loadGeneration) resources.value = result.items
  } catch (e) { if (generation === loadGeneration) error.value = (e as Error).message }
  finally { if (generation === loadGeneration) loading.value = false }
}
function openForm(resource?: Resource) {
  editing.value = resource; error.value = ''; modal.value = true
  const spec = resource?.versions[0].spec || {}
  form.value = { name: resource?.name || '', description: resource?.description || '', registry_key: spec.registry_key || 'read_file',
    url: spec.url || '', model_id: spec.model_id || '', endpoint: spec.endpoint || 'https://api.openai.com/v1', skill: spec.files?.['SKILL.md'] || '' }
  skillFiles.value = { ...(spec.files || {}) }
  modelCredential.value = ''; manualModel.value = false; invalidateModels()
  if (kind.value === 'model' && resource) void fetchModels()
}
async function upload(event: Event) {
  const files = (event.target as HTMLInputElement).files
  if (!files) return
  if ([...files].reduce((total, f) => total + f.size, 0) > 1_500_000) { error.value = '技能包不能超过 1.5 MB'; return }
  skillFiles.value = {}
  for (const file of files) {
    const relative = file.webkitRelativePath ? file.webkitRelativePath.split('/').slice(1).join('/') : file.name
    skillFiles.value[relative] = await file.text()
  }
  form.value.skill = skillFiles.value['SKILL.md'] || ''
}
async function save() {
  if (busy.value) return
  if (kind.value === 'model' && (!form.value.model_id || (!manualModel.value && !modelChoices.value.some(item => item.id === form.value.model_id)))) {
    error.value = '请从可用模型中选择，或切换为手动输入模型 ID'; return
  }
  busy.value = true; error.value = ''
  const generation = pageGeneration
  let spec: any
  if (kind.value === 'tool') spec = { registry_key: form.value.registry_key }
  else if (kind.value === 'skill') spec = { files: { ...skillFiles.value, 'SKILL.md': form.value.skill } }
  else if (kind.value === 'mcp') spec = { url: form.value.url, transport: 'http' }
  else spec = { provider: 'openai', endpoint: form.value.endpoint.trim().replace(/\/+$/, ''), model_id: form.value.model_id, max_output_tokens: editing.value?.versions[0].spec.max_output_tokens || 4096 }
  try {
    const resource = await api<Resource>(editing.value ? `/resources/${editing.value.id}/versions` : `/spaces/${state.spaceId}/resources/${kind.value}`,
      'POST', { name: form.value.name, description: form.value.description, spec,
        ...(kind.value === 'model' && modelCredential.value ? { credential: modelCredential.value } : {}) })
    if (generation !== pageGeneration) return
    closeForm(); await load()
    if (generation === pageGeneration) showDetail(resource)
  } catch (e) { if (generation === pageGeneration) error.value = (e as Error).message }
  finally { if (generation === pageGeneration) busy.value = false }
}
function showDetail(resource: Resource) {
  detail.value = resource; detailVersion.value = resource.versions[0].version; memberSelection.value = []
}
async function discover(resource: Resource) {
  if (busy.value) return
  busy.value = true; error.value = ''
  const generation = pageGeneration
  try {
    const result = await api<Resource>(`/resources/${resource.id}/discover`, 'POST')
    if (generation !== pageGeneration) return
    showDetail(result); await load()
  } catch (e) { if (generation === pageGeneration) error.value = (e as Error).message }
  finally { if (generation === pageGeneration) busy.value = false }
}
async function enableMembers() {
  if (!detail.value || busy.value) return
  busy.value = true; error.value = ''
  const generation = pageGeneration
  try {
    const result = await api<Resource>(`/resources/${detail.value.id}/enable-members`, 'POST',
      { discovery_version: detailVersion.value, members: memberSelection.value })
    if (generation !== pageGeneration) return
    showDetail(result); await load()
  } catch (e) { if (generation === pageGeneration) error.value = (e as Error).message }
  finally { if (generation === pageGeneration) busy.value = false }
}
async function saveCredential() {
  if (!credentialFor.value || busy.value) return
  busy.value = true; error.value = ''
  const generation = pageGeneration
  try {
    await api(`/resources/${credentialFor.value.id}/credential`, 'PUT', { secret: secret.value })
    if (generation !== pageGeneration) return
    credentialFor.value = undefined; secret.value = ''; detail.value = undefined; await load()
  } catch (e) { if (generation === pageGeneration) error.value = (e as Error).message }
  finally { if (generation === pageGeneration) busy.value = false }
}
watch(kind, () => { pageGeneration++; busy.value = false; closeForm(); detail.value = undefined; credentialFor.value = undefined; secret.value = ''; search.value = ''; category.value = 'all'; error.value = ''; load() }, { immediate: true })
onBeforeUnmount(() => { pageGeneration++; loadGeneration++; closeForm(); secret.value = '' })
</script>
<template>
  <div class="page-head"><div><h1>{{ title }}</h1><p class="muted">{{ kind === 'tool' ? '管理预置工具、外部集成和自定义工具。' : kind === 'skill' ? '导入技能说明与只读参考文件，在 Agent 能力配置中选择。' : kind === 'mcp' ? '添加连接、发现工具，再选择允许使用的成员。' : '连接模型服务，自动获取可用模型，管理团队的运行环境。' }}</p></div>
    <button v-if="isAdmin" class="primary" @click="openForm()"><Plus :size="17" />{{ addLabel }}</button></div>
  <p v-if="error && !modal && !credentialFor" role="alert" class="error">{{ error }}</p>
  <section class="resource-panel" :aria-label="kind === 'model' ? '模型连接' : title">
  <div class="resource-toolbar"><div class="row"><h2>{{ kind === 'model' ? '模型连接' : '资源列表' }}</h2><span class="count-badge">{{ resources.length }}</span></div>
    <div class="row resource-filters"><div class="search-field"><Search :size="16" /><input v-model="search" type="search" aria-label="搜索资源" :placeholder="kind === 'model' ? '搜索连接或模型' : '搜索名称、标识或用途'"></div><select v-if="kind === 'tool'" v-model="category" aria-label="工具分类"><option value="all">全部分类</option><option v-for="(label, key) in categories" :key="key" :value="key">{{ label }}</option></select></div>
  </div>
  <div class="resource-table-head" :class="{ 'model-columns': kind === 'model' }" aria-hidden="true"><span>{{ kind === 'model' ? '连接名称 / API 地址' : '资源名称' }}</span><span v-if="kind === 'model'">当前模型</span><span>{{ kind === 'model' ? '连接配置' : '状态 / 版本' }}</span><span>操作</span></div>
  <p v-if="loading" class="resource-loading muted" role="status">正在读取资源…</p><div v-else-if="!visible.length" class="empty resource-empty"><Server v-if="kind === 'model'" :size="28" /><h2>{{ search ? '没有匹配的资源' : kind === 'model' ? '连接你的第一个模型服务' : '还没有资源' }}</h2><p class="muted">{{ kind === 'model' ? '填写 API 地址与密钥，即可获取模型列表并选择。' : isAdmin ? '添加资源后，编辑者即可在应用配置中选择。' : '请联系空间管理员添加资源。' }}</p><button v-if="isAdmin && !search" class="primary" @click="openForm()"><Plus :size="16" />{{ addLabel }}</button></div>
  <div v-else class="resource-list">
    <article v-for="resource in visible" :key="resource.id" :aria-label="resource.name" class="resource-row" :class="{ 'model-columns': kind === 'model' }">
      <div class="resource-name"><span v-if="kind === 'model'" class="connection-icon"><Server :size="19" /></span><div><h3>{{ resource.name }}</h3><code v-if="resource.kind === 'tool'">{{ resource.versions[0].spec.registry_key }}</code><p v-if="kind === 'model'" class="muted endpoint-text">{{ resource.versions[0].spec.endpoint }}</p><p v-else class="muted">{{ resource.description }}</p></div></div>
      <div v-if="kind === 'model'" class="model-cell"><code>{{ resource.versions[0].spec.model_id }}</code><span class="muted">v{{ resource.versions[0].version }} · {{ resource.versions.length }} 个版本</span></div>
      <div><span class="badge" :class="{ good: resource.versions[0].status === 'ready' }">{{ kind === 'model' ? '已配置' : statusLabel(resource.versions[0]) }}</span><p v-if="kind !== 'model'" class="muted">资源 v{{ resource.versions[0].version }} · {{ resource.versions.length }} 个版本</p>
        <p v-if="kind === 'model' || kind === 'mcp'" class="muted">凭据{{ resource.credential_configured ? '已设置' : '未设置' }}</p>
        <p v-if="kind === 'tool' && resource.versions[0].spec.category === 'files'" class="muted">{{ resource.versions[0].spec.writes ? '需要工作区读写权限' : '需要工作区读取权限' }}</p>
      </div>
      <div class="row resource-actions"><button v-if="kind === 'model' && isAdmin" class="text-link" @click="openForm(resource)">选择模型</button><button class="text-link" @click="showDetail(resource)">详情</button><button v-if="kind === 'mcp' && isAdmin" :disabled="busy" @click="discover(resource)">发现工具</button></div>
    </article>
  </div></section>
  <section v-if="kind === 'model'" class="environment-section"><div class="section-caption"><h2>运行环境</h2><span class="muted">在应用配置中按需启用</span></div><div class="environment-grid"><article class="environment-card"><FolderOpen :size="21" /><div><h3>工作空间</h3><p class="muted">支持只读或读写权限，文件可按任务清理或按会话保留。</p></div><span class="badge good">可配置</span></article><article class="environment-card"><Shield :size="21" /><div><h3>执行沙箱</h3><p class="muted">隔离执行命令与脚本，沙箱服务接入后可启用。</p></div><span class="badge">尚未接入</span></article></div></section>
  <div v-if="modal" class="modal-backdrop" :class="{ 'drawer-backdrop': kind === 'model' }"><form class="modal" :class="{ 'connection-drawer': kind === 'model' }" role="dialog" aria-modal="true" aria-labelledby="resource-title" @submit.prevent="save">
    <div class="row spread dialog-heading"><div><h2 id="resource-title">{{ editing ? kind === 'model' ? '配置模型连接' : '添加资源新版本' : addLabel }}</h2><p v-if="kind === 'model'" class="muted">填写连接信息，获取并选择可用模型。</p></div><button type="button" class="icon-button" aria-label="关闭配置" :disabled="busy" @click="closeForm"><X :size="20" /></button></div>
    <label>名称<input v-model="form.name" required maxlength="100" :readonly="!!editing"></label>
    <label>用途描述<textarea v-model="form.description" maxlength="2000" rows="2" :readonly="!!editing"></textarea></label>
    <label v-if="kind === 'tool'">受信工具实现<select v-model="form.registry_key"><option v-for="resource in readyImplementations" :key="resource.id" :value="resource.versions[0].spec.registry_key">{{ resource.name }} · {{ resource.versions[0].spec.registry_key }}</option></select></label>
    <template v-if="kind === 'skill'"><label>选择技能包目录<input type="file" webkitdirectory multiple @change="upload"></label><label>SKILL.md<textarea v-model="form.skill" rows="10" required placeholder="---&#10;name: report&#10;description: 报告写作规范&#10;---&#10;请按以下规则编写报告…"></textarea></label><small class="muted">包含 {{ Object.keys(skillFiles).length }} 个参考文件。基础导入支持 UTF-8 文件，暂不支持 Git 拉取；脚本只作为参考文件保存，不执行。</small></template>
    <label v-if="kind === 'mcp'">MCP 服务地址<input v-model="form.url" type="url" required placeholder="https://mcp.example.com/mcp"></label>
    <template v-if="kind === 'model'">
      <div class="form-section-label">服务连接</div>
      <label>API 地址<input v-model="form.endpoint" type="url" required :readonly="!!editing" placeholder="https://api.example.com/v1" @change="fetchModels"><small class="muted">{{ editing ? '更换服务地址请添加新连接，原连接的应用配置继续保留。' : '填写兼容 Chat Completions 的基础地址，通常以 /v1 结尾。' }}</small></label>
      <label>API Key<input v-model="modelCredential" type="password" autocomplete="new-password" :placeholder="savedCredential ? '已保存，留空继续使用原密钥' : '填写服务密钥；无需认证的服务可留空'" @change="fetchModels"></label>
      <p class="muted credential-hint"><KeyRound :size="14" />{{ savedCredential ? '使用已保存的密钥获取模型，原密钥不会回显。' : '密钥仅由服务端使用，保存后加密存储。' }}</p>
      <div class="form-section-label">选择模型</div>
      <div class="row spread"><span class="muted">{{ modelsLoading ? '正在获取模型…' : modelsLoaded ? `已获取 ${modelChoices.length} 个模型` : '填写连接信息后自动获取模型列表' }}</span><button type="button" class="text-link" :disabled="modelsLoading || busy || !form.endpoint" @click="fetchModels"><RefreshCw :size="14" :class="{ spinning: modelsLoading }" />{{ modelsLoading ? '获取中…' : '刷新模型' }}</button></div>
      <p v-if="modelError" role="alert" class="callout model-feedback">{{ modelError }}</p>
      <p v-else-if="modelsLoaded && !modelChoices.length" class="callout model-feedback">此连接没有返回可用模型，请检查服务权限或手动填写模型 ID。</p>
      <label v-if="manualModel">模型 ID<input v-model="form.model_id" required maxlength="100" placeholder="填写服务支持的完整模型 ID"></label>
      <label v-else>可用模型<select v-model="form.model_id" required :disabled="modelsLoading || !modelChoices.length"><option value="" disabled>{{ modelsLoading ? '正在加载…' : modelChoices.length ? '请选择模型' : '等待获取模型列表' }}</option><option v-for="item in modelChoices" :key="item.id" :value="item.id">{{ item.id }}</option></select></label>
      <button type="button" class="text-link" @click="manualModel = !manualModel; form.model_id = ''">{{ manualModel ? '从模型列表选择' : '手动输入模型 ID' }}</button>
      <p v-if="editing" class="muted model-version-note">保存为新版本，已发布应用仍使用原版本。</p>
    </template>
    <p v-if="error" class="error" role="alert">{{ error }}</p><div class="row end" :class="{ 'drawer-footer': kind === 'model' }"><button type="button" :disabled="busy" @click="closeForm">取消</button><button class="primary" :disabled="busy || (kind === 'model' && (modelsLoading || !form.model_id || (!manualModel && !modelsLoaded)))">{{ busy ? '保存中…' : kind === 'model' ? '保存连接' : '保存资源' }}</button></div>
  </form></div>
  <div v-if="detail && !modal && !credentialFor" class="modal-backdrop"><section class="modal wide" role="dialog" aria-modal="true" aria-labelledby="detail-title">
    <h2 id="detail-title">{{ detail.name }} · 详情与版本</h2><p>{{ detail.description }}</p>
    <label>查看资源版本<select v-model="detailVersion" @change="memberSelection = []"><option v-for="version in detail.versions" :key="version.version" :value="version.version">v{{ version.version }} · {{ statusLabel(version) }}</option></select></label>
    <template v-if="kind === 'skill'"><h3>包内文件</h3><ul><li v-for="name in Object.keys(selectedVersion?.spec.files || {})" :key="name">{{ name }}</li></ul><pre>{{ selectedVersion?.spec.files['SKILL.md'] }}</pre></template>
    <template v-else-if="kind === 'mcp'"><p class="muted">{{ selectedVersion?.spec.url }}</p><p v-if="selectedVersion?.status === 'pending'" class="callout">尚未发现工具，不能在 Agent 中选用。</p>
      <div v-for="member in selectedVersion?.spec.members || []" :key="member.name" class="resource-block"><label v-if="selectedVersion?.status === 'discovered'" class="check"><input v-model="memberSelection" type="checkbox" :value="member.name"><span>{{ member.name }}</span></label><h3 v-else>{{ member.name }}</h3><p class="muted">{{ member.description }}</p><details><summary>输入参数</summary><pre>{{ JSON.stringify(member.schema, null, 2) }}</pre></details></div>
      <button v-if="isAdmin && selectedVersion?.status === 'discovered'" class="primary" :disabled="busy || !memberSelection.length" @click="enableMembers">保存选定成员为新版本</button>
    </template><dl v-else-if="kind === 'model'"><dt>模型标识</dt><dd>{{ selectedVersion?.spec.model_id }}</dd><dt>API 地址</dt><dd>{{ selectedVersion?.spec.endpoint }}</dd><dt>凭据</dt><dd>{{ detail.credential_configured ? '已设置' : '未设置' }}</dd></dl>
    <dl v-else><dt>工具标识</dt><dd><code>{{ selectedVersion?.spec.registry_key }}</code></dd><dt>状态</dt><dd>{{ selectedVersion && statusLabel(selectedVersion) }}</dd></dl>
    <p v-if="error" class="error" role="alert">{{ error }}</p><div class="row end">
      <button v-if="isAdmin && (kind === 'model' || kind === 'mcp')" @click="credentialFor = detail; secret = ''; error = ''">设置凭据</button>
      <button v-if="isAdmin && (kind !== 'tool' || selectedVersion?.status === 'ready')" @click="openForm(detail)">添加新版本</button>
      <button @click="detail = undefined">关闭</button></div>
  </section></div>
  <div v-if="credentialFor" class="modal-backdrop"><form class="modal" role="dialog" aria-modal="true" aria-labelledby="credential-title" @submit.prevent="saveCredential"><h2 id="credential-title">设置 {{ credentialFor.name }} 的凭据</h2><label>API Key / Bearer Token<input v-model="secret" type="password" autocomplete="new-password" required></label><p class="muted">加密保存，页面不会回显原值。MCP 无需认证时可以不设置。</p><p v-if="error" class="error" role="alert">{{ error }}</p><div class="row end"><button type="button" :disabled="busy" @click="credentialFor = undefined; secret = ''">取消</button><button class="primary" :disabled="busy">保存凭据</button></div></form></div>
</template>
