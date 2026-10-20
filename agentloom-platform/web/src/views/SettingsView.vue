<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api'
import { state, isAdmin } from '../state'
import type { Role } from '../types'
const members = ref<{id: string; username: string; role: Role}[]>([]), audit = ref<any[]>([])
const username = ref(''), password = ref(''), role = ref<Role>('viewer'), error = ref(''), busy = ref(false)
async function load() {
  if (!isAdmin.value) return
  try {
    members.value = (await api<{items: typeof members.value}>(`/spaces/${state.spaceId}/members`)).items
    audit.value = (await api<{items: any[]}>(`/spaces/${state.spaceId}/audit`)).items
  } catch (e) { error.value = (e as Error).message }
}
async function add() {
  busy.value = true; error.value = ''
  try { await api(`/spaces/${state.spaceId}/members`, 'POST', { username: username.value, password: password.value, role: role.value }); username.value = ''; password.value = ''; await load() }
  catch (e) { error.value = (e as Error).message } finally { busy.value = false }
}
async function setRole(id: string, event: Event) {
  const next = (event.target as HTMLSelectElement).value
  try { await api(`/spaces/${state.spaceId}/members/${id}`, 'PUT', { role: next }); await load() }
  catch (e) { error.value = (e as Error).message; await load() }
}
onMounted(load)
</script>
<template>
  <h1>空间管理</h1><p v-if="!isAdmin" class="error">仅空间管理员可访问。</p>
  <template v-else><p class="muted">维护团队成员、角色与操作记录。</p><p v-if="error" class="error" role="alert">{{ error }}</p>
    <div class="editor-layout"><section class="panel"><h2>成员与角色</h2><div v-for="member in members" :key="member.id" class="member-row"><strong>{{ member.username }}</strong><select :value="member.role" :aria-label="`${member.username} 的角色`" @change="setRole(member.id, $event)"><option value="viewer">使用者</option><option value="editor">编辑者</option><option value="admin">管理员</option></select></div></section>
      <form class="panel" @submit.prevent="add"><h2>添加团队账号</h2><label>用户名<input v-model="username" required maxlength="100" autocomplete="off"></label><label>初始密码<input v-model="password" type="password" required minlength="12" maxlength="256" autocomplete="new-password"></label><small class="muted">至少 12 个字符，通过团队安全渠道交付。</small><label>空间角色<select v-model="role"><option value="viewer">使用者</option><option value="editor">编辑者</option><option value="admin">管理员</option></select></label><button class="primary" :disabled="busy">创建账号</button></form></div>
    <section class="panel environment-note"><h2>最近操作</h2><p v-if="!audit.length" class="muted">还没有操作记录。</p><div v-for="entry in audit" :key="entry.id" class="audit-row"><span>{{ entry.action }}</span><code>{{ entry.target }}</code><small>{{ new Date(entry.created_at * 1000).toLocaleString() }}</small></div></section>
  </template>
</template>
