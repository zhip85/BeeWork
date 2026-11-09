import type { VersionChange } from '../assistants/VersionDiff'

export const STAGES = ['pii', 'memory', 'glossary', 'rewrite', 'routing', 'components'] as const
export type Stage = (typeof STAGES)[number]
export const GROUPS = [
  { key: 'input', stages: ['pii'] },
  { key: 'context', stages: ['memory', 'glossary', 'rewrite'] },
  { key: 'execute', stages: ['routing'] },
  { key: 'output', stages: ['components'] },
] as const satisfies readonly { key: string; stages: readonly Stage[] }[]

export type OcConfig = Record<string, unknown>

export type StageSummary = { stage: Stage; index: number; group: string; available: boolean; summary: string | null }

export type OcState = {
  assistant: {
    id: string
    name: string
    icon: string | null
    color: string | null
    status: string
    draft_version: string
    live_version: string | null
    can_edit: boolean
  }
  oc: OcConfig
  live_oc: OcConfig | null
  row_version: number
  updated_at: string
  has_changes: boolean
  diff: VersionChange[]
  stages: StageSummary[]
  pending_approval_id: string | null
  approval: { required: boolean; levels: number; approver_role: string; pending_approval_id: string | null }
}

export type OcAssistantOption = { id: string; name: string; icon: string | null; color: string | null; status: string; version: string | null }
