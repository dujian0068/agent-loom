import { afterEach, expect, it, vi } from 'vitest'
import { api, setSession } from '../src/api'

afterEach(() => { vi.unstubAllGlobals(); setSession(null) })

it('retries a lost response with exactly the same body, CSRF token, revision and idempotency key', async () => {
  setSession({ id: 'test', username: 'test', csrf_token: 'csrf-fixture' })
  const fetch = vi.fn()
    .mockRejectedValueOnce(new TypeError('network disconnected after server commit'))
    .mockResolvedValueOnce(new Response(JSON.stringify({ revision: 3 }), { status: 200 }))
  vi.stubGlobal('fetch', fetch)
  expect(await api('/agents/test/draft', 'PUT', { description: 'saved' }, 2)).toEqual({ revision: 3 })
  expect(fetch).toHaveBeenCalledTimes(2)
  expect(fetch.mock.calls[1]).toEqual(fetch.mock.calls[0])
  expect(fetch.mock.calls[0][1]).toMatchObject({
    credentials: 'include', body: '{"description":"saved"}',
    headers: { 'If-Match': '"2"', 'X-CSRF-Token': 'csrf-fixture', 'Idempotency-Key': expect.any(String) },
  })
})

it('does not retry a server conflict and preserves its structured error for draft recovery', async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({
    error: { code: 'REVISION_CONFLICT', message: '草稿已被修改，请重新加载', violations: [] },
  }), { status: 409 }))
  vi.stubGlobal('fetch', fetch)
  await expect(api('/agents/test/draft', 'PUT', {}, 1)).rejects.toMatchObject({
    status: 409, code: 'REVISION_CONFLICT', message: '草稿已被修改，请重新加载',
  })
  expect(fetch).toHaveBeenCalledOnce()
})
