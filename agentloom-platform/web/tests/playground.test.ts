import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import DraftPlayground from '../src/components/DraftPlayground.vue'
import { api, ApiError } from '../src/api'

vi.mock('../src/api', () => ({
  api: vi.fn(), apiBase: '/api/v1',
  ApiError: class extends Error { constructor(public status: number, public code: string, message: string, public violations: any[] = []) { super(message) } },
}))
const mockedApi = vi.mocked(api)
let wrapper: VueWrapper | undefined
let records: any, previews: any[], nextSession: number
class Events extends EventTarget {
  static instances: Events[] = []
  onopen = null; onerror = null; close = vi.fn()
  constructor(public url: string) { super(); Events.instances.push(this) }
  emit(kind: string, value: any, seq = 1) { this.dispatchEvent(new MessageEvent(kind, { data: JSON.stringify(value), lastEventId: String(seq) })) }
}
const emptyHistory = (id: string, revision = 2) => ({ id, app_id: 'app', app_name: 'App', title: 'Test', draft_revision: revision, items: [], runs: [] })
const run = (status = 'queued') => ({ id: 'run', status, result: null, error: null, release_version: null, draft_revision: 2, cleanup_state: 'pending', model_calls: 0, tool_calls: 0, last_event_seq: 1 })
const button = (label: string) => wrapper!.findAll('button').find(item => item.attributes('aria-label') === label || item.text() === label)!
async function open(props = {}) {
  wrapper = mount(DraftPlayground, { props: { agentId: 'app', revision: 2, dirty: false, disabled: false, modelName: 'MiniMax', mode: 'react', prepare: async () => 2, ...props } })
  await flushPromises()
}
beforeEach(() => {
  vi.useFakeTimers(); vi.stubGlobal('EventSource', Events); Events.instances = []
  records = {}; previews = []; nextSession = 0
  mockedApi.mockImplementation(async (path, method, body: any) => {
    if (path === '/agents/app/previews') {
      if (method === 'POST') {
        const preview = { id: `session-${++nextSession}`, draft_revision: body.draft_revision, title: 'Test' }
        records[preview.id] = emptyHistory(preview.id, preview.draft_revision)
        return preview as any
      }
      return { items: previews } as any
    }
    const id = path.split('/')[2]
    if (path.endsWith('/history')) return structuredClone(records[id])
    if (path.endsWith('/inputs')) {
      records[id].runs = [run()]
      records[id].items = [{ id: 'input', role: 'user', content: body.content, run_id: 'run' }]
      return { run_id: 'run' } as any
    }
    if (path.endsWith('/stop')) { Object.values(records).forEach((h: any) => { h.runs[0].status = 'stopping' }); return {} as any }
    throw new Error('Unexpected API request ' + path)
  })
})
afterEach(() => { wrapper?.unmount(); wrapper = undefined; vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('draft playground', () => {
  it('waits for the saved revision, starts a private preview and never publishes', async () => {
    const prepare = vi.fn(async () => 3)
    await open({ dirty: true, prepare })
    await wrapper!.find('textarea').setValue('Try my latest config')
    await wrapper!.find('form').trigger('submit'); await flushPromises()
    expect(prepare).toHaveBeenCalledOnce()
    expect(mockedApi).toHaveBeenCalledWith('/agents/app/previews', 'POST', { draft_revision: 3 }, 3)
    expect(mockedApi.mock.calls.some(([path]) => path.includes('/releases'))).toBe(false)
    expect(wrapper!.text()).toContain('Try my latest config')
    expect(wrapper!.text()).toContain('排队中')
    expect(Events.instances[0].url).toBe('/api/v1/runs/run/events')
  })
  it('keeps the question when saving is interrupted by a newer edit', async () => {
    await open({ prepare: async () => null })
    await wrapper!.find('textarea').setValue('Do not lose me')
    await wrapper!.find('form').trigger('submit'); await flushPromises()
    expect((wrapper!.find('textarea').element as HTMLTextAreaElement).value).toBe('Do not lose me')
    expect(mockedApi.mock.calls.some(([, method]) => method === 'POST')).toBe(false)
  })
  it('renders tool and plan events, finishes with the real answer and closes its stream', async () => {
    await open({ mode: 'plan_executor' })
    await wrapper!.find('textarea').setValue('Write a file')
    await wrapper!.find('form').trigger('submit'); await flushPromises()
    const events = Events.instances[0]
    events.emit('tool', { call_id: 'call', name: 'write_file', role: 'writer', status: 'running' }, 2)
    events.emit('tool', { call_id: 'call', name: 'write_file', role: 'writer', status: 'completed' }, 3)
    events.emit('plan', { tasks: [{ executor: 'writer', instruction: 'Write the file', status: 'completed' }] }, 4)
    await flushPromises()
    records['session-1'].runs = [{ ...run('completed'), model_calls: 3, tool_calls: 1 }]
    records['session-1'].items.push({ id: 'answer', role: 'assistant', content: 'File written and verified', run_id: 'run' })
    events.emit('finished', {})
    await flushPromises()
    expect(wrapper!.text()).toContain('File written and verified')
    expect(wrapper!.text()).toContain('模型 3 次')
    expect(wrapper!.findAll('.test-tool')).toHaveLength(1)
    expect(wrapper!.find('.test-tool').text()).toContain('成功')
    expect(wrapper!.text()).toContain('Write the file')
    expect(events.close).toHaveBeenCalled()
    const count = mockedApi.mock.calls.length
    await vi.advanceTimersByTimeAsync(5000)
    expect(mockedApi.mock.calls.length).toBe(count)
  })
  it('restores an active preview after reload and supports stop', async () => {
    previews = [{ id: 'old', draft_revision: 2, title: 'Old test' }]
    records.old = { ...emptyHistory('old'), runs: [run('running')] }
    await open()
    expect(wrapper!.text()).toContain('执行中')
    expect(button('新建测试会话').attributes('disabled')).toBeDefined()
    await button('停止').trigger('click'); await flushPromises()
    expect(mockedApi).toHaveBeenCalledWith('/runs/run/stop', 'POST')
    expect(wrapper!.text()).toContain('停止中')
  })
  it('starts a new isolated session after configuration changes', async () => {
    previews = [{ id: 'old', draft_revision: 1, title: 'Old test' }]
    records.old = { ...emptyHistory('old', 1), items: [{ id: 'old-answer', role: 'assistant', content: 'Old answer', run_id: 'old-run' }] }
    await open()
    expect(wrapper!.text()).toContain('下次发送将开始新的测试会话')
    await wrapper!.find('textarea').setValue('Use revision 2')
    await wrapper!.find('form').trigger('submit'); await flushPromises()
    expect(mockedApi.mock.calls.find(([path]) => path.endsWith('/inputs'))?.[0]).toBe('/sessions/session-1/inputs')
    expect(wrapper!.text()).not.toContain('Old answer')
  })
  it('retains failed input and exposes configuration validation errors', async () => {
    const fallback = mockedApi.getMockImplementation()!
    mockedApi.mockImplementation(async (path, method, body, revision) => {
      if (method === 'POST') throw new ApiError(422, 'VALIDATION_FAILED', '请选择模型连接', [{ code: 'MODEL_REQUIRED', path: 'model', message: '请选择模型连接' }])
      return fallback(path, method, body, revision)
    })
    await open()
    await wrapper!.find('textarea').setValue('Keep this question')
    await wrapper!.find('form').trigger('submit'); await flushPromises()
    expect(wrapper!.text()).toContain('请选择模型连接')
    expect((wrapper!.find('textarea').element as HTMLTextAreaElement).value).toBe('Keep this question')
    expect(wrapper!.emitted('violations')?.at(-1)?.[0]).toEqual([{ code: 'MODEL_REQUIRED', path: 'model', message: '请选择模型连接' }])
  })
  it('ignores an old history response when a new test starts before that response arrives', async () => {
    previews = [{ id: 'old', draft_revision: 2, title: 'Previous test' }]
    records.old = { ...emptyHistory('old'), runs: [{ ...run('completed'), id: 'old-run' }] }
    await open()
    let resolve!: (value: any) => void
    const pending = new Promise(done => { resolve = done })
    const fallback = mockedApi.getMockImplementation()!
    mockedApi.mockImplementation(async (path, method, body, revision) => path === '/sessions/old/history' ? pending : fallback(path, method, body, revision))
    Events.instances[0].emit('finished', {}); await flushPromises()
    await button('新建测试会话').trigger('click')
    await wrapper!.find('textarea').setValue('New test question')
    await wrapper!.find('form').trigger('submit'); await flushPromises()
    expect(wrapper!.text()).toContain('New test question')
    expect(wrapper!.text()).toContain('排队中')
    resolve({ ...emptyHistory('old'), items: [{ id: 'old-answer', role: 'assistant', content: 'Outdated answer', run_id: 'old-run' }] })
    await flushPromises()
    expect(wrapper!.text()).not.toContain('Outdated answer')
    expect(wrapper!.text()).toContain('New test question')
  })
  it('clears private data on revocation and ignores late events after leaving', async () => {
    await open()
    await wrapper!.find('textarea').setValue('Private test')
    await wrapper!.find('form').trigger('submit'); await flushPromises()
    const events = Events.instances[0]
    events.emit('revoked', {}); await flushPromises()
    expect(wrapper!.text()).not.toContain('Private test')
    expect(wrapper!.text()).toContain('草稿测试权限已变更')
    expect(button('发送测试').attributes('disabled')).toBeDefined()
    wrapper!.unmount(); wrapper = undefined
    const count = mockedApi.mock.calls.length
    events.emit('finished', {}); await flushPromises()
    expect(mockedApi.mock.calls.length).toBe(count)
  })
})
