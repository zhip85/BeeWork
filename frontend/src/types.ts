

export type TenantMembership = {
  id: string
  name: string
  slug: string
  role: string
  custom_role_id: string | null
  custom_role_name: string | null
  organization_id: string | null
  permissions: string[]
  tenant_status: string
  access_mode: 'full' | 'read_only'
}

export type CurrentUser = {
  id: string
  email: string
  display_name: string
  is_platform_admin: boolean
  memberships: TenantMembership[]
}

export type TenantMember = {
  id: string
  email: string
  display_name: string
  role: string
  is_active: boolean
  status: 'active' | 'suspended'
  organization_id: string
  organization_name: string
  custom_role_id: string | null
  custom_role_name: string | null
}

export type MemberInvitation = {
  id: string
  email: string
  role: string
  custom_role_id: string | null
  custom_role_name: string | null
  organization_id: string
  organization_name: string
  status: 'pending' | 'accepted' | 'revoked' | 'expired'
  expires_at: string
}

export type CreatedMemberInvitation = MemberInvitation & { invitation_url: string }

export type PermissionModule = {
  key: string
  label: string
  actions: string[]
}

export type PermissionCatalog = {
  modules: PermissionModule[]
  action_labels: Record<string, string>
}

export type TenantRole = {
  key: string
  id: string | null
  name: string
  description: string
  scope: 'tenant' | 'organization'
  scope_organization_id: string | null
  permissions: Record<string, string[]>
  is_active: boolean
  is_builtin: boolean
  member_count: number
}

export type RoleDraft = {
  id: string | null
  name: string
  description: string
  scope: 'tenant' | 'organization'
  scope_organization_id: string
  permissions: Record<string, string[]>
  is_active: boolean
}

export type Organization = {
  id: string
  tenant_id: string
  parent_id: string | null
  name: string
  depth: number
  member_count: number
  child_count: number
  is_root: boolean
}

export type PlatformTenant = {
  id: string
  name: string
  slug: string
  status: string
  seat_limit: number
  plan: string
  region: string
  member_count: number
  admin_email: string | null
  admin_name: string | null
  trial_ends_at: string | null
  available_actions: TenantLifecycleActionOption[]
}

export type TenantLifecycleActionOption = {
  action: string
  label: string
  to_status: string
  requires_review: boolean
}

export type PlatformOperation = {
  id: string
  operation: string
  operation_label: string
  target_tenant_id: string
  target_tenant_name: string
  from_status: string
  to_status: string
  reason: string
  status: 'pending' | 'succeeded' | 'rejected'
  requested_by_id: string
  requested_by_name: string
  reviewed_by_name: string | null
  review_comment: string | null
  created_at: string
  expires_at: string
  reviewed_at: string | null
}

export type PlatformJob = {
  id: string
  kind: string
  payload: Record<string, unknown>
  tenant_id: string | null
  status: string
  attempts: number
  max_attempts: number
  run_after: string
  locked_by: string | null
  locked_at: string | null
  last_error: string | null
  idempotency_key: string | null
  created_at: string
  finished_at: string | null
}

export type TenantModelEnvironment = {
  tenant_id: string
  environment: 'development' | 'staging' | 'production'
  available_environments: Array<'development' | 'staging' | 'production'>
  plan: string
  region: string
  version: number
  updated_at: string | null
  models: Array<{
    id: string
    display_name: string
    provider: string
    model_type: string
    region: string
    input_price_per_million: string
  }>
  enabled_model_ids: string[]
  default_model_ids: Record<'chat' | 'embedding' | 'rerank', string | null>
  unavailable_enabled_model_ids: string[]
  gateway_sync_status: 'not_configured' | 'synced' | 'failed'
  gateway_error: string | null
}

export type PlatformModel = {
  id: string
  display_name: string
  provider: string
  model_type: string
  availability: string
  input_price_per_million: string
  output_price_per_million: string
  input_points_per_thousand: string
  output_points_per_thousand: string
  status: 'healthy' | 'canary' | 'disabled'
  provider_model_id: string
  endpoint_url: string
  credential_configured: boolean
  api_key_masked: string
  api_version: string | null
  region: string
  context_window: number | null
  max_output_tokens: number | null
  default_temperature: string | null
  rpm_limit: number | null
  fallback_model_id: string | null
  allowlist_tenant_ids: string[]
  gateway_sync_status: 'not_configured' | 'synced' | 'failed'
  gateway_error: string | null
  gateway_synced_at: string | null
  created_at: string
  updated_at: string
}

export type ModelDraft = {
  id: string | null
  display_name: string
  provider: string
  model_type: string
  availability: string
  input_price_per_million: string
  output_price_per_million: string
  input_points_per_thousand: string
  output_points_per_thousand: string
  status: PlatformModel['status']
  provider_model_id: string
  endpoint_url: string
  api_key: string
  api_version: string
  region: string
  context_window: string
  max_output_tokens: string
  default_temperature: string
  rpm_limit: string
  fallback_model_id: string
  allowlist_tenant_ids: string[]
}

export type PlatformOverview = {
  tenant_count: number
  active_tenant_count: number
  suspended_tenant_count: number
  member_count: number
  active_member_count: number
  platform_admin_count: number
}

export type IntegrationStatus = {
  key: string
  name: string
  purpose: string
  configured: boolean
  reachable: boolean | null
  detail: string | null
}

export type ModelTestResult = {
  ok: boolean
  model: string
  content: string | null
  latency_ms: number
  prompt_tokens: number | null
  completion_tokens: number | null
  access_only: boolean
  error: string | null
}
