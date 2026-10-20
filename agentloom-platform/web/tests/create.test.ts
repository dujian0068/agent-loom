import { afterEach, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import AgentsView from '../src/views/AgentsView.vue'
import { api } from '../src/api'
import { state } from '../src/state'

vi.mock('../src/api', () => ({ api: vi.fn(), setSession: vi.fn() }))
let wrapper: VueWrapper | undefined
afterEach(() => wrapper?.unmount())

it('requires an explicit mode and sends name, description and selected mode together', async () => {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/apps', component: AgentsView },
      { path: '/apps/:id/config', component: { template: '<div>Configuration</div>' } },
    ],
  })
  state.spaceId = 'space'
  state.spaces = [{ id: 'space', name: 'Space', role: 'admin' }]
  vi.mocked(api).mockImplementation(async (_path, method) =>
    method === 'POST' ? { id: 'created' } as any : { items: [] } as any)
  await router.push('/apps')
  wrapper = mount({ template: '<RouterView />' }, { global: { plugins: [router] } })
  await flushPromises()
  await wrapper.findAll('button').find(item => item.text() === '新建应用')!.trigger('click')
  const modal = wrapper.find('form')
  await modal.find('input[type="text"], input:not([type])').setValue('Team assistant')
  await modal.find('textarea').setValue('Describe the application')
  expect(modal.findAll('input[type="radio"]').every(input => !(input.element as HTMLInputElement).checked)).toBe(true)
  await modal.trigger('submit')
  await flushPromises()
  expect(vi.mocked(api).mock.calls.filter(([, method]) => method === 'POST')).toHaveLength(0)
  expect(wrapper.text()).toContain('请选择 ReAct 或 Plan 执行模式')
  await modal.find('input[value="plan_executor"]').setValue(true)
  await modal.trigger('submit')
  await flushPromises()
  expect(vi.mocked(api).mock.calls.find(([, method]) => method === 'POST')?.[2]).toEqual({
    name: 'Team assistant', description: 'Describe the application', mode: 'plan_executor',
  })
  expect(router.currentRoute.value.path).toBe('/apps/created/config')
})
