export type FlowKind = 'chatflow' | 'workflow'
export type FlowStatus = 'draft' | 'published' | 'disabled'
export type FlowIconName = 'brain' | 'assistant' | 'creative' | 'chat' | 'flow' | 'team' | 'global' | 'governance'

export type FlowSummary = {
  id: string
  kind: FlowKind
  name: string
  description: string
  icon: FlowIconName
  status: FlowStatus
  node_count: number
  has_draft_changes: boolean
  config_corrupted: boolean
  published_at: string | null
  live_version: string | null
  updated_at: string
  updated_by: string | null
  row_version: number
}

export type FlowNode = { id: string; type: string; name: string; instruction?: string; target_id?: string | null }

export type FlowDetail = FlowSummary & {
  config: { nodes?: FlowNode[] } & Record<string, unknown> | null
  published_config: Record<string, unknown> | null
  reference_count: number
  invalid_nodes: string[]
  versions_count: number
  can_edit: boolean
  can_publish: boolean
}

export type FlowVersion = { id: string; version: string; version_seq: number; change_note: string; published_by: string | null; published_at: string; is_live: boolean }
export type FlowPage = { items: FlowSummary[]; total: number }
export type FlowCounts = Record<FlowKind, number>
export type CheckIssue = { code: string; message: string }
export type FlowChecks = { ok: boolean; issues: CheckIssue[] }
export type FlowReferences = { total: number }

export const KINDS: FlowKind[] = ['chatflow', 'workflow']
export const DEFAULT_ICON: Record<FlowKind, FlowIconName> = { chatflow: 'chat', workflow: 'flow' }

export function parseKind(value: string | null): FlowKind {
  return KINDS.includes(value as FlowKind) ? (value as FlowKind) : 'chatflow'
}
