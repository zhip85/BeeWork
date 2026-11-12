import { useCallback, useEffect, useRef, useState } from 'react'
import type { ChangeEvent, FormEvent } from 'react'
import { ArrowLeft, CheckCircle2, XCircle } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { apiRequest } from '../../api'
import type { TenantMembership } from '../../types'
import type { ApiCallResult, ApiDetail, ApiParseResult, Capability } from './types'

type Form = {
  name: string
  description: string
  apiType: ApiDetail['api_type']
  baseUrl: string
  auth: ApiDetail['auth_type']
  header: string
  secret: string
  tokenUrl: string
  clientId: string
  clientSecret: string
  scope: string
  spec: string
  specFilename: string
}
const EMPTY: Form = { name: '', description: '', apiType: 'rest', baseUrl: '', auth: 'api_key', header: '', secret: '', tokenUrl: '', clientId: '', clientSecret: '', scope: '', spec: '', specFilename: '' }

function toForm(cap: Capability): Form {
  const api = cap.api as ApiDetail
  return {
    name: cap.name, description: cap.description, apiType: api.api_type, baseUrl: api.base_url, auth: api.auth_type, header: api.api_key_header, secret: '',
    tokenUrl: api.oauth_token_url, clientId: api.oauth_client_id, clientSecret: '', scope: api.oauth_scope, spec: '', specFilename: api.spec_filename,
  }
}

/** Connect / edit an API plugin (T-073, CAP-040–042): a REST (OpenAPI) or GraphQL (SDL) API offered to agents as tools.
 * Secrets are password fields; a saved secret shows only its mask and a blank field keeps it. The spec is only sent when it
 * was changed (the server keeps the stored one otherwise). */
export function ApiFormPage({ tenant }: { tenant: TenantMembership }) {
  const { t } = useTranslation('capabilities')
  const { capabilityId } = useParams()
  const navigate = useNavigate()
  const creating = !capabilityId
  const writable = tenant.access_mode === 'full' && tenant.permissions.includes('capabilities:edit')
  const fileInput = useRef<HTMLInputElement>(null)
  const [cap, setCap] = useState<Capability | null>(null)
  const [form, setForm] = useState<Form>(EMPTY)
  const [specTouched, setSpecTouched] = useState(false)
  const [selected, setSelected] = useState<string[]>([])
  const [preview, setPreview] = useState<ApiParseResult | null>(null)
  const [loading, setLoading] = useState(!creating)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [testTool, setTestTool] = useState('')
  const [testArgs, setTestArgs] = useState('{}')
  const [testResult, setTestResult] = useState<ApiCallResult | null>(null)

  const apply = useCallback((next: Capability) => {
    setCap(next); setForm(toForm(next)); setSpecTouched(false); setPreview(null)
    setSelected((next.api?.tools ?? []).filter((tool) => tool.selected).map((tool) => tool.name))
  }, [])

  useEffect(() => {
    if (!capabilityId) return
    apiRequest<Capability>(`/api/v1/capabilities/${capabilityId}`, {}, tenant.id)
      .then(apply)
      .catch((requestError: Error) => setError(requestError.message || t('loadFailed')))
      .finally(() => setLoading(false))
  }, [capabilityId, tenant.id, apply, t])

  const api = cap?.api ?? null
  const set = <K extends keyof Form>(key: K, value: Form[K]) => setForm((current) => ({ ...current, [key]: value }))
  const savedSecret = Boolean(api?.secret_mask) && api?.auth_type === form.auth
  const savedClientSecret = Boolean(api?.oauth_client_secret_mask)
  const canSubmit = Boolean(form.name.trim() && form.baseUrl.trim()) && !busy
  const toolList = preview?.ok && specTouched ? preview.tools : api?.tools ?? []

  function payload(extra: Record<string, unknown> = {}) {
    return {
      name: form.name.trim(), description: form.description.trim(), api_type: form.apiType, base_url: form.baseUrl.trim(), auth_type: form.auth,
      api_key_header: form.auth === 'api_key' ? form.header.trim() : '', secret: form.secret || null, oauth_token_url: form.tokenUrl.trim(),
      oauth_client_id: form.clientId.trim(), oauth_client_secret: form.clientSecret || null, oauth_scope: form.scope.trim(),
      ...(specTouched || creating ? { spec_text: form.spec, spec_filename: form.specFilename } : {}), ...extra,
    }
  }

  async function run(action: () => Promise<void>) {
    setBusy(true); setError(''); setNotice('')
    try { await action() } catch (requestError) { setError(requestError instanceof Error ? requestError.message : t('actionFailed')) } finally { setBusy(false) }
  }

  const onFile = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    if (!file) return
    void file.text().then((text) => { setForm((current) => ({ ...current, spec: text, specFilename: file.name })); setSpecTouched(true); setPreview(null) })
    event.target.value = ''
  }

  const parse = () => void run(async () => {
    setPreview(await apiRequest<ApiParseResult>('/api/v1/capabilities/api/parse', { method: 'POST', body: JSON.stringify({ api_type: form.apiType, spec_text: form.spec }) }, tenant.id))
  })

  const submit = (event: FormEvent) => {
    event.preventDefault()
    void run(async () => {
      if (creating) {
        const created = await apiRequest<Capability>('/api/v1/capabilities/api', { method: 'POST', body: JSON.stringify(payload()) }, tenant.id)
        navigate(`/capabilities/api/${created.id}`, { replace: true })
        apply(created); setNotice(t('apiForm.created'))
      } else if (cap) {
        const saved = await apiRequest<Capability>(`/api/v1/capabilities/api/${cap.id}`, { method: 'PUT', body: JSON.stringify(payload({ selected_tools: selected, row_version: cap.row_version })) }, tenant.id)
        apply(saved); setTestResult(null)
        setNotice(saved.pending_approval ? t('apiForm.savedPending') : t('apiForm.saved'))
        if (saved.approval_warning) setError(saved.approval_warning)
      }
    })
  }

  const call = (confirmed: boolean | null) => {
    if (!cap) return
    let args: unknown
    try { args = JSON.parse(testArgs || '{}') } catch { setError(t('apiForm.badJson')); return }
    void run(async () => {
      const result = await apiRequest<ApiCallResult>(`/api/v1/capabilities/api/${cap.id}/test-call`, { method: 'POST', body: JSON.stringify({ tool: testTool, arguments: args, confirmed }) }, tenant.id)
      setTestResult(result)
      if (result.capability) { setCap(result.capability); if (result.ok && result.capability.approval_warning) setError(result.capability.approval_warning) }
    })
  }

  if (loading) return <section className="capability-form"><p>{t('loading')}</p></section>
  if (!creating && !cap) return <section className="capability-form"><p className="form-error" role="alert">{error || t('notFound')}</p></section>

  const locked = !writable
  const ticked = (api?.tools ?? []).filter((tool) => tool.selected)
  const when = (value: string) => new Date(value).toLocaleString()
  return (
    <section className="kb-create-page capability-form">
      <Link className="back-link" to="/capabilities?type=api"><ArrowLeft size={15} /> {t('backTo', { kind: t('kind.api') })}</Link>
      <div className="kb-create-shell">
        <header className="kb-create-title">
          <div>
            <span className="eyebrow">CAPABILITY</span>
            <h1>{creating ? t('create.api') : t('editTitle', { kind: t('kind.api') })}</h1>
            <p>{t('kindHint.api')}</p>
          </div>
          {cap && <span className={`assistant-status flows-status-${cap.status === 'available' ? 'published' : cap.status}`}>{t(`status.${cap.status}`)}{cap.pending_approval ? ` · ${t('pendingApproval')}` : ''}</span>}
        </header>
        <form className="kb-create-form" onSubmit={submit}>
          <fieldset disabled={locked} style={{ border: 0, padding: 0, margin: 0, display: 'grid', gap: 14 }}>
            <label className="kb-field"><span>{t('form.name')} <em>*</em></span><input maxLength={50} onChange={(event) => set('name', event.target.value)} required value={form.name} /></label>
            <label className="kb-field"><span>{t('form.description')}</span><textarea maxLength={500} onChange={(event) => set('description', event.target.value)} placeholder={t('form.descriptionHint')} rows={3} value={form.description} /></label>
            <label className="kb-field">
              <span>{t('apiForm.type')}</span>
              <select onChange={(event) => { set('apiType', event.target.value as Form['apiType']); setPreview(null) }} value={form.apiType}>
                <option value="rest">{t('apiForm.typeRest')}</option><option value="graphql">{t('apiForm.typeGraphql')}</option>
              </select>
            </label>
            <label className="kb-field"><span>{t(form.apiType === 'graphql' ? 'apiForm.baseUrlGraphql' : 'apiForm.baseUrl')} <em>*</em></span><input onChange={(event) => set('baseUrl', event.target.value)} placeholder={form.apiType === 'graphql' ? 'https://api.example.com/graphql' : 'https://api.example.com/v1'} required value={form.baseUrl} /><small>{t('apiForm.baseUrlHint')}</small></label>
            <label className="kb-field">
              <span>{t('apiForm.auth')}</span>
              <select onChange={(event) => set('auth', event.target.value as Form['auth'])} value={form.auth}>
                <option value="none">{t('apiForm.authNone')}</option><option value="api_key">{t('apiForm.authApiKey')}</option>
                <option value="bearer">{t('apiForm.authBearer')}</option><option value="oauth_cc">{t('apiForm.authOauth')}</option>
              </select>
            </label>
            {form.auth === 'api_key' && (
              <label className="kb-field"><span>{t('apiForm.headerName')} <em>*</em></span><input onChange={(event) => set('header', event.target.value)} placeholder="X-API-Key" value={form.header} /><small>{t('apiForm.headerHint')}</small></label>
            )}
            {(form.auth === 'api_key' || form.auth === 'bearer') && (
              <label className="kb-field"><span>{t(form.auth === 'api_key' ? 'apiForm.apiKey' : 'apiForm.bearer')} <em>*</em></span><input autoComplete="off" onChange={(event) => set('secret', event.target.value)} placeholder={savedSecret ? api?.secret_mask ?? '' : ''} type="password" value={form.secret} />{savedSecret && <small>{t('apiForm.keepHint')}</small>}</label>
            )}
            {form.auth === 'oauth_cc' && (
              <div className="capability-oauth">
                <label className="kb-field"><span>{t('apiForm.tokenUrl')} <em>*</em></span><input onChange={(event) => set('tokenUrl', event.target.value)} value={form.tokenUrl} /></label>
                <label className="kb-field"><span>{t('apiForm.clientId')} <em>*</em></span><input onChange={(event) => set('clientId', event.target.value)} value={form.clientId} /></label>
                <label className="kb-field"><span>{t('apiForm.clientSecret')} <em>*</em></span><input autoComplete="off" onChange={(event) => set('clientSecret', event.target.value)} placeholder={savedClientSecret ? api?.oauth_client_secret_mask ?? '' : ''} type="password" value={form.clientSecret} />{savedClientSecret && <small>{t('apiForm.keepHint')}</small>}</label>
                <label className="kb-field"><span>{t('apiForm.scope')}</span><input onChange={(event) => set('scope', event.target.value)} value={form.scope} /></label>
              </div>
            )}
            <div className="kb-field">
              <span>{t('apiForm.spec')}{form.specFilename ? ` · ${form.specFilename}` : ''}</span>
              <textarea
                onChange={(event) => { set('spec', event.target.value); setSpecTouched(true); setPreview(null) }}
                placeholder={!creating && api?.has_spec && !specTouched ? t('apiForm.keepHint') : ''} rows={8} spellCheck={false} style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }} value={form.spec}
              />
              <small>{t(form.apiType === 'graphql' ? 'apiForm.specHintGraphql' : 'apiForm.specHintRest')}</small>
              <div style={{ display: 'flex', gap: 8 }}>
                <input accept=".yaml,.yml,.json,.graphql,.gql,.txt" hidden onChange={onFile} ref={fileInput} type="file" />
                <button className="secondary-button" onClick={() => fileInput.current?.click()} type="button">{t('apiForm.upload')}</button>
                <button className="secondary-button" disabled={busy || !form.spec.trim()} onClick={parse} type="button">{busy ? t('apiForm.parsing') : t('apiForm.parse')}</button>
              </div>
              {preview && (
                <p className={preview.ok ? 'capability-test ok' : 'capability-test bad'} role="status">
                  {preview.ok ? <CheckCircle2 size={14} /> : <XCircle size={14} />}{' '}
                  {preview.ok ? t('apiForm.parseOk', { count: preview.tool_count }) : preview.line ? t('apiForm.parseFailedLine', { line: preview.line, reason: preview.error ?? '' }) : t('apiForm.parseFailed', { reason: preview.error ?? '' })}
                </p>
              )}
            </div>
          </fieldset>
          {!creating && api && (
            <div className="capability-tools">
              <div className="capability-tools-head">
                <span>{t('apiForm.tools', { count: selected.length })}</span>
                {writable && toolList.some((tool) => !tool.writes) && (
                  <button className="secondary-button" onClick={() => setSelected(Array.from(new Set([...selected, ...api.tools.filter((tool) => !tool.writes).map((tool) => tool.name)])))} type="button">{t('apiForm.selectReadOnly')}</button>
                )}
              </div>
              <p className="wizard-muted">{api.has_spec || specTouched ? t('apiForm.toolsHint') : t('apiForm.noSpec')}</p>
              <ul>
                {api.tools.map((tool) => (
                  <li key={tool.name}>
                    <label>
                      <input checked={selected.includes(tool.name)} disabled={locked} onChange={(event) => setSelected(event.target.checked ? [...selected, tool.name] : selected.filter((name) => name !== tool.name))} type="checkbox" />
                      <code>{tool.name}</code>
                      <span>{tool.description}</span>
                      <em>{tool.writes ? t('apiForm.write') : t('apiForm.read')}</em>
                    </label>
                  </li>
                ))}
              </ul>
              {api.last_test_at && (
                <p className={api.last_test_ok ? 'capability-test ok' : 'capability-test bad'}>
                  {api.last_test_ok ? <CheckCircle2 size={14} /> : <XCircle size={14} />}{' '}
                  {api.last_test_ok ? t('apiForm.lastTestOk', { tool: api.last_test_tool ?? '', when: when(api.last_test_at) }) : t('apiForm.lastTestFailed', { when: when(api.last_test_at), reason: api.last_test_error ?? '' })}
                </p>
              )}
              <p className="wizard-muted">{t('apiForm.statusRule')}</p>
            </div>
          )}
          {!creating && api && (
            <div className="capability-tools">
              <div className="capability-tools-head"><span>{t('apiForm.test')}</span></div>
              <p className="wizard-muted">{ticked.length === 0 ? t('apiForm.saveFirst') : t('apiForm.testHint')}</p>
              {ticked.length > 0 && (
                <>
                  <label className="kb-field">
                    <span>{t('apiForm.testTool')}</span>
                    <select disabled={locked} onChange={(event) => { setTestTool(event.target.value); setTestResult(null) }} value={testTool}>
                      <option value="" />
                      {ticked.map((tool) => <option key={tool.name} value={tool.name}>{tool.name}{tool.writes ? ` · ${t('apiForm.write')}` : ''}</option>)}
                    </select>
                  </label>
                  <label className="kb-field"><span>{t('apiForm.testArgs')}</span><textarea disabled={locked} onChange={(event) => setTestArgs(event.target.value)} rows={4} spellCheck={false} style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }} value={testArgs} /></label>
                  <div style={{ display: 'flex', gap: 8 }}>
                    <button className="secondary-button" disabled={busy || locked || !testTool} onClick={() => call(null)} type="button">{busy ? t('apiForm.testRunning') : t('apiForm.testRun')}</button>
                  </div>
                </>
              )}
              {testResult?.status === 'confirmation_required' && (
                <div className="capability-test bad" role="alertdialog" aria-label={t('apiForm.testConfirm')}>
                  <p>{t('apiForm.testConfirmBody')}</p>
                  <div style={{ display: 'flex', gap: 8 }}>
                    <button className="primary-button" disabled={busy} onClick={() => call(true)} type="button">{t('apiForm.testConfirm')}</button>
                    <button className="secondary-button" disabled={busy} onClick={() => setTestResult(null)} type="button">{t('apiForm.testCancel')}</button>
                  </div>
                </div>
              )}
              {testResult && testResult.status !== 'confirmation_required' && (
                <div className={testResult.ok ? 'capability-test ok' : 'capability-test bad'} role="status">
                  <p>{testResult.ok ? <CheckCircle2 size={14} /> : <XCircle size={14} />} {testResult.ok ? t('apiForm.testOk') : t('apiForm.testFailed', { reason: testResult.content })}</p>
                  {testResult.ok && <><strong>{t('apiForm.testResponse')}</strong><pre style={{ whiteSpace: 'pre-wrap', maxHeight: 240, overflow: 'auto' }}>{testResult.content}</pre></>}
                </div>
              )}
            </div>
          )}
          {notice && <p className="assistants-notice" role="status">{notice}</p>}
          {error && <p className="form-error" role="alert">{error}</p>}
          <div className="kb-form-actions">
            <Link className="kb-cancel" to="/capabilities?type=api">{t('cancel')}</Link>
            {writable && <button className="primary-button" disabled={!canSubmit} type="submit">{busy ? t('saving') : t('save')}</button>}
          </div>
        </form>
      </div>
    </section>
  )
}
