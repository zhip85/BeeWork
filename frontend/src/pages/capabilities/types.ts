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
