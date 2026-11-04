import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { Copy, KeyRound } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import { formatRelativeTime } from '../../i18n/format'
import type { TenantEnvironmentName } from '../../types'
import { SettingsTabs } from './SettingsTabs'

type ApiKey = {
  id: string
  name: string
  environment: TenantEnvironmentName
  scopes: string[]
  masked: string
  created_by_name: string
  created_at: string
  expires_at: string | null
  last_used_at: string | null
  status: 'active' | 'expired' | 'revoked'
}
const SCOPES = ['chat', 'knowledge.read', 'knowledge.write', 'analytics.read'] as const

/** API keys (SET-090): the plaintext is returned once, at creation, and never again. */
export function ApiKeysPage({ tenantId, canEdit, environments }: { tenantId: string; canEdit: boolean; environments: TenantEnvironmentName[] }) {
  const { t } = useTranslation('channels')
  const [keys, setKeys] = useState<ApiKey[] | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [created, setCreated] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [environment, setEnvironment] = useState<TenantEnvironmentName>('production')
  const [scopes, setScopes] = useState<string[]>(['chat'])
  const [expires, setExpires] = useState('')
  const [creating, setCreating] = useState(false)
  const [revoking, setRevoking] = useState<ApiKey | null>(null)

  const load = useCallback(() => {
    apiRequest<{ items: ApiKey[] }>('/api/v1/tenant/api-keys', {}, tenantId)
      .then((result) => { setKeys(result.items); setError('') })
      .catch((requestError: Error) => setError(requestError.message || t('keys.loadFailed')))
  }, [tenantId, t])

  useEffect(() => { load() }, [load])

  async function create(event: FormEvent) {
    event.preventDefault()
    setCreating(true)
    setError('')
    try {
      const result = await apiRequest<{ key: string }>('/api/v1/tenant/api-keys', {
        method: 'POST',
        body: JSON.stringify({ name, environment, scopes, expires_at: expires ? new Date(expires).toISOString() : null }),
      }, tenantId)
      setCreated(result.key)
      setName('')
      setExpires('')
      load()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('keys.createFailed'))
    } finally {
      setCreating(false)
    }
  }

  async function revoke(key: ApiKey) {
    try {
      await apiRequest(`/api/v1/tenant/api-keys/${key.id}/revoke`, { method: 'POST' }, tenantId)
      setNotice(t('keys.revoked'))
      setRevoking(null)
      load()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('keys.createFailed'))
      setRevoking(null)
    }
  }

  async function copyAndClose() {
    try { await navigator.clipboard?.writeText(created ?? '') } catch { /* clipboard unavailable */ }
    setCreated(null)
    setNotice(t('keys.copied'))
  }

  return (
    <section className="settings-page">
      <SettingsTabs />
      <div className="page-heading"><div><h1>{t('keys.title')}</h1><p>{t('keys.subtitle')}</p></div></div>
      {created && (
        <div className="key-reveal" role="alert">
          <strong>{t('keys.copyNow')}</strong>
          <code>{created}</code>
          <button className="primary-button" onClick={() => void copyAndClose()} type="button"><Copy size={14} /> {t('keys.copyClose')}</button>
        </div>
      )}
      {notice && <p className="assistants-notice" role="status">{notice}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}

      {canEdit && (
        <form className="detail-card api-key-form" onSubmit={(event) => void create(event)}>
          <h2><KeyRound size={16} /> {t('keys.create')}</h2>
          <label>{t('keys.name')}<input maxLength={100} onChange={(event) => setName(event.target.value)} required value={name} /></label>
          <label>{t('keys.environment')}
            <select onChange={(event) => setEnvironment(event.target.value as TenantEnvironmentName)} value={environment}>
              {environments.map((item) => <option key={item} value={item}>{t(`environments.${item}`)}</option>)}
            </select>
          </label>
          <fieldset><legend>{t('keys.scopes')}</legend>
            {SCOPES.map((scope) => (
              <label className="wizard-radio" key={scope}>
                <input checked={scopes.includes(scope)} onChange={(event) => setScopes(event.target.checked ? [...scopes, scope] : scopes.filter((item) => item !== scope))} type="checkbox" /> {scope}
              </label>
            ))}
          </fieldset>
          <label>{t('keys.expires')}<input onChange={(event) => setExpires(event.target.value)} type="date" value={expires} /></label>
          <button className="primary-button" disabled={creating || !name.trim() || scopes.length === 0} type="submit">{creating ? t('keys.creating') : t('keys.create')}</button>
        </form>
      )}

      {keys && keys.length === 0 && <p className="assistants-empty-filtered">{t('keys.empty')}</p>}
      {keys && keys.length > 0 && (
        <div className="assistants-table-wrap">
          <table className="assistants-table">
            <thead><tr><th>{t('keys.name')}</th><th>{t('keys.key')}</th><th>{t('keys.lastUsed')}</th><th>{t('keys.status')}</th><th /></tr></thead>
            <tbody>
              {keys.map((key) => (
                <tr key={key.id}>
                  <td><strong>{key.name}</strong><small className="platform-tenant-slug">{key.created_by_name}</small></td>
                  <td><code>{key.masked}</code><small className="platform-tenant-slug">{t(`environments.${key.environment}`)} · {key.scopes.join(', ')}</small></td>
                  <td>{key.last_used_at ? formatRelativeTime(key.last_used_at) : '—'}</td>
                  <td><span className={`approval-status ${key.status === 'active' ? 'approved' : 'withdrawn'}`}>{t(`keys.statuses.${key.status}`)}</span></td>
                  <td>{canEdit && key.status === 'active' && <button className="text-button" onClick={() => setRevoking(key)} type="button">{t('keys.revoke')}</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {revoking && <ConfirmDialog body={t('keys.revokeBody', { name: revoking.name })} danger onCancel={() => setRevoking(null)} onConfirm={() => revoke(revoking)} title={t('keys.revoke')} />}
    </section>
  )
}
