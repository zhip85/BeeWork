import { Bot, Brain, Globe, MessageSquare, ShieldCheck, Sparkles, Users, Workflow } from 'lucide-react'
import type { ComponentType } from 'react'
import type { AgentIconName } from './types'

export const AGENT_ICONS: Record<AgentIconName, ComponentType<{ size?: number }>> = {
  brain: Brain,
  assistant: Bot,
  creative: Sparkles,
  chat: MessageSquare,
  flow: Workflow,
  team: Users,
  global: Globe,
  governance: ShieldCheck,
}

export const ICON_ORDER = Object.keys(AGENT_ICONS) as AgentIconName[]

export function AgentIcon({ name, size = 18 }: { name: string; size?: number }) {
  const Icon = AGENT_ICONS[name as AgentIconName] ?? Brain
  return <Icon size={size} />
}
