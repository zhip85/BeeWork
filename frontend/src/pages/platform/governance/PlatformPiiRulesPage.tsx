import { useCallback, useEffect, useState } from 'react'
import { Plus } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { apiRequest, apiRequestWithStatus } from '../../../api'
import { ConfirmDialog } from '../../../components/ConfirmDialog'
import { PiiRuleDialog } from '../../../components/pii/PiiRuleDialog'
import { PiiRuleTable } from '../../../components/pii/PiiRuleTable'
import type { RuleDraft, RuleRow } from '../../../components/pii/types'
import { RuleChangesPanel } from './RuleChangesPanel'
import type { ChangeRequest, PiiRule } from './types'

type List = { items: PiiRule[]; total: number; enforced: number; enabled: number }
type Draft = RuleDraft & { id: string | null; enforced: boolean; row_version: number | null; reason: string }
const EMPTY: Draft = { key: null, id: null, name: '', match_type: 'regex', pattern: '', stage: 'both', action: 'mask', block_message: '', enforced: false, enabled: true, row_version: null, reason: '' }

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
    setDraft({ ...rule, key: rule.id, reason: '' })
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
      const pending = await send({ ...rule, key: rule.id, enabled: !rule.enabled, reason: '' }, rule)
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

  const rows: RuleRow[] = (list?.items ?? []).map((rule) => ({
    key: rule.id, name: rule.name, match_type: rule.match_type, pattern: rule.pattern, stage: rule.stage, action: rule.action,
    block_message: rule.block_message, enabled: rule.enabled, enforced: rule.enforced, label: 'platform', restorable: false,
    deletable: true, pending: !!rule.pending_request_id,
  }))
  const ruleOf = (row: RuleRow) => list?.items.find((item) => item.id === row.key)
  const existing = draft?.id ? list?.items.find((item) => item.id === draft.id) : undefined

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
          <PiiRuleTable
            mode="platform" onDelete={(row) => { const rule = ruleOf(row); if (rule) setDeleting(rule) }}
            onEdit={(row) => { const rule = ruleOf(row); if (rule) edit(rule) }} onToggle={(row) => { const rule = ruleOf(row); if (rule) void toggle(rule) }} rows={rows}
          />
        )}
      </div>

      {draft && (
        <PiiRuleDialog
          draft={draft} error={formError} inherited={false} mode="platform" onChange={(next) => setDraft({ ...draft, ...next })} onClose={() => setDraft(null)} onSave={() => void save()}
          saving={saving} titleKey={draft.id ? 'pii.editTitle' : 'pii.new'}
          extras={(
            <>
              <label className="governance-check"><input checked={draft.enforced} onChange={(event) => setDraft({ ...draft, enforced: event.target.checked })} type="checkbox" /> {t('field.enforced')}<small>{t('field.enforcedHint')}</small></label>
              {(draft.enforced || existing?.enforced) && (
                <label>{t('field.reason')}<textarea maxLength={500} onChange={(event) => setDraft({ ...draft, reason: event.target.value })} rows={2} value={draft.reason} /><small>{t('field.reasonHint')}</small></label>
              )}
            </>
          )}
        />
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
