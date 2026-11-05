import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import { formatDateTime } from '../../i18n/format'
import type { PlatformTenant } from '../../types'

type Scalar = boolean | number | string | null
type PlanVersion = {
  version: number
  entitlements: Record<string, Scalar>
  quotas: Record<string, number | null>
  overage_policy: Record<string, string>
  published_at: string
  tenant_count: number
}
type Plan = {
  code: string
  name: string
  description: string
  price_text: string
  tenant_count: number
  tenants: { id: string; name: string }[]
  latest_version: number
  versions: PlanVersion[]
}
const QUOTA_KEYS = ['assistants', 'seats', 'monthly_points', 'storage_gb', 'monthly_conversations', 'api_qps'] as const
const POLICIES = ['hard', 'soft', 'degrade'] as const
const ENVIRONMENT_VALUES = ['production_only', 'all']

/** Platform plans and entitlements (PLT-080–085): edit → publish a new version; apply a version to a tenant. */
export function PlatformPlansPage() {
  const { t } = useTranslation('platform')
  const [plans, setPlans] = useState<Plan[]>([])
  const [tenants, setTenants] = useState<PlatformTenant[]>([])
  const [selected, setSelected] = useState('standard')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [draft, setDraft] = useState<{ description: string; price_text: string; entitlements: Record<string, Scalar>; quotas: Record<string, string>; overage: Record<string, string> } | null>(null)
  const [publishing, setPublishing] = useState(false)
  const [deleting, setDeleting] = useState<Plan | null>(null)
  const [apply, setApply] = useState({ tenantId: '', planCode: 'standard', version: '', seats: '', cycle: 'monthly' })
  const [applying, setApplying] = useState(false)

  const load = useCallback(() => {
    apiRequest<Plan[]>('/api/v1/platform/plans')
      .then((result) => { setPlans(result); setError('') })
      .catch((requestError: Error) => setError(requestError.message))
    apiRequest<PlatformTenant[]>('/api/v1/platform/tenants').then(setTenants).catch(() => undefined)
  }, [])
  useEffect(() => { load() }, [load])

  const plan = plans.find((item) => item.code === selected) ?? plans[0]
  const latest = plan?.versions[0]

  function startEdit() {
    if (!plan || !latest) return
    setDraft({
      description: plan.description,
      price_text: plan.price_text,
      entitlements: { ...latest.entitlements },
      quotas: Object.fromEntries(QUOTA_KEYS.map((key) => [key, latest.quotas[key] === null || latest.quotas[key] === undefined ? '' : String(latest.quotas[key])])),
      overage: Object.fromEntries(QUOTA_KEYS.map((key) => [key, latest.overage_policy[key] ?? 'hard'])),
    })
    setNotice('')
  }

  async function publish(event: FormEvent) {
    event.preventDefault()
    if (!plan || !draft) return
    setPublishing(true)
    setError('')
    try {
      await apiRequest<Plan>(`/api/v1/platform/plans/${plan.code}/versions`, {
        method: 'POST',
        body: JSON.stringify({
          description: draft.description,
          price_text: draft.price_text,
          entitlements: draft.entitlements,
          quotas: Object.fromEntries(QUOTA_KEYS.map((key) => [key, draft.quotas[key].trim() === '' ? null : Number(draft.quotas[key])])),
          overage_policy: draft.overage,
        }),
      })
      setDraft(null)
      setNotice(t('plans.published'))
      load()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('plans.publishFailed'))
    } finally {
      setPublishing(false)
    }
  }

  async function remove(target: Plan) {
    try {
      await apiRequest(`/api/v1/platform/plans/${target.code}`, { method: 'DELETE' })
      setNotice(t('plans.deleted'))
      setSelected('standard')
      load()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('plans.deleteFailed'))
    }
    setDeleting(null)
  }

  async function applyChange(event: FormEvent) {
    event.preventDefault()
    setApplying(true)
    setError('')
    try {
      await apiRequest(`/api/v1/platform/tenants/${apply.tenantId}/subscription`, {
        method: 'POST',
        body: JSON.stringify({
          plan_code: apply.planCode,
          version: apply.version ? Number(apply.version) : null,
          seats: apply.seats ? Number(apply.seats) : null,
          billing_cycle: apply.cycle,
        }),
      })
      setNotice(t('plans.applied'))
      load()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('plans.applyFailed'))
    } finally {
      setApplying(false)
    }
  }

  function entitlementInput(key: string, value: Scalar) {
    if (!draft) return null
    const set = (next: Scalar) => setDraft({ ...draft, entitlements: { ...draft.entitlements, [key]: next } })
    if (key === 'environments') {
      return <select onChange={(event) => set(event.target.value)} value={String(value)}>{ENVIRONMENT_VALUES.map((item) => <option key={item} value={item}>{item}</option>)}</select>
    }
    if (key === 'approval_levels') {
      return <select onChange={(event) => set(event.target.value === '1' ? 1 : event.target.value)} value={String(value)}><option value="1">1</option><option value="multi">multi</option></select>
    }
    if (typeof value === 'boolean') return <input checked={value} onChange={(event) => set(event.target.checked)} type="checkbox" />
    if (typeof value === 'number') return <input min={0} onChange={(event) => set(Number(event.target.value))} type="number" value={value} />
    return <input onChange={(event) => set(event.target.value)} value={String(value ?? '')} />
  }

  const shown = (value: Scalar) => (value === true ? '✓' : value === false || value === null ? '—' : String(value))

  return (
    <section className="plans-page">
      <div className="page-heading"><div><h1>{t('plans.title')}</h1><p>{t('plans.subtitle')}</p></div></div>
      {notice && <p className="assistants-notice" role="status">{notice}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      <div className="plans-cards">
        {plans.map((item) => (
          <button className={`detail-card plans-card${item.code === plan?.code ? ' selected' : ''}`} key={item.code} onClick={() => { setSelected(item.code); setDraft(null) }} type="button">
            <span className="approval-status approved">{t('plans.tenantBadge', { count: item.tenant_count })}</span>
            <strong>{item.name}</strong>
            <span className="plans-price">{item.price_text}</span>
            <small>{item.description}</small>
            <small>{item.tenants.length ? item.tenants.map((tenant) => tenant.name).join(' · ') : t('plans.noTenants')}</small>
          </button>
        ))}
      </div>

      {plan && latest && (
        <form className="detail-card plans-detail" onSubmit={(event) => void publish(event)}>
          <div className="detail-card-head">
            <h2>{plan.name} · v{latest.version}</h2>
            <span>
              {!draft && <button className="text-button" onClick={startEdit} type="button">{t('plans.edit')}</button>}
              {!draft && <button className="text-button" onClick={() => setDeleting(plan)} type="button">{t('plans.delete')}</button>}
            </span>
          </div>
          <p className="usage-hint">{t('plans.versionNote')}</p>
          {draft && (
            <>
              <label>{t('plans.price')}<input maxLength={80} onChange={(event) => setDraft({ ...draft, price_text: event.target.value })} value={draft.price_text} /></label>
              <label>{t('plans.description')}<input maxLength={500} onChange={(event) => setDraft({ ...draft, description: event.target.value })} value={draft.description} /></label>
            </>
          )}
          <h3>{t('plans.quotas')}</h3>
          <table className="assistants-table">
            <thead><tr><th>{t('plans.quota')}</th><th>{t('plans.limit')}</th><th>{t('plans.overage')}</th></tr></thead>
            <tbody>
              {QUOTA_KEYS.map((key) => (
                <tr key={key}>
                  <td>{t(`plans.quotaNames.${key}`)}</td>
                  <td>{draft
                    ? <input min={0} onChange={(event) => setDraft({ ...draft, quotas: { ...draft.quotas, [key]: event.target.value } })} placeholder={t('plans.unlimited')} type="number" value={draft.quotas[key]} />
                    : latest.quotas[key] === null || latest.quotas[key] === undefined ? t('plans.unlimited') : latest.quotas[key]?.toLocaleString()}</td>
                  <td>{draft
                    ? <select onChange={(event) => setDraft({ ...draft, overage: { ...draft.overage, [key]: event.target.value } })} value={draft.overage[key]}>{POLICIES.map((policy) => <option key={policy} value={policy}>{t(`plans.policies.${policy}`)}</option>)}</select>
                    : t(`plans.policies.${latest.overage_policy[key] ?? 'hard'}`)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h3>{t('plans.entitlements')}</h3>
          <table className="assistants-table">
            <tbody>
              {Object.keys(draft ? draft.entitlements : latest.entitlements).map((key) => (
                <tr key={key}>
                  <td>{t(`plans.entitlementNames.${key}`, { defaultValue: key })}</td>
                  <td>{draft ? entitlementInput(key, draft.entitlements[key]) : shown(latest.entitlements[key])}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {draft && (
            <div className="plans-actions">
              <button className="primary-button" disabled={publishing} type="submit">{publishing ? t('plans.publishing') : t('plans.publish')}</button>
              <button className="text-button" onClick={() => setDraft(null)} type="button">{t('plans.cancel')}</button>
            </div>
          )}
          <h3>{t('plans.versions')}</h3>
          <ul className="plans-versions">
            {plan.versions.map((version) => (
              <li key={version.version}>v{version.version} · {formatDateTime(version.published_at)} · {t('plans.tenantBadge', { count: version.tenant_count })}</li>
            ))}
          </ul>
        </form>
      )}

      <form className="detail-card plans-apply" onSubmit={(event) => void applyChange(event)}>
        <h2>{t('plans.applyTitle')}</h2>
        <p className="usage-hint">{t('plans.applyHint')}</p>
        <label>{t('plans.tenant')}
          <select onChange={(event) => setApply({ ...apply, tenantId: event.target.value })} required value={apply.tenantId}>
            <option value="">{t('plans.chooseTenant')}</option>
            {tenants.map((tenant) => <option key={tenant.id} value={tenant.id}>{tenant.name} ({tenant.plan})</option>)}
          </select>
        </label>
        <label>{t('plans.planLabel')}
          <select onChange={(event) => setApply({ ...apply, planCode: event.target.value, version: '' })} value={apply.planCode}>
            {plans.map((item) => <option key={item.code} value={item.code}>{item.name}</option>)}
          </select>
        </label>
        <label>{t('plans.versionLabel')}
          <select onChange={(event) => setApply({ ...apply, version: event.target.value })} value={apply.version}>
            <option value="">{t('plans.latest')}</option>
            {(plans.find((item) => item.code === apply.planCode)?.versions ?? []).map((version) => <option key={version.version} value={version.version}>v{version.version}</option>)}
          </select>
        </label>
        <label>{t('plans.seatsLabel')}<input min={1} onChange={(event) => setApply({ ...apply, seats: event.target.value })} placeholder={t('plans.seatsDefault')} type="number" value={apply.seats} /></label>
        <label>{t('plans.cycle')}
          <select onChange={(event) => setApply({ ...apply, cycle: event.target.value })} value={apply.cycle}>
            <option value="monthly">{t('plans.monthly')}</option>
            <option value="yearly">{t('plans.yearly')}</option>
          </select>
        </label>
        <button className="primary-button" disabled={applying || !apply.tenantId} type="submit">{applying ? t('plans.applying') : t('plans.apply')}</button>
      </form>
      {deleting && <ConfirmDialog body={t('plans.deleteBody', { name: deleting.name })} danger onCancel={() => setDeleting(null)} onConfirm={() => remove(deleting)} title={t('plans.delete')} />}
    </section>
  )
}
