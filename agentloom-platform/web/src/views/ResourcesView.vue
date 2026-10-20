<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Plus } from 'lucide-vue-next'
import { api } from '../api'
import { state, isAdmin, canEdit } from '../state'
import type { Resource, ResourceVersion } from '../types'
const route = useRoute(), resources = ref<Resource[]>([]), search = ref(''), category = ref('all')
const error = ref(''), busy = ref(false), loading = ref(true), modal = ref(false), detail = ref<Resource>()
const detailVersion = ref(1), editing = ref<Resource>(), credentialFor = ref<Resource>(), secret = ref('')
const form = ref({ name: '', description: '', registry_key: 'read_file', url: '', model_id: '', endpoint: 'https://api.openai.com/v1', skill: '' })
const skillFiles = ref<Record<string, string>>({}), memberSelection = ref<string[]>([])
const kind = computed(() => String(route.params.kind))
const title = computed(() => ({ tool: 'Tool · 工具', skill: 'Skill · 技能', mcp: 'MCP · 服务', model: '模型与环境' }[kind.value] || '资源'))
const addLabel = computed(() => ({ tool: '注册工具', skill: '导入 Skill', mcp: '添加 MCP', model: '添加模型连接' }[kind.value]))
const categories: Record<string, string> = { files: '文件与工作区', execution: '命令与任务', integrations: '外部工具集成', orchestration: '子 Agent 编排' }
const readyImplementations = computed(() => [...new Map(resources.value.filter(r => r.versions[0].status === 'ready').map(r => [r.versions[0].spec.registry_key, r])).values()])
const visible = computed(() => resources.value.filter(r => (r.name + r.description + (r.versions[0].spec.registry_key || '')).includes(search.value)
  && (category.value === 'all' || r.versions[0].spec.category === category.value)))
const selectedVersion = computed(() => detail.value?.versions.find(v => v.version === detailVersion.value))
const statusLabel = (v: ResourceVersion) => ({ ready: '可用', pending: '待发现', discovered: '待选择成员', unavailable: '尚未接入' }[v.status] || v.status)
async function load() {
  loading.value = true
  if (!canEdit.value) { error.value = '当前角色不能访问资源管理'; loading.value = false; return }
  try { resources.value = (await api<{items: Resource[]}>(`/spaces/${state.spaceId}/resources?kind=${kind.value}`)).items }
  catch (e) { error.value = (e as Error).message } finally { loading.value = false }
}
function openForm(resource?: Resource) {
  editing.value = resource; error.value = ''; modal.value = true
  const spec = resource?.versions[0].spec || {}
  form.value = { name: resource?.name || '', description: resource?.description || '', registry_key: spec.registry_key || 'read_file',
    url: spec.url || '', model_id: spec.model_id || '', endpoint: spec.endpoint || 'https://api.openai.com/v1', skill: spec.files?.['SKILL.md'] || '' }
  skillFiles.value = { ...(spec.files || {}) }
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
  busy.value = true; error.value = ''
  let spec: any
  if (kind.value === 'tool') spec = { registry_key: form.value.registry_key }
  else if (kind.value === 'skill') spec = { files: { ...skillFiles.value, 'SKILL.md': form.value.skill } }
  else if (kind.value === 'mcp') spec = { url: form.value.url, transport: 'http' }
  else spec = { provider: 'openai', endpoint: form.value.endpoint, model_id: form.value.model_id, max_output_tokens: 4096 }
  try {
    const resource = await api<Resource>(editing.value ? `/resources/${editing.value.id}/versions` : `/spaces/${state.spaceId}/resources/${kind.value}`,
      'POST', { name: form.value.name, description: form.value.description, spec })
    modal.value = false; await load(); showDetail(resource)
  } catch (e) { error.value = (e as Error).message } finally { busy.value = false }
}
function showDetail(resource: Resource) {
  detail.value = resource; detailVersion.value = resource.versions[0].version; memberSelection.value = []
}
async function discover(resource: Resource) {
  busy.value = true; error.value = ''
  try { showDetail(await api<Resource>(`/resources/${resource.id}/discover`, 'POST')); await load() }
  catch (e) { error.value = (e as Error).message } finally { busy.value = false }
}
async function enableMembers() {
  if (!detail.value) return
  busy.value = true; error.value = ''
  try {
    showDetail(await api<Resource>(`/resources/${detail.value.id}/enable-members`, 'POST',
      { discovery_version: detailVersion.value, members: memberSelection.value }))
    await load()
  } catch (e) { error.value = (e as Error).message } finally { busy.value = false }
}
async function saveCredential() {
  if (!credentialFor.value) return
  busy.value = true; error.value = ''
  try {
    await api(`/resources/${credentialFor.value.id}/credential`, 'PUT', { secret: secret.value })
    credentialFor.value = undefined; secret.value = ''; detail.value = undefined; await load()
  } catch (e) { error.value = (e as Error).message } finally { busy.value = false }
}
watch(kind, () => { detail.value = undefined; search.value = ''; category.value = 'all'; error.value = ''; load() }, { immediate: true })
</script>
<template>
  <div class="page-head"><div><h1>{{ title }}</h1><p class="muted">{{ kind === 'tool' ? '管理预置工具、外部集成和自定义工具。' : kind === 'skill' ? '导入技能说明与只读参考文件，在 Agent 能力配置中选择。' : kind === 'mcp' ? '添加连接、发现工具，再选择允许使用的成员。' : '维护模型连接与凭据，供应用按固定版本选择。' }}</p></div>
    <button v-if="isAdmin" class="primary" @click="openForm()"><Plus :size="17" />{{ addLabel }}</button></div>
  <p v-if="error && !modal && !credentialFor" role="alert" class="error">{{ error }}</p>
  <div class="toolbar"><input v-model="search" type="search" aria-label="搜索资源" placeholder="搜索名称、标识或用途"><select v-if="kind === 'tool'" v-model="category" aria-label="工具分类"><option value="all">全部分类</option><option v-for="(label, key) in categories" :key="key" :value="key">{{ label }}</option></select><span class="muted">{{ visible.length }} 项资源</span></div>
  <p v-if="loading" class="muted">正在读取资源…</p><div v-else-if="!visible.length" class="empty"><h2>{{ search ? '没有匹配的资源' : '还没有资源' }}</h2><p class="muted">{{ isAdmin ? '添加资源后，编辑者即可在应用配置中选择。' : '请联系空间管理员添加资源。' }}</p></div>
  <section v-else class="resource-list">
    <article v-for="resource in visible" :key="resource.id" :aria-label="resource.name" class="resource-row">
      <div><h3>{{ resource.name }}</h3><code v-if="resource.kind === 'tool'">{{ resource.versions[0].spec.registry_key }}</code><p class="muted">{{ resource.description }}</p></div>
      <div><span class="badge" :class="{ good: resource.versions[0].status === 'ready' }">{{ statusLabel(resource.versions[0]) }}</span><p class="muted">资源 v{{ resource.versions[0].version }} · {{ resource.versions.length }} 个版本</p>
        <p v-if="kind === 'model' || kind === 'mcp'" class="muted">凭据{{ resource.credential_configured ? '已设置' : '未设置' }}</p>
        <p v-if="kind === 'tool' && resource.versions[0].spec.category === 'files'" class="muted">{{ resource.versions[0].spec.writes ? '需要工作区读写权限' : '需要工作区读取权限' }}</p>
      </div>
      <div class="row"><button @click="showDetail(resource)">详情</button><button v-if="kind === 'mcp' && isAdmin" :disabled="busy" @click="discover(resource)">发现工具</button></div>
    </article>
  </section>
  <section v-if="kind === 'model'" class="panel environment-note"><h2>运行环境</h2><p>工作区支持只读／读写、按任务清理／按会话保留，在应用运行环境中设置。</p><p class="muted">执行沙箱尚未接入，命令与脚本执行暂不可用。</p></section>
  <div v-if="modal" class="modal-backdrop"><form class="modal" role="dialog" aria-modal="true" aria-labelledby="resource-title" @submit.prevent="save">
    <h2 id="resource-title">{{ editing ? '添加资源新版本' : addLabel }}</h2>
    <label>名称<input v-model="form.name" required maxlength="100" :readonly="!!editing"></label>
    <label>用途描述<textarea v-model="form.description" maxlength="2000" rows="2" :readonly="!!editing"></textarea></label>
    <label v-if="kind === 'tool'">受信工具实现<select v-model="form.registry_key"><option v-for="resource in readyImplementations" :key="resource.id" :value="resource.versions[0].spec.registry_key">{{ resource.name }} · {{ resource.versions[0].spec.registry_key }}</option></select></label>
    <template v-if="kind === 'skill'"><label>选择技能包目录<input type="file" webkitdirectory multiple @change="upload"></label><label>SKILL.md<textarea v-model="form.skill" rows="10" required placeholder="---&#10;name: report&#10;description: 报告写作规范&#10;---&#10;请按以下规则编写报告…"></textarea></label><small class="muted">包含 {{ Object.keys(skillFiles).length }} 个参考文件。基础导入支持 UTF-8 文件，暂不支持 Git 拉取；脚本只作为参考文件保存，不执行。</small></template>
    <label v-if="kind === 'mcp'">MCP 服务地址<input v-model="form.url" type="url" required placeholder="https://mcp.example.com/mcp"></label>
    <template v-if="kind === 'model'"><p class="muted">支持 OpenAI 兼容的 Chat Completions 接口。连接保存后单独设置凭据。</p><label>API 地址<input v-model="form.endpoint" type="url" required></label><label>模型标识<input v-model="form.model_id" required placeholder="填写服务实际支持的模型 ID"></label></template>
    <p v-if="error" class="error" role="alert">{{ error }}</p><div class="row end"><button type="button" :disabled="busy" @click="modal = false">取消</button><button class="primary" :disabled="busy">{{ busy ? '保存中…' : '保存资源' }}</button></div>
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
