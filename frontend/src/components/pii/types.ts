export type RuleMode = 'platform' | 'tenant' | 'assistant'
export type RuleLabel = 'platform' | 'platform_inherited' | 'tenant' | 'tenant_modified' | 'assistant' | 'assistant_modified'
export type MatchType = 'regex' | 'keyword' | 'classifier' | 'llm_judge'
export type RuleAction = 'mask' | 'block' | 'flag'
export type RuleStage = 'both' | 'input' | 'output'

/** One row of the rule table, the same shape at every layer. */
export type RuleRow = {
  key: string
  name: string
  match_type: MatchType
  pattern: string
  stage: RuleStage
  action: RuleAction
  block_message: string
  enabled: boolean
  enforced: boolean
  label: RuleLabel
  restorable: boolean
  deletable: boolean
  pending: boolean
  switchedOff?: boolean
}

export type RuleDraft = {
  key: string | null
  name: string
  match_type: MatchType
  pattern: string
  stage: RuleStage
  action: RuleAction
  block_message: string
  enabled: boolean
}

export const EMPTY_RULE: RuleDraft = { key: null, name: '', match_type: 'regex', pattern: '', stage: 'both', action: 'mask', block_message: '', enabled: true }
export const MATCH_TYPES: MatchType[] = ['regex', 'keyword', 'classifier', 'llm_judge']
export const ACTIONS: RuleAction[] = ['mask', 'block', 'flag']
