export type PortalConfig = {
  portal_name: string
  input_placeholder: string
  answer_style: 'concise' | 'balanced' | 'detailed'
  nav_search: boolean
  nav_projects: boolean
  nav_app_library: boolean
  nav_recent: boolean
  nav_pinned: boolean
  file_upload: boolean
  voice_input: boolean
  profile_edit: boolean
  personal_memory: boolean
  task_notifications: boolean
  delisted_history: 'readonly' | 'hidden'
  has_console: boolean
}

export type PortalSeat = {
  state: 'assigned' | 'requested' | 'unassigned'
  source: 'rule' | 'manual' | 'auto' | 'unlimited' | null
  auto_assigned: boolean
  can_request: boolean
  portal_name: string
}
