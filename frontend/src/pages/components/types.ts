export type ComponentType = 'card' | 'comparison' | 'timeline' | 'form'
export type Mode = 'native' | 'text' | 'json' | 'off'
export const TYPES: ComponentType[] = ['card', 'comparison', 'timeline', 'form']
export const MODES: Mode[] = ['native', 'text', 'json', 'off']
export const CHANNEL_CODES = ['website', 'h5', 'whatsapp', 'wechat', 'open-api'] as const

export type Compatibility = Record<string, Mode>

export type ComponentContent = {
  name_zh: string
  name_en: string
  type: ComponentType
  description: string
  trigger_tag: string
  data_schema: string
  sample_data: string
  compatibility: Compatibility
}

export type TenantComponent = ComponentContent & {
  id: string
  version: string
  status: 'enabled' | 'draft' | 'disabled'
  template_id: string | null
  template_name: string | null
  template_version: string | null
  template_latest: string | null
  used_by: string[]
  row_version: number
  can_delete: boolean
  updated_at: string
}

export type WidgetTemplate = ComponentContent & {
  id: string
  version: string
  status: 'published' | 'draft' | 'deprecated'
  copies: number
  updated_at: string
}

export type TypeDefaults = Record<ComponentType, { trigger_tag: string; data_schema: string; sample_data: string; compatibility: Compatibility }>
