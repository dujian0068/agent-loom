export type Role = 'viewer' | 'editor' | 'admin'
export interface User { id: string; username: string; csrf_token: string }
export interface Space { id: string; name: string; role: Role }
export interface Binding { resource_id: string; version: number; members: string[] }
export interface Definition { name: string; description: string; prompt: string; capabilities: Binding[] }
export interface Draft {
  description: string
  model: Binding | null
  definition: Definition
  executors: Record<string, Definition>
  environment: { sandbox_enabled: boolean; workspace_access: 'read_only' | 'read_write'; retention: 'run' | 'session' }
  execution: { model_calls: number; tool_calls: number; timeout_seconds: number; max_tasks: number }
}
export interface Agent {
  id: string; name: string; mode: 'react' | 'plan_executor'; revision: number; description: string
  lifecycle: string; release_id: string | null; release_version: number | null; draft: Draft
}
export interface ResourceVersion { version: number; status: string; spec: Record<string, any> }
export interface Resource {
  id: string; name: string; description: string; kind: 'tool' | 'skill' | 'mcp' | 'model'
  enabled: boolean; credential_configured: boolean; versions: ResourceVersion[]
}
export interface Run {
  id: string; status: string; result: string | null; error: string | null; release_version: number
  cleanup_state: string; model_calls: number; tool_calls: number; last_event_seq: number
}
export interface History {
  id: string; app_id: string; app_name: string; title: string
  items: { id: string; role: string; content: string; run_id: string; status?: string }[]
  runs: Run[]
}
export interface Violation { code: string; path: string; message: string }
export const definition = (): Definition => ({ name: '', description: '', prompt: '', capabilities: [] })
export const terminal = (run?: Run) => !run || ['completed', 'failed', 'cancelled'].includes(run.status)
