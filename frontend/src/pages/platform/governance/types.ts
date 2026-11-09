export type MatchType = 'regex' | 'keyword' | 'classifier' | 'llm_judge'
export type RuleAction = 'mask' | 'block' | 'flag'

export type PiiRule = {
  id: string
  name: string
  match_type: MatchType
  pattern: string
  stage: 'both' | 'input' | 'output'
  action: RuleAction
  block_message: string
  enforced: boolean
  enabled: boolean
  row_version: number
  updated_by: string | null
  updated_at: string
  pending_request_id: string | null
}

export type Ruleset = { kind: 'all' | 'enforced' | 'rules'; rule_ids: string[] }

export type BaselineRule = {
  id: string
  name: string
  category: 'pii' | 'prompt_attack' | 'content_safety' | 'data_retention'
  stage: 'both' | 'input' | 'output' | 'data'
  level: 'enforced' | 'recommended'
  scope: 'all' | 'enterprise' | 'region_cn' | 'industry_finance'
  status: 'enabled' | 'draft'
  action: RuleAction
  match_type: MatchType
  pattern: string
  block_message: string
  ruleset: Ruleset | null
  row_version: number
  updated_by: string | null
  updated_at: string
  pending_request_id: string | null
}

export type ChangeRequest = {
  id: string
  kind: 'pii_rule' | 'baseline_rule'
  operation: 'create' | 'update' | 'delete'
  object_id: string | null
  object_name: string
  summary: string
  reason: string
  status: 'pending' | 'succeeded' | 'rejected' | 'expired'
  requested_by_id: string
  requested_by_name: string
  reviewed_by_name: string | null
  review_comment: string | null
  created_at: string
  expires_at: string
}

export const MATCH_TYPES: MatchType[] = ['regex', 'keyword', 'classifier', 'llm_judge']
export const ACTIONS: RuleAction[] = ['mask', 'block', 'flag']
