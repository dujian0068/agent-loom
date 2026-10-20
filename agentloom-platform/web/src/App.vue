<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Boxes, MessageSquare, Wrench, BookOpen, Plug, Settings, Layers, LogOut, Menu } from 'lucide-vue-next'
import { state, space, canEdit, isAdmin, signOut } from './state'
const route = useRoute(), router = useRouter(), menu = ref(false), error = ref('')
const links = [
  { path: '/apps', text: '应用', icon: Boxes, management: false },
  { path: '/sessions', text: '我的会话', icon: MessageSquare, management: false },
  { path: '/resources/tool', text: 'Tool · 工具', icon: Wrench, management: true },
  { path: '/resources/skill', text: 'Skill · 技能', icon: BookOpen, management: true },
  { path: '/resources/mcp', text: 'MCP · 服务', icon: Plug, management: true },
  { path: '/resources/model', text: '模型与环境', icon: Layers, management: true },
]
watch(() => route.path, () => { menu.value = false })
async function changeSpace(event: Event) {
  const select = event.target as HTMLSelectElement
  const nextSpace = select.value
  select.value = state.spaceId
  const failure = await router.push('/apps')
  if (failure && route.path !== '/apps') return
  state.spaceId = nextSpace
  localStorage.setItem('agentloom-space', nextSpace)
}
async function logout() {
  try { await signOut(); await router.push('/login') } catch (e) { error.value = (e as Error).message }
}
</script>
<template>
  <RouterView v-if="route.path === '/login'" />
  <div v-else class="shell">
    <aside class="sidebar" :class="{ open: menu }">
      <RouterLink to="/apps" class="brand"><span class="mark">织</span><div>AgentLoom<small>团队工作台</small></div></RouterLink>
      <label class="space-label">当前空间<select :value="state.spaceId" @change="changeSpace" aria-label="当前空间">
        <option v-for="s in state.spaces" :key="s.id" :value="s.id">{{ s.name }}</option>
      </select></label>
      <p class="nav-label">工作台</p>
      <nav aria-label="空间导航">
        <template v-for="(link, i) in links" :key="link.path">
          <p v-if="i === 2 && canEdit" class="nav-label">资源与管理</p>
          <RouterLink v-if="!link.management || canEdit" :to="link.path" class="nav-link"
            :class="{ active: route.path.startsWith(link.path) }"><component :is="link.icon" :size="17" />{{ link.text }}</RouterLink>
        </template>
        <RouterLink v-if="isAdmin" to="/settings" class="nav-link" active-class="active"><Settings :size="17" />空间管理</RouterLink>
      </nav>
      <div class="person"><span class="avatar">{{ state.user?.username.slice(0, 1).toUpperCase() }}</span>
        <div><strong>{{ state.user?.username }}</strong><small>{{ { admin: '管理员', editor: '编辑者', viewer: '使用者' }[space?.role || 'viewer'] }}</small></div>
        <button class="icon-button" aria-label="退出登录" @click="logout"><LogOut :size="17" /></button>
      </div>
    </aside>
    <header class="topbar"><button class="icon-button mobile-menu" aria-label="导航" @click="menu = !menu"><Menu /></button>
      <span>{{ space?.name || '尚无空间' }} <span class="separator">/</span> {{ links.find(l => route.path.startsWith(l.path))?.text || '空间管理' }}</span>
      <span class="badge">开发版 · 0.1</span>
    </header>
    <main class="main"><p v-if="error" class="error" role="alert">{{ error }}</p>
      <RouterView v-if="state.spaceId" :key="state.spaceId" /><div v-else class="empty">当前账号没有可访问的空间，请联系管理员。</div>
    </main>
  </div>
</template>
