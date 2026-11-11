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
