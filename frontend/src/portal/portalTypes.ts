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

export type PortalAppEntry = { id: string; name: string; description: string; category: string; icon: string; featured: boolean; mode: string }
export type PortalConversation = {
  id: string; title: string; app_id: string | null; app_name: string | null; project_id: string | null
  pinned: boolean; last_active_at: string; readonly: boolean
}
export type PortalProject = { id: string; name: string; description: string; conversation_count: number; updated_at: string; last_used_at: string | null }
export type PortalMessage = { id: string; seq: number; role: 'user' | 'assistant'; content: string; citations: { document: string | null; snippet: string | null; kind: string }[]; created_at: string }
