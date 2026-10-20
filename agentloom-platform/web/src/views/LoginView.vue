<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api'
import { loadIdentity } from '../state'
const username = ref(''), password = ref(''), error = ref(''), busy = ref(false), router = useRouter()
async function login() {
  busy.value = true; error.value = ''
  try {
    await api('/auth/login', 'POST', { username: username.value, password: password.value })
    await loadIdentity(); await router.push('/apps')
  } catch (e) { error.value = (e as Error).message } finally { busy.value = false }
}
</script>
<template>
  <main class="login"><form class="panel" @submit.prevent="login">
    <div class="brand"><span class="mark">织</span>AgentLoom</div><h1>登录团队工作台</h1>
    <p class="muted">使用管理员为你创建的团队账号。</p>
    <label>用户名<input v-model="username" autocomplete="username" required maxlength="100"></label>
    <label>密码<input v-model="password" type="password" autocomplete="current-password" required maxlength="256"></label>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <button class="primary full" :disabled="busy">{{ busy ? '正在登录…' : '登录' }}</button>
  </form></main>
</template>
