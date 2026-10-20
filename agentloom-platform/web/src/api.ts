import type { User } from './types'

declare global { interface Window { AGENTLOOM_CONFIG?: { apiBase: string } } }
export const apiBase = (window.AGENTLOOM_CONFIG?.apiBase || '/api/v1').replace(/\/$/, '')
let csrf = ''
export function setSession(user: User | null) { csrf = user?.csrf_token || '' }
export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public violations: any[] = []) {
    super(message)
  }
}
export async function api<T>(path: string, method = 'GET', body?: unknown, revision?: number): Promise<T> {
  const headers: Record<string, string> = {}
  if (method !== 'GET') {
    headers['Content-Type'] = 'application/json'
    headers['X-CSRF-Token'] = csrf
    headers['Idempotency-Key'] = crypto.randomUUID()
  }
  if (revision !== undefined) headers['If-Match'] = `"${revision}"`
  const options: RequestInit = { method, headers, credentials: 'include', body: body === undefined ? undefined : JSON.stringify(body) }
  let response: Response
  try { response = await fetch(apiBase + path, options) }
  catch {
    // The same key/body is reused when the first response was lost.
    try { response = await fetch(apiBase + path, options) }
    catch { throw new ApiError(0, 'NETWORK_ERROR', '连接失败，请检查网络后重试。输入内容仍保留。') }
  }
  const data = await response.json()
  if (!response.ok) {
    const error = data.error || {}
    throw new ApiError(response.status, error.code, error.message || '请求失败', error.violations || [])
  }
  return data as T
}
