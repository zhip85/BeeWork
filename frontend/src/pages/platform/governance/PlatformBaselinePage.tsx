import { useCallback, useEffect, useState } from 'react'
import { Lock, Pencil, Plus, Search, Trash2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { apiRequest, apiRequestWithStatus } from '../../../api'
import { ConfirmDialog } from '../../../components/ConfirmDialog'
import { RuleChangesPanel } from './RuleChangesPanel'
import { ACTIONS, MATCH_TYPES } from './types'
import type { BaselineRule, ChangeRequest, MatchType, PiiRule, RuleAction, Ruleset } from './types'

type List = { items: BaselineRule[]; total: number; counts: Record<string, number> }
type Draft = Omit<BaselineRule, 'id' | 'updated_by' | 'updated_at' | 'pending_request_id' | 'row_version'> & { id: string | null; row_version: number | null; reason: string }
const CATEGORIES = ['pii', 'prompt_attack', 'content_safety', 'data_retention'] as const
const LEVELS = ['enforced', 'recommended'] as const
const SCOPES = ['all', 'enterprise', 'region_cn', 'industry_finance'] as const
const STATUSES = ['all', 'enabled', 'draft'] as const
const EMPTY: Draft = {
  id: null, name: '', category: 'pii', stage: 'both', level: 'recommended', scope: 'all', status: 'draft', action: 'flag',
  match_type: 'keyword', pattern: '', block_message: '', ruleset: { kind: 'all', rule_ids: [] }, row_version: null, reason: '',
}

/** Security baseline (PLT-140–144): rules distributed to tenants by plan, region and industry. */
export function PlatformBaselinePage({ currentUserId }: { currentUserId: string }) {
  const { t } = useTranslation('governance')
  const [list, setList] = useState<List | null>(null)
  const [changes, setChanges] = useState<ChangeRequest[]>([])
  const [piiRules, setPiiRules] = useState<PiiRule[]>([])
  const [status, setStatus] = useState<(typeof STATUSES)[number]>('all')
  const [search, setSearch] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [draft, setDraft] = useState<Draft | null>(null)
  const [formError, setFormError] = useState('')
  const [saving, setSaving] = useState(false)
  const [deleting, setDeleting] = useState<BaselineRule | null>(null)

  const load = useCallback(() => {
    const params = new URLSearchParams()
    if (status !== 'all') params.set('status', status)
    if (search.trim()) params.set('q', search.trim())
    Promise.all([
      apiRequest<List>(`/api/v1/platform/baseline-rules?${params}`),
      apiRequest<ChangeRequest[]>('/api/v1/platform/rule-changes?status=pending'),
      apiRequest<{ items: PiiRule[] }>('/api/v1/platform/pii-rules'),
    ])
      .then(([rules, pending, pii]) => { setList(rules); setChanges(pending.filter((item) => item.kind === 'baseline_rule')); setPiiRules(pii.items); setError('') })
      .catch((requestError: Error) => setError(requestError.message || t('loadFailed')))
  }, [status, search, t])
  useEffect(() => {
    const timer = window.setTimeout(load, 200)
    return () => window.clearTimeout(timer)
  }, [load])

  function edit(rule: BaselineRule) {
    setFormError('')
    setDraft({ ...rule, ruleset: rule.ruleset ?? { kind: 'all', rule_ids: [] }, reason: '' })
  }

  async function send(body: Draft, rule: BaselineRule | null) {
    const payload = {
      name: body.name, category: body.category, stage: body.stage, level: body.level, scope: body.scope, status: body.status, action: body.action,
      match_type: body.match_type, pattern: body.pattern, block_message: body.block_message,
      ruleset: body.category === 'pii' ? body.ruleset : null, row_version: rule?.row_version ?? null, reason: body.reason,
    }
    const url = rule ? `/api/v1/platform/baseline-rules/${rule.id}` : '/api/v1/platform/baseline-rules'
    return (await apiRequestWithStatus(url, { method: rule ? 'PUT' : 'POST', body: JSON.stringify(payload) })).status === 202
  }

  async function save() {
    if (!draft) return
    setSaving(true)
    setFormError('')
    try {
      const existing = draft.id ? list?.items.find((item) => item.id === draft.id) ?? null : null
      const pending = await send(draft, existing)
      setNotice(t(pending ? 'submittedForReview' : 'ruleSaved'))
      setDraft(null)
      load()
    } catch (requestError) {
      setFormError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    } finally {
      setSaving(false)
    }
  }

  async function remove(rule: BaselineRule) {
    try {
      const result = await apiRequestWithStatus<ChangeRequest | undefined>(`/api/v1/platform/baseline-rules/${rule.id}`, { method: 'DELETE' })
      setNotice(t(result.status === 202 ? 'submittedForReview' : 'ruleDeleted'))
      load()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
  }

  function setCategory(category: Draft['category']) {
    if (!draft) return
    setDraft({
      ...draft, category,
      stage: category === 'data_retention' ? 'data' : draft.stage === 'data' ? 'both' : draft.stage,
      match_type: category === 'pii' ? 'regex' : category === 'data_retention' ? 'keyword' : draft.match_type,
    })
  }

  function toggleRule(id: string) {
    if (!draft?.ruleset) return
    const ids = draft.ruleset.rule_ids.includes(id) ? draft.ruleset.rule_ids.filter((item) => item !== id) : [...draft.ruleset.rule_ids, id]
    setDraft({ ...draft, ruleset: { ...draft.ruleset, rule_ids: ids } })
  }

  const stages = draft?.category === 'data_retention' ? (['data'] as const) : (['both', 'input', 'output'] as const)
  return (
    <section className="platform-governance-page">
      <div className="page-heading">
        <div>
          <p className="section-kicker">PLATFORM</p>
          <h1>{t('baseline.title')}</h1>
          <p>{t('baseline.subtitle')}</p>
        </div>
        <button className="primary-button" onClick={() => { setFormError(''); setDraft(EMPTY) }} type="button"><Plus size={15} /> {t('baseline.new')}</button>
      </div>
      {notice && <p className="tenant-success" role="status">{notice}</p>}
      {error && <p className="members-error" role="alert">{error}</p>}
      <RuleChangesPanel currentUserId={currentUserId} onDecided={(message) => { setNotice(message); load() }} onError={setError} requests={changes} />
      <div className="panel">
        <div className="knowledge-status-bar">
          {STATUSES.map((value) => (
            <button aria-pressed={status === value} className={status === value ? 'active' : ''} key={value} onClick={() => setStatus(value)} type="button">
              {t(`baseline.status.${value}`)} <strong>{list?.counts[value] ?? 0}</strong>
            </button>
          ))}
          <label className="search-field"><Search size={14} /><input aria-label={t('baseline.search')} onChange={(event) => setSearch(event.target.value)} placeholder={t('baseline.search')} value={search} /></label>
        </div>
        {list && list.items.length === 0 && <p className="assistants-empty-filtered">{t('baseline.empty')}</p>}
        {list && list.items.length > 0 && (
          <div className="members-table-wrap">
            <table className="members-table">
              <thead><tr><th>{t('col.rule')}</th><th>{t('col.category')}</th><th>{t('col.stage')}</th><th>{t('col.level')}</th><th>{t('col.scope')}</th><th>{t('col.status')}</th><th /></tr></thead>
              <tbody>
                {list.items.map((rule) => (
                  <tr className={rule.status === 'enabled' ? '' : 'governance-off'} key={rule.id}>
                    <td>
                      <span className="governance-name">{rule.level === 'enforced' && <Lock aria-label={t('enforced')} size={13} />}<strong>{rule.name}</strong></span>
                      <small>
                        {rule.category === 'pii' ? t(`ruleset.${rule.ruleset?.kind ?? 'all'}`, { count: rule.ruleset?.rule_ids.length ?? 0 })
                          : rule.category === 'data_retention' ? t('retentionDays', { days: rule.pattern }) : (rule.pattern || '—')}
                      </small>
                      {rule.pending_request_id && <small className="tenant-status-note">{t('pendingReview')}</small>}
                    </td>
                    <td>{t(`category.${rule.category}`)}</td>
                    <td>{t(`stage.${rule.stage}`)}</td>
                    <td><span className={`assistant-status ${rule.level === 'enforced' ? 'published' : 'draft'}`}>{t(`level.${rule.level}`)}</span></td>
                    <td>{t(`scope.${rule.scope}`)}</td>
                    <td><span className={`assistant-status ${rule.status === 'enabled' ? 'published' : 'draft'}`}>{t(`baseline.status.${rule.status}`)}</span></td>
                    <td className="member-actions-cell">
                      <button aria-label={t('edit')} className="icon-button" onClick={() => edit(rule)} title={t('edit')} type="button"><Pencil size={15} /></button>
                      <button aria-label={t('delete')} className="icon-button" disabled={!!rule.pending_request_id} onClick={() => setDeleting(rule)} title={t('delete')} type="button"><Trash2 size={15} /></button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {draft && (
        <div className="modal-backdrop" onClick={() => !saving && setDraft(null)} role="presentation">
          <form className="create-modal governance-modal" onClick={(event) => event.stopPropagation()} onSubmit={(event) => { event.preventDefault(); void save() }} role="dialog" aria-modal="true" aria-labelledby="baseline-title">
            <div className="section-kicker">PLATFORM</div>
            <h2 id="baseline-title">{t(draft.id ? 'baseline.editTitle' : 'baseline.new')}</h2>
            <label>{t('field.name')}<input autoFocus maxLength={80} onChange={(event) => setDraft({ ...draft, name: event.target.value })} required value={draft.name} /></label>
            <label>{t('col.category')}
              <select onChange={(event) => setCategory(event.target.value as Draft['category'])} value={draft.category}>
                {CATEGORIES.map((value) => <option key={value} value={value}>{t(`category.${value}`)}</option>)}
              </select>
            </label>
            <label>{t('field.stage')}
              <select disabled={draft.category === 'data_retention'} onChange={(event) => setDraft({ ...draft, stage: event.target.value as Draft['stage'] })} value={draft.stage}>
                {stages.map((value) => <option key={value} value={value}>{t(`stage.${value}`)}</option>)}
              </select>
            </label>
            <label>{t('col.level')}
              <select onChange={(event) => setDraft({ ...draft, level: event.target.value as Draft['level'] })} value={draft.level}>
                {LEVELS.map((value) => <option key={value} value={value}>{t(`level.${value}`)}</option>)}
              </select>
              <small>{t(`levelHint.${draft.level}`)}</small>
            </label>
            <label>{t('col.scope')}
              <select onChange={(event) => setDraft({ ...draft, scope: event.target.value as Draft['scope'] })} value={draft.scope}>
                {SCOPES.map((value) => <option key={value} value={value}>{t(`scope.${value}`)}</option>)}
              </select>
            </label>
            <label>{t('col.status')}
              <select onChange={(event) => setDraft({ ...draft, status: event.target.value as Draft['status'] })} value={draft.status}>
                {(['enabled', 'draft'] as const).map((value) => <option key={value} value={value}>{t(`baseline.status.${value}`)}</option>)}
              </select>
              <small>{t('statusHint')}</small>
            </label>
            {draft.category === 'pii' && (
              <fieldset className="governance-ruleset">
                <legend>{t('field.ruleset')}</legend>
                <small>{t('rulesetHint')}</small>
                {(['all', 'enforced', 'rules'] as const).map((kind) => (
                  <label className="governance-check" key={kind}>
                    <input checked={draft.ruleset?.kind === kind} name="ruleset" onChange={() => setDraft({ ...draft, ruleset: { kind, rule_ids: kind === 'rules' ? draft.ruleset?.rule_ids ?? [] : [] } as Ruleset })} type="radio" /> {t(`rulesetKind.${kind}`)}
                  </label>
                ))}
                {draft.ruleset?.kind === 'rules' && (
                  <div className="governance-rule-picker">
                    {piiRules.map((rule) => (
                      <label className="governance-check" key={rule.id}>
                        <input checked={draft.ruleset?.rule_ids.includes(rule.id) ?? false} onChange={() => toggleRule(rule.id)} type="checkbox" /> {rule.name}
                      </label>
                    ))}
                  </div>
                )}
              </fieldset>
            )}
            {draft.category === 'data_retention' && (
              <label>{t('field.retentionDays')}<input inputMode="numeric" max={3650} min={1} onChange={(event) => setDraft({ ...draft, pattern: event.target.value })} required type="number" value={draft.pattern} /><small>{t('retentionHint')}</small></label>
            )}
            {draft.category !== 'pii' && draft.category !== 'data_retention' && (
              <>
                <label>{t('field.matchType')}
                  <select onChange={(event) => setDraft({ ...draft, match_type: event.target.value as MatchType })} value={draft.match_type}>
                    {MATCH_TYPES.map((value) => <option key={value} value={value}>{t(`matchType.${value}`)}</option>)}
                  </select>
                </label>
                <label>{t('field.pattern')}<textarea className="governance-mono" onChange={(event) => setDraft({ ...draft, pattern: event.target.value })} rows={3} value={draft.pattern} /><small>{t(`patternHint.${draft.match_type}`)}</small></label>
                <label>{t('field.action')}
                  <select onChange={(event) => setDraft({ ...draft, action: event.target.value as RuleAction })} value={draft.action}>
                    {ACTIONS.map((value) => <option key={value} value={value}>{t(`action.${value}`)}</option>)}
                  </select>
                </label>
                {draft.action === 'block' && (
                  <label>{t('field.blockMessage')}<textarea maxLength={300} onChange={(event) => setDraft({ ...draft, block_message: event.target.value })} placeholder={t('field.blockMessagePlaceholder')} rows={2} value={draft.block_message} /></label>
                )}
              </>
            )}
            {(draft.level === 'enforced' || list?.items.find((item) => item.id === draft.id)?.level === 'enforced') && (
              <label>{t('field.reason')}<textarea maxLength={500} onChange={(event) => setDraft({ ...draft, reason: event.target.value })} rows={2} value={draft.reason} /><small>{t('field.reasonHint')}</small></label>
            )}
            {formError && <p className="members-error" role="alert">{formError}</p>}
            <div className="plans-actions">
              <button className="text-button" disabled={saving} onClick={() => setDraft(null)} type="button">{t('cancel')}</button>
              <button className="primary-button" disabled={saving || !draft.name.trim()} type="submit">{saving ? t('saving') : t('save')}</button>
            </div>
          </form>
        </div>
      )}
      {deleting && (
        <ConfirmDialog
          body={t(deleting.level === 'enforced' ? 'deleteEnforcedBody' : 'deleteBody', { name: deleting.name })} danger onCancel={() => setDeleting(null)}
          onConfirm={async () => { const target = deleting; setDeleting(null); await remove(target) }} title={t('deleteTitle')}
        />
      )}
    </section>
  )
}
