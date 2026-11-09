import { useCallback, useEffect, useState } from 'react'
import { Plus } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { apiRequestWithStatus } from '../../api'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import { PiiRuleDialog } from '../../components/pii/PiiRuleDialog'
import { PiiRuleTable } from '../../components/pii/PiiRuleTable'
import { EMPTY_RULE } from '../../components/pii/types'
import type { RuleDraft, RuleRow } from '../../components/pii/types'
import { SettingsTabs } from './SettingsTabs'

type Item = Omit<RuleRow, 'pending'> & { pending_approval_id: string | null }
type State = {
  tenant_name: string
  chain: { platform_total: number; platform_enforced: number; inherited: number; tenant_level: number; overrides: number; total: number; effective: number }
  inherit_platform: boolean
  settings_version: number
  items: Item[]
  approval: { required: boolean }
}
type Mutation = { outcome: 'applied' | 'pending'; kind: string; approval_id: string | null }
const SCOPES = ['all', 'tenant'] as const

/** Settings → rule library (SET-050–054): the tenant layer of the PII rules, on the shared rule table. */
export function TenantPiiRulesPage({ tenantId, canEdit, canDelete }: { tenantId: string; canEdit: boolean; canDelete: boolean }) {
  const { t } = useTranslation('governance')
  const [state, setState] = useState<State | null>(null)
  const [scope, setScope] = useState<(typeof SCOPES)[number]>('all')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [draft, setDraft] = useState<RuleDraft | null>(null)
  const [inherited, setInherited] = useState(false)
  const [formError, setFormError] = useState('')
  const [saving, setSaving] = useState(false)
  const [deleting, setDeleting] = useState<RuleRow | null>(null)

  const load = useCallback(() => {
    apiRequestWithStatus<State>(`/api/v1/tenant/pii-rules?scope=${scope}`, {}, tenantId)
      .then((result) => { setState(result.data); setError('') })
      .catch((requestError: Error) => setError(requestError.message || t('loadFailed')))
  }, [scope, tenantId, t])
  useEffect(() => { load() }, [load])

  const rows: RuleRow[] = (state?.items ?? []).map((item) => ({ ...item, pending: !!item.pending_approval_id }))

  /** Run a change and say what happened: applied (with the PRD's toast for the kind) or waiting for approval. */
  async function run(path: string, method: string, body: unknown, done: string) {
    const result = await apiRequestWithStatus<Mutation>(path, { method, body: body === undefined ? undefined : JSON.stringify(body) }, tenantId)
    setNotice(result.data.outcome === 'pending' ? t('tenant.submittedForApproval') : t(`tenant.toast.${result.data.kind === 'override_created' ? done : result.data.kind}`, { defaultValue: t('ruleSaved') }))
    load()
  }

  function open(row: RuleRow | null) {
    setFormError('')
    setInherited(!!row && row.label === 'platform_inherited')
    setDraft(row ? { key: row.key, name: row.name, match_type: row.match_type, pattern: row.pattern, stage: row.stage, action: row.action, block_message: row.block_message, enabled: row.enabled } : EMPTY_RULE)
  }

  async function save() {
    if (!draft) return
    setSaving(true)
    setFormError('')
    try {
      const body = { name: draft.name, match_type: draft.match_type, pattern: draft.pattern, stage: draft.stage, action: draft.action, block_message: draft.block_message, enabled: draft.enabled }
      await run(draft.key ? `/api/v1/tenant/pii-rules/${draft.key}` : '/api/v1/tenant/pii-rules', draft.key ? 'PUT' : 'POST', body, 'savedAsTenant')
      setDraft(null)
    } catch (requestError) {
      setFormError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    } finally {
      setSaving(false)
    }
  }

  async function guard(action: () => Promise<void>) {
    setError('')
    try {
      await action()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
  }

  const toggle = (row: RuleRow) => guard(() => run(`/api/v1/tenant/pii-rules/${row.key}`, 'PUT', {
    name: row.name, match_type: row.match_type, pattern: row.pattern, stage: row.stage, action: row.action, block_message: row.block_message, enabled: !row.enabled,
  }, 'toggled'))
  const restore = (row: RuleRow) => guard(() => run(`/api/v1/tenant/pii-rules/${row.key}/restore`, 'POST', undefined, 'restored'))
  const remove = (row: RuleRow) => guard(() => run(`/api/v1/tenant/pii-rules/${row.key}`, 'DELETE', undefined, 'deleted'))
  const setInherit = (value: boolean) => guard(() => run('/api/v1/tenant/pii-rules/settings', 'PUT', { inherit_platform: value, row_version: state?.settings_version ?? 0 }, 'settings'))

  const chain = state?.chain
  return (
    <section className="settings-page platform-governance-page">
      <SettingsTabs />
      <div className="page-heading">
        <div>
          <p className="section-kicker">{t('tenant.kicker')}</p>
          <h1>{t('tenant.title')}</h1>
          <p>{t('tenant.subtitle')}</p>
        </div>
        {canEdit && <button className="primary-button" onClick={() => open(null)} type="button"><Plus size={15} /> {t('tenant.new')}</button>}
      </div>
      {notice && <p className="tenant-success" role="status">{notice}{' '}{notice === t('tenant.submittedForApproval') && <Link to="/approvals">{t('viewApproval')}</Link>}</p>}
      {error && <p className="members-error" role="alert">{error}</p>}
      {state?.approval.required && <p className="usage-hint">{t('tenant.approvalHint')}</p>}

      {chain && (
        <ol className="governance-chain" aria-label={t('tenant.chainLabel')}>
          <li><strong>{t('tenant.chainPlatform')}</strong><small>{t('tenant.chainPlatformCounts', { total: chain.platform_total, enforced: chain.platform_enforced })}</small></li>
          <li><strong>{t('tenant.chainTenant', { name: state?.tenant_name })}</strong><small>{t('tenant.chainTenantCounts', { inherited: chain.inherited, own: chain.tenant_level + chain.overrides, total: chain.total, effective: chain.effective })}</small></li>
        </ol>
      )}

      {state && (
        <label className="governance-inherit">
          <input checked={state.inherit_platform} disabled={!canEdit} onChange={(event) => void setInherit(event.target.checked)} type="checkbox" />
          <span><strong>{t('tenant.inherit')}</strong><small>{t('tenant.inheritHint')}</small></span>
        </label>
      )}

      <div className="panel">
        <div className="knowledge-status-bar">
          {SCOPES.map((value) => (
            <button aria-pressed={scope === value} className={scope === value ? 'active' : ''} key={value} onClick={() => setScope(value)} type="button">{t(`tenant.scope.${value}`)}</button>
          ))}
        </div>
        <h2 className="governance-table-title">{t('tenant.table')}</h2>
        <p className="usage-hint">{t('tenant.priority')}</p>
        {rows.length === 0 && <p className="assistants-empty-filtered">{t('tenant.empty')}</p>}
        {rows.length > 0 && (
          <PiiRuleTable
            mode="tenant" onDelete={canDelete ? (row) => setDeleting(row) : undefined} onEdit={(row) => open(row)}
            onRestore={(row) => void restore(row)} onToggle={(row) => void toggle(row)} readOnly={!canEdit} rows={rows}
          />
        )}
      </div>

      {draft && (
        <PiiRuleDialog
          draft={draft} error={formError} inherited={inherited} mode="tenant" onChange={setDraft} onClose={() => setDraft(null)} onSave={() => void save()}
          saving={saving} titleKey={draft.key ? 'tenant.editTitle' : 'tenant.new'}
        />
      )}
      {deleting && (
        <ConfirmDialog body={t('deleteBody', { name: deleting.name })} danger onCancel={() => setDeleting(null)} onConfirm={async () => { const target = deleting; setDeleting(null); await remove(target) }} title={t('deleteTitle')} />
      )}
    </section>
  )
}
