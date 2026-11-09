import { useCallback, useEffect, useState } from 'react'
import { Lock, Pencil, Plus, Trash2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { apiRequest, apiRequestWithStatus } from '../../../api'
import { ConfirmDialog } from '../../../components/ConfirmDialog'
import { RuleChangesPanel } from './RuleChangesPanel'
import { ACTIONS, MATCH_TYPES } from './types'
import type { ChangeRequest, MatchType, PiiRule, RuleAction } from './types'

type List = { items: PiiRule[]; total: number; enforced: number; enabled: number }
type Draft = {
  id: string | null
  name: string
  match_type: MatchType
  pattern: string
  stage: PiiRule['stage']
  action: RuleAction
  block_message: string
  enforced: boolean
  enabled: boolean
  row_version: number | null
  reason: string
}
const EMPTY: Draft = { id: null, name: '', match_type: 'regex', pattern: '', stage: 'both', action: 'mask', block_message: '', enforced: false, enabled: true, row_version: null, reason: '' }

/** Platform PII rules (PLT-130–133): the platform layer every tenant and assistant inherits. */
export function PlatformPiiRulesPage({ currentUserId }: { currentUserId: string }) {
  const { t } = useTranslation('governance')
  const [list, setList] = useState<List | null>(null)
  const [changes, setChanges] = useState<ChangeRequest[]>([])
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [draft, setDraft] = useState<Draft | null>(null)
  const [formError, setFormError] = useState('')
  const [saving, setSaving] = useState(false)
  const [deleting, setDeleting] = useState<PiiRule | null>(null)

  const load = useCallback(() => {
    Promise.all([
      apiRequest<List>('/api/v1/platform/pii-rules'),
      apiRequest<ChangeRequest[]>('/api/v1/platform/rule-changes?status=pending'),
    ])
      .then(([rules, pending]) => { setList(rules); setChanges(pending.filter((item) => item.kind === 'pii_rule')); setError('') })
      .catch((requestError: Error) => setError(requestError.message || t('loadFailed')))
  }, [t])
  useEffect(() => { load() }, [load])

  function edit(rule: PiiRule) {
    setFormError('')
    setDraft({ ...rule, reason: '' })
  }

  async function send(body: Draft, rule: PiiRule | null) {
    const payload = {
      name: body.name, match_type: body.match_type, pattern: body.pattern, stage: body.stage, action: body.action,
      block_message: body.block_message, enforced: body.enforced, enabled: body.enabled, row_version: rule?.row_version ?? null, reason: body.reason,
    }
    const url = rule ? `/api/v1/platform/pii-rules/${rule.id}` : '/api/v1/platform/pii-rules'
    const result = await apiRequestWithStatus(url, { method: rule ? 'PUT' : 'POST', body: JSON.stringify(payload) })
    return result.status === 202
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

  async function toggle(rule: PiiRule) {
    setError('')
    try {
      const pending = await send({ ...rule, enabled: !rule.enabled, reason: '' }, rule)
      setNotice(t(pending ? 'submittedForReview' : 'ruleSaved'))
      load()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
  }

  async function remove(rule: PiiRule) {
    try {
      const result = await apiRequestWithStatus<ChangeRequest | undefined>(`/api/v1/platform/pii-rules/${rule.id}`, { method: 'DELETE' })
      setNotice(t(result.status === 202 ? 'submittedForReview' : 'ruleDeleted'))
      load()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
  }

  const needsPattern = draft?.match_type === 'regex' || draft?.match_type === 'keyword'
  return (
    <section className="platform-governance-page">
      <div className="page-heading">
        <div>
          <p className="section-kicker">PLATFORM</p>
          <h1>{t('pii.title')}</h1>
          <p>{t('pii.subtitle')}</p>
        </div>
        <button className="primary-button" onClick={() => { setFormError(''); setDraft(EMPTY) }} type="button"><Plus size={15} /> {t('pii.new')}</button>
      </div>
      {notice && <p className="tenant-success" role="status">{notice}</p>}
      {error && <p className="members-error" role="alert">{error}</p>}
      <RuleChangesPanel currentUserId={currentUserId} onDecided={(message) => { setNotice(message); load() }} onError={setError} requests={changes} />
      <div className="panel">
        <div className="members-table-heading">
          <div><h2>{t('pii.table')}</h2><p className="usage-hint">{list ? t('pii.counts', { total: list.total, enforced: list.enforced, enabled: list.enabled }) : ''}</p></div>
        </div>
        {list && list.items.length === 0 && <p className="assistants-empty-filtered">{t('pii.empty')}</p>}
        {list && list.items.length > 0 && (
          <div className="members-table-wrap">
            <table className="members-table">
              <thead><tr><th>{t('col.rule')}</th><th>{t('col.pattern')}</th><th>{t('col.stage')}</th><th>{t('col.action')}</th><th>{t('col.enabled')}</th><th /></tr></thead>
              <tbody>
                {list.items.map((rule) => (
                  <tr className={rule.enabled ? '' : 'governance-off'} key={rule.id}>
                    <td>
                      <span className="governance-name">{rule.enforced && <Lock aria-label={t('enforced')} size={13} />}<strong>{rule.name}</strong></span>
                      {rule.pending_request_id && <small className="tenant-status-note">{t('pendingReview')}</small>}
                    </td>
                    <td><code className="governance-pattern">{rule.pattern || '—'}</code></td>
                    <td>{t(`stage.${rule.stage}`)}</td>
                    <td>{t(`action.${rule.action}`)}{rule.action === 'block' && <small>{rule.block_message || t('defaultBlockMessage')}</small>}</td>
                    <td>
                      <label className="governance-switch">
                        <input aria-label={t('col.enabled')} checked={rule.enabled} disabled={!!rule.pending_request_id} onChange={() => void toggle(rule)} type="checkbox" />
                        {rule.enforced && <span className="assistant-status published">{t('enforced')}</span>}
                      </label>
                    </td>
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
          <form className="create-modal governance-modal" onClick={(event) => event.stopPropagation()} onSubmit={(event) => { event.preventDefault(); void save() }} role="dialog" aria-modal="true" aria-labelledby="pii-rule-title">
            <div className="section-kicker">PLATFORM</div>
            <h2 id="pii-rule-title">{t(draft.id ? 'pii.editTitle' : 'pii.new')}</h2>
            <label>{t('field.name')}<input autoFocus maxLength={80} onChange={(event) => setDraft({ ...draft, name: event.target.value })} required value={draft.name} /></label>
            <label>{t('field.matchType')}
              <select onChange={(event) => setDraft({ ...draft, match_type: event.target.value as MatchType })} value={draft.match_type}>
                {MATCH_TYPES.map((value) => <option key={value} value={value}>{t(`matchType.${value}`)}</option>)}
              </select>
            </label>
            <label>{t('field.stage')}
              <select onChange={(event) => setDraft({ ...draft, stage: event.target.value as Draft['stage'] })} value={draft.stage}>
                {(['both', 'input', 'output'] as const).map((value) => <option key={value} value={value}>{t(`stage.${value}`)}</option>)}
              </select>
            </label>
            <label>{t('field.pattern')}
              <textarea className="governance-mono" onChange={(event) => setDraft({ ...draft, pattern: event.target.value })} required={needsPattern} rows={3} value={draft.pattern} />
              <small>{t(`patternHint.${draft.match_type}`)}</small>
            </label>
            <label>{t('field.action')}
              <select onChange={(event) => setDraft({ ...draft, action: event.target.value as RuleAction })} value={draft.action}>
                {ACTIONS.map((value) => <option key={value} value={value}>{t(`action.${value}`)}</option>)}
              </select>
            </label>
            {draft.action === 'block' && (
              <label>{t('field.blockMessage')}<textarea maxLength={300} onChange={(event) => setDraft({ ...draft, block_message: event.target.value })} placeholder={t('field.blockMessagePlaceholder')} rows={2} value={draft.block_message} /></label>
            )}
            <label className="governance-check"><input checked={draft.enforced} onChange={(event) => setDraft({ ...draft, enforced: event.target.checked })} type="checkbox" /> {t('field.enforced')}<small>{t('field.enforcedHint')}</small></label>
            <label className="governance-check"><input checked={draft.enabled} onChange={(event) => setDraft({ ...draft, enabled: event.target.checked })} type="checkbox" /> {t('field.enabled')}</label>
            {(draft.enforced || list?.items.find((item) => item.id === draft.id)?.enforced) && (
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
          body={t(deleting.enforced ? 'deleteEnforcedBody' : 'deleteBody', { name: deleting.name })} danger onCancel={() => setDeleting(null)}
          onConfirm={async () => { const target = deleting; setDeleting(null); await remove(target) }} title={t('deleteTitle')}
        />
      )}
    </section>
  )
}
