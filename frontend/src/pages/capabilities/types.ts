export type CapKind = 'skill' | 'mcp' | 'api'
export type CapStatus = 'draft' | 'available' | 'disabled'

export type McpTool = { name: string; description: string; read_only: boolean; selected: boolean }
export type McpDetail = {
  transport: string
  url: string
  host: string
  auth_type: 'none' | 'bearer' | 'oauth'
  token_mask: string | null
  oauth_authorize_url: string
  oauth_token_url: string
  oauth_client_id: string
  oauth_client_secret_mask: string | null
  oauth_scopes: string
  oauth_authorized: boolean
  oauth_expires_at: string | null
  tools: McpTool[]
  last_test_at: string | null
  last_test_ok: boolean
  last_test_error: string | null
  health: 'unknown' | 'ok' | 'error'
}

export type SkillCheck = { key: 'structure' | 'executables' | 'secrets' | 'dependencies'; status: 'pending' | 'running' | 'passed' | 'warning' | 'failed'; issues: Record<string, unknown>[] }
export type SkillDetail = { builtin: boolean; platform_active: boolean; filename: string; size_bytes: number; files: Record<string, unknown>[]; has_scripts: boolean; security_status: 'pending' | 'running' | 'passed' | 'failed'; checks: SkillCheck[]; checked_at: string | null; enable_warning: string | null }

export type Capability = {
  id: string
  kind: CapKind
  name: string
  description: string
  status: CapStatus
  meta: string
  pending_approval: boolean
  approval_id: string | null
  approval_warning: string | null
  updated_at: string
  row_version: number
  can_edit: boolean
  can_publish: boolean
  can_delete: boolean
  mcp: McpDetail | null
  skill: SkillDetail | null
}

export type TestResult = { ok: boolean; error: string | null; tool_count: number; capability: Capability | null }
export type CapCounts = Record<CapKind, number>
export const KINDS: CapKind[] = ['skill', 'mcp', 'api']
export const READY_KINDS: CapKind[] = ['skill', 'mcp']

export function parseKind(value: string | null): CapKind {
  return KINDS.includes(value as CapKind) ? (value as CapKind) : 'mcp'
}

// Chatflow / Workflow connections (T-075) — listed under their own tabs, served by /api/v1/flow-connections.
export type FlowKind = 'chatflow' | 'workflow'
export type FlowSource = 'native' | 'dify'
export const FLOW_KINDS: FlowKind[] = ['chatflow', 'workflow']
export type TabKind = CapKind | FlowKind
export const TABS: TabKind[] = ['skill', 'mcp', 'api', 'chatflow', 'workflow']
export const isFlowKind = (value: string | null): value is FlowKind => value === 'chatflow' || value === 'workflow'
export function parseTab(value: string | null): TabKind {
  return isFlowKind(value) ? value : parseKind(value)
}

export type FlowConnection = {
  id: string
  kind: FlowKind
  name: string
  description: string
  source_type: FlowSource
  flow_id: string | null
  flow_name: string | null
  base_url: string
  host: string
  app_id: string
  credential_mask: string | null
  input_mapping: Record<string, string>
  output_mapping: Record<string, string>
  streaming: boolean
  status: 'enabled' | 'disabled'
  state: 'available' | 'disabled' | 'invalid'
  state_reason: string | null
  last_test_at: string | null
  last_test_ok: boolean | null
  last_test_error: string | null
  last_test_ms: number | null
  references: number
  row_version: number
  can_edit: boolean
  can_delete: boolean
  updated_at: string
}
export type FlowConnectionList = { items: FlowConnection[]; total: number }
export type FlowTestResult = { ok: boolean; error: string | null; error_category: string | null; duration_ms: number | null; connection: FlowConnection }
export type AvailableFlow = { id: string; name: string; kind: FlowKind; version: number | null }
