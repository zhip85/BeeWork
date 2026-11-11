import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { ArrowLeft, CheckCircle2, ShieldAlert, XCircle } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { apiRequest } from '../../api'
import type { TenantMembership } from '../../types'
import type { Capability, McpDetail, TestResult } from './types'

type Form = {
  name: string
  description: string
  url: string
  auth_type: 'none' | 'bearer' | 'oauth'
  token: string
  oauth_authorize_url: string
  oauth_token_url: string
  oauth_client_id: string
  oauth_client_secret: string
  oauth_scopes: string
}
const EMPTY: Form = { name: '', description: '', url: '', auth_type: 'none', token: '', oauth_authorize_url: '', oauth_token_url: '', oauth_client_id: '', oauth_client_secret: '', oauth_scopes: '' }

function toForm(cap: Capability): Form {
  const mcp = cap.mcp as McpDetail
  return { name: cap.name, description: cap.description, url: mcp.url, auth_type: mcp.auth_type, token: '', oauth_authorize_url: mcp.oauth_authorize_url, oauth_token_url: mcp.oauth_token_url, oauth_client_id: mcp.oauth_client_id, oauth_client_secret: '', oauth_scopes: mcp.oauth_scopes }
}

/** Connect / edit an MCP server (3.5.2, 3.5.4). Secrets are password fields; a saved secret shows only its mask and a
 * blank field keeps it. */
export function McpFormPage({ tenant }: { tenant: TenantMembership }) {
  const { t } = useTranslation('capabilities')
  const { capabilityId } = useParams()
  const navigate = useNavigate()
  const [query] = useSearchParams()
  const creating = !capabilityId
  const writable = tenant.access_mode === 'full' && tenant.permissions.includes('capabilities:edit')
  const [cap, setCap] = useState<Capability | null>(null)
  const [form, setForm] = useState<Form>(EMPTY)
  const [selected, setSelected] = useState<string[]>([])
  const [loading, setLoading] = useState(!creating)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState(query.get('oauth') === 'ok' ? t('oauth.done') : '')

  const apply = useCallback((next: Capability) => {
    setCap(next)
    setForm(toForm(next))
    setSelected((next.mcp?.tools ?? []).filter((tool) => tool.selected).map((tool) => tool.name))
  }, [])

  useEffect(() => {
    if (!capabilityId) return
    apiRequest<Capability>(`/api/v1/capabilities/${capabilityId}`, {}, tenant.id)
      .then((data) => { apply(data); if (query.get('oauth') === 'error') setError(t('oauth.failed', { reason: query.get('reason') ?? '' })) })
      .catch((requestError: Error) => setError(requestError.message || t('loadFailed')))
      .finally(() => setLoading(false))
  }, [capabilityId, tenant.id, apply, query, t])

  const mcp = cap?.mcp ?? null
  const set = <K extends keyof Form>(key: K, value: Form[K]) => setForm((current) => ({ ...current, [key]: value }))
  const canSubmit = Boolean(form.name.trim() && form.url.trim()) && !busy

  function payload(extra: Record<string, unknown> = {}) {
    return {
      name: form.name.trim(), description: form.description.trim(), transport: 'streamable_http', url: form.url.trim(), auth_type: form.auth_type,
      token: form.token || null, oauth_authorize_url: form.oauth_authorize_url.trim(), oauth_token_url: form.oauth_token_url.trim(),
      oauth_client_id: form.oauth_client_id.trim(), oauth_client_secret: form.oauth_client_secret || null, oauth_scopes: form.oauth_scopes.trim(), ...extra,
    }
  }

  async function run(action: () => Promise<void>) {
    setBusy(true); setError(''); setNotice('')
    try { await action() } catch (requestError) { setError(requestError instanceof Error ? requestError.message : t('actionFailed')) } finally { setBusy(false) }
  }

  const connect = (event: FormEvent) => {
    event.preventDefault()
    void run(async () => {
      const result = await apiRequest<TestResult>('/api/v1/capabilities/mcp', { method: 'POST', body: JSON.stringify(payload()) }, tenant.id)
      if (!result.capability) return
      navigate(`/capabilities/mcp/${result.capability.id}`, { replace: true, state: {} })
      apply(result.capability)
      setNotice(result.ok ? (result.capability.pending_approval ? t('connectedPending') : t('connected')) : '')
      if (!result.ok && result.error) setError(result.error)
      else if (result.capability.approval_warning) setError(result.capability.approval_warning)
    })
  }

  const save = (event: FormEvent) => {
    event.preventDefault()
    if (!cap) return
    void run(async () => {
      const saved = await apiRequest<Capability>(`/api/v1/capabilities/mcp/${cap.id}`, { method: 'PUT', body: JSON.stringify(payload({ selected_tools: selected, row_version: cap.row_version })) }, tenant.id)
      apply(saved)
      setNotice(saved.pending_approval ? t('savedPending') : t('saved'))
      if (saved.approval_warning) setError(saved.approval_warning)
    })
  }

  const test = () => {
    if (!cap) return
    void run(async () => {
      const result = await apiRequest<TestResult>(`/api/v1/capabilities/mcp/${cap.id}/test`, { method: 'POST' }, tenant.id)
      if (result.capability) apply(result.capability)
      if (!result.ok) setError(result.error ?? t('testFailed'))
      else { setNotice(t('testOk', { count: result.tool_count })); if (result.capability?.approval_warning) setError(result.capability.approval_warning) }
    })
  }

  const authorize = () => {
    if (!cap) return
    void run(async () => {
      const result = await apiRequest<{ authorize_url: string }>(`/api/v1/capabilities/mcp/${cap.id}/oauth/start`, { method: 'POST' }, tenant.id)
      window.location.assign(result.authorize_url)
    })
  }

  if (loading) return <section className="capability-form"><p>{t('loading')}</p></section>
  if (!creating && !cap) return <section className="capability-form"><p className="form-error" role="alert">{error || t('notFound')}</p></section>

  const locked = !writable
  return (
    <section className="kb-create-page capability-form">
      <Link className="back-link" to="/capabilities"><ArrowLeft size={15} /> {t('backTo', { kind: t('kind.mcp') })}</Link>
      <div className="kb-create-shell">
        <header className="kb-create-title">
          <div>
            <span className="eyebrow">CAPABILITY</span>
            <h1>{creating ? t('create.mcp') : t('editTitle', { kind: t('kind.mcp') })}</h1>
            <p>{t('kindHint.mcp')}</p>
          </div>
          {cap && <span className={`assistant-status flows-status-${cap.status === 'available' ? 'published' : cap.status}`}>{t(`status.${cap.status}`)}{cap.pending_approval ? ` · ${t('pendingApproval')}` : ''}</span>}
        </header>
        <form className="kb-create-form" onSubmit={(event) => (creating ? connect(event) : save(event))}>
          <fieldset disabled={locked} style={{ border: 0, padding: 0, margin: 0, display: 'grid', gap: 14 }}>
            <label className="kb-field"><span>{t('form.name')} <em>*</em></span><input maxLength={50} onChange={(event) => set('name', event.target.value)} required value={form.name} /></label>
            <label className="kb-field"><span>{t('form.description')}</span><textarea maxLength={500} onChange={(event) => set('description', event.target.value)} placeholder={t('form.descriptionHint')} rows={3} value={form.description} /></label>
            <label className="kb-field"><span>{t('form.transport')}</span><select disabled value="streamable_http"><option value="streamable_http">Streamable HTTP</option></select><small>{t('form.transportHint')}</small></label>
            <label className="kb-field"><span>{t('form.url')} <em>*</em></span><input onChange={(event) => set('url', event.target.value)} placeholder="https://mcp.example.com/mcp" required value={form.url} /></label>
            <label className="kb-field">
              <span>{t('form.auth')}</span>
              <select onChange={(event) => set('auth_type', event.target.value as Form['auth_type'])} value={form.auth_type}>
                <option value="none">{t('auth.none')}</option><option value="bearer">{t('auth.bearer')}</option><option value="oauth">{t('auth.oauth')}</option>
              </select>
            </label>
            {form.auth_type === 'bearer' && (
              <label className="kb-field"><span>{t('form.token')} <em>*</em></span><input autoComplete="off" onChange={(event) => set('token', event.target.value)} placeholder={mcp?.token_mask ?? ''} type="password" value={form.token} /><small>{mcp?.token_mask ? t('form.keepHint') : t('form.tokenHint')}</small></label>
            )}
            {form.auth_type === 'oauth' && (
              <div className="capability-oauth">
                <label className="kb-field"><span>{t('oauth.authorizeUrl')} <em>*</em></span><input onChange={(event) => set('oauth_authorize_url', event.target.value)} value={form.oauth_authorize_url} /></label>
                <label className="kb-field"><span>{t('oauth.tokenUrl')} <em>*</em></span><input onChange={(event) => set('oauth_token_url', event.target.value)} value={form.oauth_token_url} /></label>
                <label className="kb-field"><span>{t('oauth.clientId')} <em>*</em></span><input onChange={(event) => set('oauth_client_id', event.target.value)} value={form.oauth_client_id} /></label>
                <label className="kb-field"><span>{t('oauth.clientSecret')}</span><input autoComplete="off" onChange={(event) => set('oauth_client_secret', event.target.value)} placeholder={mcp?.oauth_client_secret_mask ?? ''} type="password" value={form.oauth_client_secret} /><small>{mcp?.oauth_client_secret_mask ? t('form.keepHint') : ''}</small></label>
                <label className="kb-field"><span>{t('oauth.scopes')}</span><input onChange={(event) => set('oauth_scopes', event.target.value)} value={form.oauth_scopes} /></label>
                {!creating && (
                  <div className="capability-oauth-status">
                    {mcp?.oauth_authorized ? <span><CheckCircle2 size={15} /> {t('oauth.authorized', { when: mcp.oauth_expires_at ? new Date(mcp.oauth_expires_at).toLocaleString() : t('oauth.noExpiry') })}</span> : <span><ShieldAlert size={15} /> {t('oauth.notAuthorized')}</span>}
                    <button className="secondary-button" disabled={busy || locked} onClick={authorize} type="button">{mcp?.oauth_authorized ? t('oauth.reauthorize') : t('oauth.authorize')}</button>
                  </div>
                )}
              </div>
            )}
          </fieldset>
          {!creating && mcp && (
            <div className="capability-tools">
              <div className="capability-tools-head">
                <span>{t('form.tools', { count: selected.length })}</span>
                <button className="secondary-button" disabled={busy || locked} onClick={test} type="button">{busy ? t('testing') : t('testConnection')}</button>
              </div>
              {mcp.last_test_at && (
                <p className={mcp.last_test_ok ? 'capability-test ok' : 'capability-test bad'}>
                  {mcp.last_test_ok ? <CheckCircle2 size={14} /> : <XCircle size={14} />} {mcp.last_test_ok ? t('lastTestOk', { when: new Date(mcp.last_test_at).toLocaleString() }) : t('lastTestFailed', { when: new Date(mcp.last_test_at).toLocaleString(), reason: mcp.last_test_error ?? '' })}
                </p>
              )}
              {mcp.health === 'error' && <p className="form-error" role="alert"><ShieldAlert size={14} /> {t('unhealthyLong')}</p>}
              {mcp.tools.length === 0 && <p className="wizard-muted">{t('noTools')}</p>}
              <ul>
                {mcp.tools.map((tool) => (
                  <li key={tool.name}>
                    <label>
                      <input checked={selected.includes(tool.name)} disabled={locked} onChange={(event) => setSelected(event.target.checked ? [...selected, tool.name] : selected.filter((name) => name !== tool.name))} type="checkbox" />
                      <code>{tool.name}</code>
                      <span>{tool.description}</span>
                      <em>{tool.read_only ? t('toolRead') : t('toolWrite')}</em>
                    </label>
                  </li>
                ))}
              </ul>
              <p className="wizard-muted">{t('statusRule')}</p>
            </div>
          )}
          {notice && <p className="assistants-notice" role="status">{notice}</p>}
          {error && <p className="form-error" role="alert">{error}</p>}
          <div className="kb-form-actions">
            <Link className="kb-cancel" to="/capabilities">{t('cancel')}</Link>
            {writable && <button className="primary-button" disabled={!canSubmit} type="submit">{busy ? t('saving') : creating ? t('connect') : t('save')}</button>}
          </div>
        </form>
      </div>
    </section>
  )
}
