import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import EditorView from '../src/views/EditorView.vue'
import { state } from '../src/state'
import { api } from '../src/api'
import { definition, type Agent } from '../src/types'

vi.mock('../src/api', () => ({ api: vi.fn(), ApiError: class extends Error {}, setSession: vi.fn() }))
const mockedApi = vi.mocked(api)
let wrapper: VueWrapper | undefined

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}
function agent(id: string): Agent {
  return {
    id, name: id, mode: 'react', revision: 1, description: 'original', lifecycle: 'active',
    release_id: null, release_version: null,
    draft: {
      description: 'original', model: null, definition: definition(), executors: {},
      environment: { sandbox_enabled: false, workspace_access: 'read_write', retention: 'run' },
      execution: { model_calls: 20, tool_calls: 50, timeout_seconds: 300, max_tasks: 5 },
    },
  }
}
const button = (text: string) => wrapper!.findAll('button').find(item => item.text() === text)!
async function openEditor() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/apps/:id/config', component: EditorView },
      { path: '/apps', component: { template: '<div>apps</div>' } },
      { path: '/resources/:kind', component: { template: '<div>resources</div>' } },
    ],
  })
  await router.push('/apps/first/config')
  await router.isReady()
  wrapper = mount({ template: '<RouterView />' }, { global: { plugins: [router] } })
  await flushPromises()
  return router
}

beforeEach(() => {
  state.spaceId = 'space'
  state.spaces = [{ id: 'space', name: 'Space', role: 'admin' }]
  mockedApi.mockImplementation(async (path: string) => {
    if (path === '/spaces/space/resources' || path.endsWith('/releases') || path.endsWith('/previews')) return { items: [] } as any
    if (path.startsWith('/agents/')) return agent(path.split('/')[2]) as any
    throw new Error('Unexpected API request: ' + path)
  })
})
afterEach(() => { wrapper?.unmount(); wrapper = undefined; vi.restoreAllMocks() })

describe('editor persistence and navigation', () => {
  it('preserves edits made while a save is awaiting the server, and saves them on the next revision', async () => {
    const pending = deferred<Agent>()
    const savedBodies: { body: any; revision: number | undefined }[] = []
    const fallback = mockedApi.getMockImplementation()!
    mockedApi.mockImplementation(async (path, method, body, revision) => {
      if (method === 'PUT') {
        savedBodies.push({ body: JSON.parse(JSON.stringify(body)), revision })
        if (savedBodies.length === 1) return pending.promise as any
        return { ...agent('first'), revision: 3, draft: JSON.parse(JSON.stringify(body)) } as any
      }
      return fallback(path, method, body, revision)
    })
    await openEditor()
    await wrapper!.find('textarea').setValue('submitted description')
    await button('保存草稿').trigger('click')
    await wrapper!.find('textarea').setValue('new edit while saving')
    const saved = agent('first')
    saved.revision = 2
    saved.draft.description = 'submitted description'
    pending.resolve(saved)
    await flushPromises()
    expect((wrapper!.find('textarea').element as HTMLTextAreaElement).value).toBe('new edit while saving')
    expect(wrapper!.text()).toContain('有未保存修改')
    expect(button('保存草稿').attributes('disabled')).toBeUndefined()
    await button('保存草稿').trigger('click')
    await flushPromises()
    expect(savedBodies.map(item => [item.body.description, item.revision])).toEqual([
      ['submitted description', 1], ['new edit while saving', 2],
    ])
    expect(wrapper!.text()).not.toContain('有未保存修改')
  })

  it('protects unsaved changes when navigating to another application using the same route component', async () => {
    const router = await openEditor()
    await wrapper!.find('textarea').setValue('keep this draft')
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    await router.push('/apps/second/config')
    await flushPromises()
    expect(confirm).toHaveBeenCalledOnce()
    expect(router.currentRoute.value.params.id).toBe('first')
    expect((wrapper!.find('textarea').element as HTMLTextAreaElement).value).toBe('keep this draft')
    confirm.mockReturnValue(true)
    await router.push('/apps/second/config')
    await flushPromises()
    expect(router.currentRoute.value.params.id).toBe('second')
    expect(wrapper!.find('h1').text()).toBe('second')
  })

  it('warns before a browser reload only when the draft has unsaved changes', async () => {
    await openEditor()
    const clean = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(clean)
    expect(clean.defaultPrevented).toBe(false)
    await wrapper!.find('textarea').setValue('unsaved')
    const dirty = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(dirty)
    expect(dirty.defaultPrevented).toBe(true)
  })

  it('does not validate a newer edit that arrived during the automatic save', async () => {
    const pending = deferred<Agent>()
    const fallback = mockedApi.getMockImplementation()!
    mockedApi.mockImplementation(async (path, method, body, revision) =>
      method === 'PUT' ? pending.promise as any : fallback(path, method, body, revision))
    await openEditor()
    await wrapper!.find('textarea').setValue('submitted')
    await button('校验配置').trigger('click')
    await wrapper!.find('textarea').setValue('still editing')
    const saved = agent('first')
    saved.revision = 2; saved.draft.description = 'submitted'
    pending.resolve(saved)
    await flushPromises()
    expect(mockedApi.mock.calls.some(([path]) => path.endsWith('/validate'))).toBe(false)
    expect(button('发布…').attributes('disabled')).toBeDefined()
    expect(wrapper!.text()).toContain('有新的未保存修改')
  })

  it('never applies a late application load to a different editor', async () => {
    const pending = deferred<Agent>()
    const fallback = mockedApi.getMockImplementation()!
    mockedApi.mockImplementation(async (path, method, body, revision) =>
      path === '/agents/first' ? pending.promise as any : fallback(path, method, body, revision))
    const router = await openEditor()
    await router.push('/apps/second/config')
    await flushPromises()
    pending.resolve(agent('first'))
    await flushPromises()
    expect(wrapper!.find('h1').text()).toBe('second')
  })
})
