import { Bot, Brain, Globe, MessageSquare, ShieldCheck, Sparkles, Users, Workflow } from 'lucide-react'
import type { ComponentType } from 'react'
import type { FlowIconName } from './types'

export const FLOW_ICONS: Record<FlowIconName, ComponentType<{ size?: number }>> = {
  brain: Brain,
  assistant: Bot,
  creative: Sparkles,
  chat: MessageSquare,
  flow: Workflow,
  team: Users,
  global: Globe,
  governance: ShieldCheck,
}

export const ICON_ORDER = Object.keys(FLOW_ICONS) as FlowIconName[]

export function FlowIcon({ name, size = 18 }: { name: string; size?: number }) {
  const Icon = FLOW_ICONS[name as FlowIconName] ?? Brain
  return <Icon size={size} />
}
