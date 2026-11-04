import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import { SettingsTabs } from './SettingsTabs'

type Policy = { event: string; request_type: string; approver_role: string; levels: 1 | 2; enabled: boolean; row_version: number }
type PolicyList = { items: Policy[]; pending_count: number; multi_level_allowed: boolean }
const ROLES = ['tenant_admin', 'security_officer', 'knowledge_owner', 'assistant_owner'] as const

export function ApprovalPoliciesPage({ tenantId, canEdit }: { tenantId: string; canEdit: boolean }) {
  const { t } = useTranslation('approvals')
  const [data, setData] = useState<PolicyList | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [saving, setSaving] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    apiRequest<PolicyList>('/api/v1/tenant/approval-policies', {}, tenantId)
      .then((result) => active && setData(result))
      .catch((requestError: Error) => active && setError(requestError.message || t('loadFailed')))
    return () => { active = false }
  }, [tenantId, t])

  async function save(policy: Policy, change: Partial<Policy>) {
    setSaving(policy.event)
    setError('')
    setNotice('')
    const next = { ...policy, ...change }
    try {
      const result = await apiRequest<PolicyList>('/api/v1/tenant/approval-policies', {
        method: 'PUT',
        body: JSON.stringify({ items: [{ event: next.event, approver_role: next.approver_role, levels: next.levels, enabled: next.enabled, row_version: policy.row_version }] }),
      }, tenantId)
      setData(result)
      if (change.enabled !== undefined) setNotice(t(change.enabled ? 'policies.enabledToast' : 'policies.disabledToast'))
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('loadFailed'))
    } finally {
      setSaving(null)
    }
  }

  return (
    <section className="settings-page">
      <SettingsTabs />
      <div className="page-heading">
        <div>
          <h1>{t('policies.title')}{data && data.pending_count > 0 && <span className="assistant-status draft">{t('policies.pending', { count: data.pending_count })}</span>}</h1>
          <p>{t('policies.subtitle')}</p>
        </div>
      </div>
      {notice && <p className="assistants-notice" role="status">{notice}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      {!data && !error && <p>{t('loading')}</p>}
      {data && (
        <div className="assistants-table-wrap">
          <table className="assistants-table">
            <thead><tr><th>{t('policies.event')}</th><th>{t('policies.approver')}</th><th>{t('policies.levels')}</th><th>{t('policies.required')}</th></tr></thead>
            <tbody>
              {data.items.map((policy) => (
                <tr className={policy.enabled ? '' : 'policy-off'} key={policy.event}>
                  <td><strong>{t(`policies.events.${policy.event}`)}</strong></td>
                  <td>
                    <select aria-label={t('policies.approver')} disabled={!canEdit || !policy.enabled || saving === policy.event} onChange={(event) => void save(policy, { approver_role: event.target.value })} value={policy.approver_role}>
                      {ROLES.map((role) => <option key={role} value={role}>{t(`policies.roles.${role}`)}</option>)}
                    </select>
                  </td>
                  <td>
                    <select aria-label={t('policies.levels')} disabled={!canEdit || !policy.enabled || saving === policy.event} onChange={(event) => void save(policy, { levels: Number(event.target.value) as 1 | 2 })} value={policy.levels}>
                      <option value={1}>{t('policies.level', { count: 1 })}</option>
                      <option disabled={!data.multi_level_allowed && policy.levels !== 2} value={2}>{t('policies.level', { count: 2 })}{data.multi_level_allowed ? '' : ` · ${t('policies.enterpriseOnly')}`}</option>
                    </select>
                  </td>
                  <td><label className="switch-row"><input aria-label={t('policies.required')} checked={policy.enabled} disabled={!canEdit || saving === policy.event} onChange={(event) => void save(policy, { enabled: event.target.checked })} type="checkbox" /></label></td>
                </tr>
              ))}
            </tbody>
          </table>
          <small className="wizard-muted">{canEdit ? t('policies.hint') : t('policies.readOnly')}</small>
        </div>
      )}
    </section>
  )
}
