import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import App from './App.vue'
import Login from './views/LoginView.vue'
import Agents from './views/AgentsView.vue'
import Editor from './views/EditorView.vue'
import Resources from './views/ResourcesView.vue'
import Sessions from './views/SessionsView.vue'
import Settings from './views/SettingsView.vue'
import { state, loadIdentity } from './state'
import './style.css'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', component: Login },
    { path: '/', redirect: '/apps' },
    { path: '/apps', component: Agents },
    { path: '/apps/:id/config', component: Editor },
    { path: '/resources/:kind', component: Resources },
    { path: '/sessions/:id?', component: Sessions },
    { path: '/settings', component: Settings },
    { path: '/:pathMatch(.*)*', redirect: '/apps' },
  ],
})
router.beforeEach(async to => {
  if (!state.initialized) {
    try { await loadIdentity() } catch { state.initialized = true }
  }
  if (!state.user && to.path !== '/login') return '/login'
  if (state.user && to.path === '/login') return '/apps'
})
createApp(App).use(router).mount('#app')
