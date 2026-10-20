import { computed, reactive } from 'vue'
import { api, setSession } from './api'
import type { Space, User } from './types'

export const state = reactive({ user: null as User | null, spaces: [] as Space[], spaceId: '', initialized: false })
export const space = computed(() => state.spaces.find(s => s.id === state.spaceId))
export const canEdit = computed(() => space.value && space.value.role !== 'viewer')
export const isAdmin = computed(() => space.value?.role === 'admin')
export async function loadIdentity() {
  state.user = await api<User>('/auth/me')
  setSession(state.user)
  state.spaces = (await api<{ items: Space[] }>('/spaces')).items
  const previous = localStorage.getItem('agentloom-space')
  state.spaceId = state.spaces.some(s => s.id === previous) ? previous! : state.spaces[0]?.id || ''
  state.initialized = true
}
export async function signOut() {
  await api('/auth/logout', 'POST')
  state.user = null; state.spaces = []; state.spaceId = ''; setSession(null)
}
