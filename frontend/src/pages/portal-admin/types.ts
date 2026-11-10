export type AppStatus = 'draft' | 'listed' | 'delisted'
export type Audience = 'all' | 'orgs' | 'admins'

export type PortalApp = {
  id: string
  source_type: string
  source_id: string | null
  source_name: string | null
  source_state: 'available' | 'unavailable' | 'unset'
  name: string
  description: string
  category: string
  audience: Audience
  organization_ids: string[]
  organization_names: string[]
  mode: 'chat' | 'form'
  icon: string
  featured: boolean
  status: AppStatus
  listed_at: string | null
  delisted_reason: string | null
  updated_at: string
  row_version: number
  can_edit: boolean
  can_publish: boolean
}

export type SourceEntry = { source_type: string; source_id: string; name: string; description: string; modes: string[]; in_use: boolean }
export type Summary = { listed: number; draft: number; delisted: number; monthly_active_employees: number | null; monthly_calls: number | null }

export const PRESET_CATEGORIES = ['efficiency', 'service', 'sales', 'finance', 'legal'] as const
