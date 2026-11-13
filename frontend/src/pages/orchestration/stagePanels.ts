import type { ComponentType } from 'react'
import type { TenantMembership } from '../../types'
import { GlossaryStagePanel } from './GlossaryStagePanel'
import { MemoryStagePanel } from './MemoryStagePanel'
import { PiiStagePanel } from './PiiStagePanel'
import { RewriteStagePanel } from './RewriteStagePanel'
import { RoutingStagePanel } from './RoutingStagePanel'
import type { OcConfig, Stage } from './types'

export type StagePanelProps = {
  assistantId: string
  tenant: TenantMembership
  oc: OcConfig
  readOnly: boolean
  onChange: (next: OcConfig) => void
}

/**
 * The stage tasks (T-055 PII, T-056 memory, T-057 glossary / rewrite, T-058 routing, T-059 components) register their
 * editor here. A stage without one is shown as “即将开放” and its section of the OC is left untouched.
 */
export const STAGE_PANELS: Partial<Record<Stage, ComponentType<StagePanelProps>>> = { pii: PiiStagePanel, glossary: GlossaryStagePanel, rewrite: RewriteStagePanel, routing: RoutingStagePanel, memory: MemoryStagePanel }
