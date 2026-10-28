export type AssistantStatus = 'draft' | 'published' | 'disabled'
export type AssistantVisibility = 'own_org' | 'specified' | 'all'
export const ASSISTANT_CATEGORIES = ['customer_service', 'sales', 'internal', 'insurance'] as const
export type AssistantCategory = (typeof ASSISTANT_CATEGORIES)[number]

export type AssistantConfig = {
  dsl_version: 1
  prompt: string
  model_id: string | null
  driven_by: string | null
  default_language: 'zh-CN' | 'en'
  greeting: string
  suggested_questions: boolean
  feedback_enabled: boolean
  mounts: Record<string, string[]>
  oc: Record<string, unknown>
}

export type Assistant = {
  id: string
  name: string
  description: string
  organization_id: string
  organization_name: string
  category: AssistantCategory
  icon: string | null
  color: string | null
  status: AssistantStatus
  visibility: AssistantVisibility
  visible_org_ids: string[]
  config: AssistantConfig
  draft_version: string
  live_version_id: string | null
  owner_id: string | null
  created_by: string | null
  updated_by: string | null
  row_version: number
  created_at: string
  updated_at: string
  can_edit: boolean
}

export type AssistantSummary = {
  total: number
  published: number
  draft: number
  disabled: number
  monthly_conversations: number | null
  quota: { used: number; limit: number | null; policy: string }
}

export type AssistantModelOptions = {
  environment: string
  models: { id: string; display_name: string; provider: string; is_default: boolean }[]
  default_model_id: string | null
}

export type OrganizationOption = { id: string; name: string; depth: number }

export type AssistantPage = { items: Assistant[]; total: number; limit: number; offset: number }
