export type AgentKind = 'agent' | 'chatflow' | 'workflow'
export type AgentStatus = 'draft' | 'available' | 'disabled'
export type AgentIconName = 'brain' | 'assistant' | 'creative' | 'chat' | 'flow' | 'team' | 'global' | 'governance'

export type AgentSummary = {
  id: string
  kind: AgentKind
  is_team: boolean
  name: string
  description: string
  icon: AgentIconName
  status: AgentStatus
  model_name: string | null
  node_count: number
  has_draft_changes: boolean
  config_corrupted: boolean
  available_at: string | null
  updated_at: string
  updated_by: string | null
  row_version: number
}

export type AgentDetail = AgentSummary & {
  config: Record<string, unknown> | null
  available_config: Record<string, unknown> | null
  reference_count: number
  can_edit: boolean
  can_publish: boolean
}

export type AgentPage = { items: AgentSummary[]; total: number }
export type AgentCounts = Record<AgentKind, number>
export type CheckIssue = { code: string; message: string }
export type AgentChecks = { ok: boolean; issues: CheckIssue[] }
export type AgentReferences = { total: number }

export const KINDS: AgentKind[] = ['agent', 'chatflow', 'workflow']
export const DEFAULT_ICON: Record<AgentKind, AgentIconName> = { agent: 'brain', chatflow: 'chat', workflow: 'flow' }

export function parseKind(value: string | null): AgentKind {
  return KINDS.includes(value as AgentKind) ? (value as AgentKind) : 'agent'
}
