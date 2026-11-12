import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import type { AssistantModelOptions } from '../assistants/types'
import type { StagePanelProps } from './stagePanels'

type Rule = {
  id: string
  name: string
  priority: number
  match_type: 'keyword' | 'regex'
  pattern: string
  target_entry_id: string | null
  target_name: string
  enabled: boolean
  created_at: string
}
type IntentConfig = {
  enabled: boolean
  model_id: string | null
  threshold: number
  margin: number
  recall_k: number
  faq_direct: boolean
  custom_prompt: { enabled: boolean; text: string }
  priority_rules_enabled: boolean
  rules: Rule[]
}
type Target = { id: string; kind: string; name: string; description: string; status: string; callable: boolean; reason: string | null }
type Status = { problems: { code: string; message: string; rule_id: string | null }[]; valid_rules: number; invalid_rules: number; in_scope: number }
type RuleTest = { valid: boolean; error: string | null; matched: boolean; keywords: string[] }

const DEFAULTS: IntentConfig = {
  enabled: false, model_id: null, threshold: 0.7, margin: 0.15, recall_k: 8, faq_direct: true, custom_prompt: { enabled: false, text: '' },
  priority_rules_enabled: false, rules: [],
}
const MAX_RULES = 50

function newRule(priority: number): Rule {
  return { id: crypto.randomUUID(), name: '', priority, match_type: 'keyword', pattern: '', target_entry_id: null, target_name: '', enabled: true, created_at: new Date().toISOString() }
}

/** OC · L2 intent judgement (OC-060 / OC-061, T-161): the classifier's thresholds and the priority rules that skip it. */
export function RoutingStagePanel({ assistantId, tenant, oc, readOnly, onChange }: StagePanelProps) {
  const { t } = useTranslation('orchestration')
  const config: IntentConfig = { ...DEFAULTS, ...((oc.intent ?? {}) as Partial<IntentConfig>) }
  const [models, setModels] = useState<AssistantModelOptions['models']>([])
  const [targets, setTargets] = useState<Target[]>([])
  const [status, setStatus] = useState<Status | null>(null)
  const [editing, setEditing] = useState<Rule | null>(null)
  const set = (next: Partial<IntentConfig>) => onChange({ ...oc, intent: { ...config, ...next } })
  const off = readOnly || !config.enabled

  const loadScope = useCallback(() => {
    apiRequest<{ items: Target[] }>(`/api/v1/assistants/${assistantId}/oc/routing/targets`, {}, tenant.id).then((data) => setTargets(data.items)).catch(() => undefined)
    apiRequest<Status>(`/api/v1/assistants/${assistantId}/oc/routing/status`, {}, tenant.id).then(setStatus).catch(() => undefined)
  }, [assistantId, tenant.id])

  useEffect(() => {
    let active = true
    apiRequest<AssistantModelOptions>('/api/v1/assistants/model-options', {}, tenant.id).then((options) => active && setModels(options.models)).catch(() => undefined)
    return () => { active = false }
  }, [tenant.id])
  useEffect(loadScope, [loadScope])

  const invalidIds = new Set((status?.problems ?? []).map((problem) => problem.rule_id))
  const targetName = (rule: Rule) => targets.find((target) => target.id === rule.target_entry_id)?.name ?? rule.target_name

  function saveRule(rule: Rule) {
    const target = targets.find((item) => item.id === rule.target_entry_id)
    const next = { ...rule, target_name: target?.name ?? rule.target_name }
    const exists = config.rules.some((item) => item.id === rule.id)
    set({ rules: exists ? config.rules.map((item) => (item.id === rule.id ? next : item)) : [...config.rules, next] })
    setEditing(null)
  }

  return (
    <div className="oc-rewrite-panel">
      <p className="usage-hint">{t('routing.hint')}</p>
      <label className="governance-inherit">
        <input checked={config.enabled} disabled={readOnly} onChange={(event) => set({ enabled: event.target.checked })} type="checkbox" />
        <span><strong>{t('routing.enable')}</strong><small>{t('routing.enableHint', { count: status?.in_scope ?? 0 })}</small></span>
      </label>
      <label className="rewrite-field">{t('routing.model')}
        <select disabled={off} onChange={(event) => set({ model_id: event.target.value || null })} value={config.model_id ?? ''}>
          <option value="">{t('routing.modelDefault')}</option>
          {models.map((model) => <option key={model.id} value={model.id}>{model.display_name}</option>)}
        </select>
        <small>{t('routing.modelHint')}</small>
      </label>
      <label className="rewrite-field">{t('routing.threshold', { value: config.threshold.toFixed(2) })}
        <input disabled={off} max={0.95} min={0.3} onChange={(event) => set({ threshold: Number(event.target.value) })} step={0.05} type="range" value={config.threshold} />
        <small>{t('routing.thresholdHint')}</small>
      </label>
      <label className="rewrite-field">{t('routing.margin', { value: config.margin.toFixed(2) })}
        <input disabled={off} max={0.5} min={0} onChange={(event) => set({ margin: Number(event.target.value) })} step={0.05} type="range" value={config.margin} />
        <small>{t('routing.marginHint')}</small>
      </label>
      <label className="rewrite-field">{t('routing.recallK')}
        <input disabled={off} max={20} min={3} onChange={(event) => set({ recall_k: Math.min(20, Math.max(3, Number(event.target.value) || 8)) })} type="number" value={config.recall_k} />
      </label>
      <label className="governance-inherit">
        <input checked={config.faq_direct} disabled={off} onChange={(event) => set({ faq_direct: event.target.checked })} type="checkbox" />
        <span><strong>{t('routing.faqDirect')}</strong><small>{t('routing.faqDirectHint')}</small></span>
      </label>
      <label className="governance-inherit">
        <input checked={config.custom_prompt.enabled} disabled={off} onChange={(event) => set({ custom_prompt: { ...config.custom_prompt, enabled: event.target.checked } })} type="checkbox" />
        <span><strong>{t('routing.custom')}</strong><small>{t('routing.customHint')}</small></span>
      </label>
      {config.custom_prompt.enabled && (
        <label className="rewrite-field">
          <textarea aria-label={t('routing.custom')} disabled={off} maxLength={1000} onChange={(event) => set({ custom_prompt: { ...config.custom_prompt, text: event.target.value } })} rows={3} value={config.custom_prompt.text} />
          <small>{config.custom_prompt.text.length} / 1000</small>
        </label>
      )}

      <div className="panel rewrite-test">
        <h3>{t('routing.rules.title')}</h3>
        <label className="governance-inherit">
          <input checked={config.priority_rules_enabled} disabled={off} onChange={(event) => set({ priority_rules_enabled: event.target.checked })} type="checkbox" />
          <span><strong>{t('routing.rules.enable')}</strong><small>{t('routing.rules.enableHint')}</small></span>
        </label>
        {status && <p className="usage-hint">{t('routing.rules.counts', { valid: status.valid_rules, invalid: status.invalid_rules })}</p>}
        {config.rules.length === 0 && <p className="assistants-empty-filtered">{t('routing.rules.empty')}</p>}
        <ol className="routing-rules">
          {[...config.rules].sort((a, b) => a.priority - b.priority).map((rule) => (
            <li key={rule.id}>
              <input aria-label={t('routing.rules.enabled', { name: rule.name })} checked={rule.enabled} disabled={readOnly} onChange={(event) => set({ rules: config.rules.map((item) => (item.id === rule.id ? { ...item, enabled: event.target.checked } : item)) })} type="checkbox" />
              <strong>{rule.name}</strong>
              <span>#{rule.priority} · {t(`routing.rules.type.${rule.match_type}`)}: {rule.pattern}</span>
              <span>→ {targetName(rule) || t('routing.rules.noTarget')}</span>
              {invalidIds.has(rule.id) && <span className="form-error">{t('routing.rules.invalid')}</span>}
              {!readOnly && (
                <span>
                  <button className="text-button" onClick={() => setEditing(rule)} type="button">{t('routing.rules.edit')}</button>
                  <button className="text-button" onClick={() => set({ rules: config.rules.filter((item) => item.id !== rule.id) })} type="button">{t('routing.rules.remove')}</button>
                </span>
              )}
            </li>
          ))}
        </ol>
        {!readOnly && <button className="secondary-button" disabled={config.rules.length >= MAX_RULES} onClick={() => setEditing(newRule((config.rules.reduce((max, rule) => Math.max(max, rule.priority), 0)) + 10))} type="button">{t('routing.rules.add')}</button>}
      </div>
      {editing && <RuleDialog assistantId={assistantId} onCancel={() => setEditing(null)} onSave={saveRule} rule={editing} targets={targets} tenantId={tenant.id} />}
    </div>
  )
}

function RuleDialog({ rule, targets, assistantId, tenantId, onSave, onCancel }: { rule: Rule; targets: Target[]; assistantId: string; tenantId: string; onSave: (rule: Rule) => void; onCancel: () => void }) {
  const { t } = useTranslation('orchestration')
  const [draft, setDraft] = useState(rule)
  const [question, setQuestion] = useState('')
  const [test, setTest] = useState<RuleTest | null>(null)
  const [error, setError] = useState('')
  const target = targets.find((item) => item.id === draft.target_entry_id)
  const ready = draft.name.trim() && draft.pattern.trim() && target?.callable

  async function runTest() {
    setError('')
    try {
      setTest(await apiRequest<RuleTest>(`/api/v1/assistants/${assistantId}/oc/routing/rule-test`, { method: 'POST', body: JSON.stringify({ question, match_type: draft.match_type, pattern: draft.pattern }) }, tenantId))
    } catch (requestError) {
      setTest(null)
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
  }

  return (
    <div className="modal-backdrop confirm-backdrop" onClick={onCancel} role="presentation">
      <form aria-label={t('routing.rules.dialog')} className="create-modal" onClick={(event) => event.stopPropagation()} onSubmit={(event) => { event.preventDefault(); if (ready) onSave({ ...draft, name: draft.name.trim(), pattern: draft.pattern.trim() }) }} role="dialog">
        <h2>{t('routing.rules.dialog')}</h2>
        <label className="kb-field"><span>{t('routing.rules.name')}</span><input maxLength={80} onChange={(event) => setDraft({ ...draft, name: event.target.value })} value={draft.name} /></label>
        <label className="kb-field"><span>{t('routing.rules.priority')}</span><input max={100000} min={0} onChange={(event) => setDraft({ ...draft, priority: Math.max(0, Number(event.target.value) || 0) })} type="number" value={draft.priority} /><small>{t('routing.rules.priorityHint')}</small></label>
        <label className="kb-field"><span>{t('routing.rules.matchType')}</span>
          <select onChange={(event) => setDraft({ ...draft, match_type: event.target.value as Rule['match_type'] })} value={draft.match_type}>
            <option value="keyword">{t('routing.rules.type.keyword')}</option>
            <option value="regex">{t('routing.rules.type.regex')}</option>
          </select>
        </label>
        <label className="kb-field"><span>{t('routing.rules.pattern')}</span><input maxLength={500} onChange={(event) => setDraft({ ...draft, pattern: event.target.value })} value={draft.pattern} /><small>{draft.match_type === 'keyword' ? t('routing.rules.keywordHint') : t('routing.rules.regexHint')}</small></label>
        {test?.keywords.length ? <p>{test.keywords.map((word) => <span className="flows-tag" key={word}>{word}</span>)}</p> : null}
        <label className="kb-field"><span>{t('routing.rules.target')}</span>
          <select onChange={(event) => setDraft({ ...draft, target_entry_id: event.target.value || null })} value={draft.target_entry_id ?? ''}>
            <option value="">{t('routing.rules.pickTarget')}</option>
            {targets.map((item) => <option disabled={!item.callable} key={item.id} value={item.id}>{item.name}{item.reason ? ` (${t(`routing.rules.reason.${item.reason}`)})` : ''}</option>)}
          </select>
        </label>
        <div className="rewrite-test-form">
          <input aria-label={t('routing.rules.testQuestion')} maxLength={500} onChange={(event) => setQuestion(event.target.value)} placeholder={t('routing.rules.testQuestion')} value={question} />
          <button className="secondary-button" disabled={!question.trim() || !draft.pattern.trim()} onClick={() => void runTest()} type="button">{t('routing.rules.test')}</button>
        </div>
        {test && <p className={test.valid ? 'assistants-notice' : 'form-error'} role="status">{test.valid ? t(test.matched ? 'routing.rules.hit' : 'routing.rules.miss') : test.error}</p>}
        {error && <p className="form-error" role="alert">{error}</p>}
        <div className="confirm-actions">
          <button className="secondary-button" onClick={onCancel} type="button">{t('routing.rules.cancel')}</button>
          <button className="primary-button" disabled={!ready} type="submit">{t('routing.rules.save')}</button>
        </div>
      </form>
    </div>
  )
}
