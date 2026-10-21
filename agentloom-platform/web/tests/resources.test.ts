import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import ResourcesView from '../src/views/ResourcesView.vue'
import { api } from '../src/api'
import { state } from '../src/state'

vi.mock('../src/api', () => ({ api: vi.fn(), setSession: vi.fn() }))
let wrapper: VueWrapper | undefined
const existing = {
  id: 'connection', kind: 'model', name: 'Team models', description: 'Shared connection',
  enabled: true, credential_configured: true,
  versions: [{ version: 1, status: 'ready', spec: { endpoint: 'https://models.example/v1', model_id: 'chat-a', max_output_tokens: 2048 } }],
}
beforeEach(() => {
  state.spaceId = 'space'
  state.spaces = [{ id: 'space', name: 'Space', role: 'admin' }]
  vi.mocked(api).mockReset()
})
afterEach(() => wrapper?.unmount())

async function open(resources: unknown[] = []) {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/resources/:kind', component: ResourcesView }] })
  vi.mocked(api).mockImplementation(async path =>
    (path.endsWith('/model-catalog') ? { items: [{ id: 'chat-a' }, { id: 'chat-b' }] } : { items: resources }) as any)
  await router.push('/resources/model')
  wrapper = mount({ template: '<RouterView />' }, { global: { plugins: [router] } })
  await flushPromises()
  return router
}

async function click(text: string) {
  await wrapper!.findAll('button').find(button => button.text() === text)!.trigger('click')
  await flushPromises()
}

it('discovers models for an existing connection without exposing the stored key', async () => {
  await open([existing])
  await click('选择模型')
  const dialog = wrapper!.find('form[role="dialog"]')
  expect(vi.mocked(api).mock.calls.find(([path]) => path.endsWith('/model-catalog'))?.[2]).toEqual({
    endpoint: 'https://models.example/v1', resource_id: 'connection',
  })
  expect(dialog.find('input[type="password"]').element.value).toBe('')
  expect(dialog.findAll('select option').map(item => item.text())).toEqual(['请选择模型', 'chat-a', 'chat-b'])
  expect(dialog.find('select').element.value).toBe('chat-a')
})

it('creates the selected model and credential together instead of asking for a second save', async () => {
  await open()
  await click('添加模型连接')
  const dialog = wrapper!.find('form[role="dialog"]')
  await dialog.find('input:not([type])').setValue('New connection')
  await dialog.find('input[type="url"]').setValue('https://models.example/v1')
  await dialog.find('input[type="password"]').setValue('new-secret')
  await flushPromises()
  await dialog.find('select').setValue('chat-b')
  vi.mocked(api).mockImplementation(async (path, method) =>
    (method === 'POST' && path.endsWith('/resources/model') ? existing : { items: [existing] }) as any)
  await dialog.trigger('submit')
  await flushPromises()
  const saved = vi.mocked(api).mock.calls.find(([path, method]) => path.endsWith('/resources/model') && method === 'POST')
  expect(saved?.[2]).toMatchObject({
    name: 'New connection', credential: 'new-secret',
    spec: { endpoint: 'https://models.example/v1', model_id: 'chat-b', provider: 'openai' },
  })
  expect(vi.mocked(api).mock.calls.some(([, method]) => method === 'PUT')).toBe(false)
  expect(wrapper!.find('form[role="dialog"]').exists()).toBe(false)
})

it('ignores a delayed response from an old endpoint and does not reuse its saved key', async () => {
  await open([existing])
  let resolveOld!: (value: any) => void
  vi.mocked(api).mockImplementation(async () => new Promise(resolve => { resolveOld = resolve }))
  await click('选择模型')
  const dialog = wrapper!.find('form[role="dialog"]')
  vi.mocked(api).mockResolvedValue({ items: [{ id: 'new-chat' }] } as any)
  await dialog.find('input[type="url"]').setValue('https://new.example/v1')
  await flushPromises()
  resolveOld({ items: [{ id: 'old-chat' }] })
  await flushPromises()
  expect(dialog.text()).toContain('new-chat')
  expect(dialog.text()).not.toContain('old-chat')
  expect(vi.mocked(api).mock.calls.filter(([path]) => path.endsWith('/model-catalog')).at(-1)?.[2]).toEqual({ endpoint: 'https://new.example/v1' })
})

it('keeps entered fields on discovery failure and permits an explicit manual model', async () => {
  await open()
  await click('添加模型连接')
  vi.mocked(api).mockRejectedValue(new Error('服务未提供模型列表'))
  const dialog = wrapper!.find('form[role="dialog"]')
  await dialog.find('input:not([type])').setValue('Private gateway')
  await dialog.find('input[type="url"]').setValue('https://private.example/v1')
  await flushPromises()
  expect(dialog.text()).toContain('服务未提供模型列表')
  expect(dialog.find('input:not([type])').element.value).toBe('Private gateway')
  expect(dialog.findAll('button').find(button => button.text() === '保存连接')!.attributes('disabled')).toBeDefined()
  await click('手动输入模型 ID')
  await dialog.find('input[placeholder="填写服务支持的完整模型 ID"]').setValue('custom-chat')
  expect(dialog.findAll('button').find(button => button.text() === '保存连接')!.attributes('disabled')).toBeUndefined()
})

it('preserves token configuration when choosing a model in a new resource version', async () => {
  await open([existing])
  await click('选择模型')
  const dialog = wrapper!.find('form[role="dialog"]')
  await dialog.find('select').setValue('chat-b')
  vi.mocked(api).mockImplementation(async (_path, method) => (method === 'POST' ? existing : { items: [existing] }) as any)
  await dialog.trigger('submit')
  await flushPromises()
  expect(vi.mocked(api).mock.calls.find(([path]) => path.endsWith('/versions'))?.[2]).toEqual({
    name: existing.name, description: existing.description,
    spec: { provider: 'openai', endpoint: 'https://models.example/v1', model_id: 'chat-b', max_output_tokens: 2048 },
  })
})

it('closes the connection form when changing resource pages and discards stale results', async () => {
  const router = await open([existing])
  let resolveOld!: (value: any) => void
  vi.mocked(api).mockImplementation(async () => new Promise(resolve => { resolveOld = resolve }))
  await click('选择模型')
  vi.mocked(api).mockResolvedValue({ items: [] } as any)
  await router.push('/resources/skill')
  await flushPromises()
  resolveOld({ items: [{ id: 'old-chat' }] })
  await flushPromises()
  expect(wrapper!.find('form[role="dialog"]').exists()).toBe(false)
  expect(wrapper!.text()).toContain('Skill · 技能')
  expect(wrapper!.text()).not.toContain('old-chat')
})
