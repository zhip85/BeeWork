import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { ArrowLeft, CheckCircle2, Plus, Trash2, XCircle } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { apiRequest } from '../../api'
import type { TenantMembership } from '../../types'
import type { FlowConnection, FlowKind, FlowTestResult } from './types'

type Row = { variable: string; argument: string }
type Form = {
  name: string
  description: string
  baseUrl: string
  appId: string
  apiKey: string
  rows: Row[]
  outputField: string
  streaming: boolean
}
const EMPTY: Form = { name: '', description: '', baseUrl: '', appId: '', apiKey: '', rows: [], outputField: '', streaming: false }

function toForm(conn: FlowConnection): Form {
  return {
    name: conn.name, description: conn.description, baseUrl: conn.base_url, appId: conn.app_id,
    apiKey: '', rows: Object.entries(conn.input_mapping).map(([variable, argument]) => ({ variable, argument })),
    outputField: conn.output_mapping.field ?? '', streaming: conn.streaming,
  }
}

/** Add / edit a Chatflow or Workflow connection (T-075, 演进 PRD 8). The source is either a published canvas of this
 * platform (recommended, no credentials) or a Dify application. The API key is a password field: a saved key shows only
 * its mask and a blank field keeps it. */
export function FlowConnectionFormPage({ tenant }: { tenant: TenantMembership }) {
  const { t } = useTranslation('capabilities')
  const { connectionId } = useParams()
  const navigate = useNavigate()
  const location = useLocation()
  const kind: FlowKind = location.pathname.startsWith('/capabilities/workflow') ? 'workflow' : 'chatflow'
  const creating = !connectionId
  const writable = tenant.access_mode === 'full' && tenant.permissions.includes('capabilities:edit')
  const [conn, setConn] = useState<FlowConnection | null>(null)
  const [form, setForm] = useState<Form>(EMPTY)
  const [loading, setLoading] = useState(!creating)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [test, setTest] = useState<FlowTestResult | null>(null)
  const kindName = t(`kind.${kind}`)

  const apply = useCallback((next: FlowConnection) => { setConn(next); setForm(toForm(next)) }, [])

  useEffect(() => {
    if (!connectionId) return
    apiRequest<FlowConnection>(`/api/v1/flow-connections/${connectionId}`, {}, tenant.id)
      .then(apply)
      .catch((requestError: Error) => setError(requestError.message || t('loadFailed')))
      .finally(() => setLoading(false))
  }, [connectionId, tenant.id, apply, t])

  const set = <K extends keyof Form>(key: K, value: Form[K]) => setForm((current) => ({ ...current, [key]: value }))
  const setRow = (index: number, patch: Partial<Row>) => set('rows', form.rows.map((row, i) => (i === index ? { ...row, ...patch } : row)))
  const savedKey = conn?.source_type === 'dify' && Boolean(conn.credential_mask)
  const complete = Boolean(form.baseUrl.trim() && form.appId.trim() && (form.apiKey.trim() || savedKey))
  const canSubmit = Boolean(form.name.trim()) && complete && !busy

  function payload() {
    const base = { name: form.name.trim(), description: form.description.trim(), source_type: 'dify' }
    const input_mapping: Record<string, string> = {}
    for (const row of form.rows) if (row.variable.trim() || row.argument.trim()) input_mapping[row.variable.trim()] = row.argument.trim()
    return {
      ...base, base_url: form.baseUrl.trim(), app_id: form.appId.trim(), credential: form.apiKey || null, input_mapping,
      output_mapping: form.outputField.trim() ? { field: form.outputField.trim() } : {}, streaming: form.streaming,
    }
  }

  async function run(action: () => Promise<void>) {
    setBusy(true); setError(''); setNotice('')
    try { await action() } catch (requestError) { setError(requestError instanceof Error ? requestError.message : t('actionFailed')) } finally { setBusy(false) }
  }

  const submit = (event: FormEvent) => {
    event.preventDefault()
    void run(async () => {
      if (creating) {
        const created = await apiRequest<FlowConnection>('/api/v1/flow-connections', { method: 'POST', body: JSON.stringify({ ...payload(), kind }) }, tenant.id)
        navigate(`/capabilities/${kind}/${created.id}`, { replace: true })
        apply(created); setNotice(t('flowConn.created'))
      } else if (conn) {
        const saved = await apiRequest<FlowConnection>(`/api/v1/flow-connections/${conn.id}`, { method: 'PUT', body: JSON.stringify({ ...payload(), row_version: conn.row_version }) }, tenant.id)
        apply(saved); setNotice(t('flowConn.saved')); setTest(null)
      }
    })
  }

  const runTest = () => {
    if (!conn) return
    void run(async () => {
      const result = await apiRequest<FlowTestResult>(`/api/v1/flow-connections/${conn.id}/test`, { method: 'POST' }, tenant.id)
      setConn(result.connection); setTest(result)
    })
  }

  if (loading) return <section className="capability-form"><p>{t('loading')}</p></section>
  if (!creating && !conn) return <section className="capability-form"><p className="form-error" role="alert">{error || t('notFound')}</p></section>

  const category = (value: string | null) => (value ? t(`flowConn.category.${value}`, { defaultValue: value }) : '')
  const when = (value: string) => new Date(value).toLocaleString()
  const locked = !writable
  return (
    <section className="kb-create-page capability-form">
      <Link className="back-link" to={`/capabilities?type=${kind}`}><ArrowLeft size={15} /> {t('flowConn.backTo', { kind: kindName })}</Link>
      <div className="kb-create-shell">
        <header className="kb-create-title">
          <div>
            <span className="eyebrow">CAPABILITY</span>
            <h1>{creating ? t(`create.${kind}`) : t('flowConn.editTitle', { kind: kindName })}</h1>
            <p>{t(`kindHint.${kind}`)}</p>
          </div>
          {conn && <span className={`assistant-status flows-status-${conn.state === 'available' ? 'published' : conn.state === 'disabled' ? 'disabled' : 'draft'}`}>{t(`flowConn.stateLabel.${conn.state}`)}</span>}
        </header>
        {conn?.state_reason && <p className="form-error" role="alert">{conn.state_reason}</p>}
        {locked && <p className="wizard-muted">{t('flowConn.readonly')}</p>}
        <form className="kb-create-form" onSubmit={submit}>
          <fieldset disabled={locked} style={{ border: 0, padding: 0, margin: 0, display: 'grid', gap: 14 }}>
            <label className="kb-field"><span>{t('flowConn.form.name')} <em>*</em></span><input maxLength={50} onChange={(event) => set('name', event.target.value)} required value={form.name} /></label>
            <label className="kb-field"><span>{t('flowConn.form.description')}</span><textarea maxLength={500} onChange={(event) => set('description', event.target.value)} placeholder={t('flowConn.form.descriptionHint')} rows={3} value={form.description} /></label>
            <>
                <label className="kb-field"><span>{t('flowConn.form.baseUrl')} <em>*</em></span><input onChange={(event) => set('baseUrl', event.target.value)} placeholder="https://api.dify.ai/v1" required value={form.baseUrl} /><small>{t('flowConn.form.baseUrlHint')}</small></label>
                <label className="kb-field"><span>{t('flowConn.form.appId')} <em>*</em></span><input onChange={(event) => set('appId', event.target.value)} required value={form.appId} /></label>
                <label className="kb-field">
                  <span>{t('flowConn.form.apiKey')} <em>*</em></span>
                  <input autoComplete="off" onChange={(event) => set('apiKey', event.target.value)} placeholder={savedKey ? conn?.credential_mask ?? '' : ''} type="password" value={form.apiKey} />
                  <small>{savedKey ? t('flowConn.form.keepHint') : t('flowConn.form.apiKeyHint')}</small>
                </label>
                <div className="kb-field">
                  <span>{t('flowConn.form.inputMapping')}</span>
                  <small>{t('flowConn.form.inputMappingHint')}</small>
                  {form.rows.map((row, index) => (
                    <div key={index} style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                      <input aria-label={t('flowConn.form.variable')} onChange={(event) => setRow(index, { variable: event.target.value })} placeholder={t('flowConn.form.variable')} value={row.variable} />
                      <input aria-label={t('flowConn.form.argument')} onChange={(event) => setRow(index, { argument: event.target.value })} placeholder={t('flowConn.form.argument')} value={row.argument} />
                      <button aria-label={t('flowConn.form.removeRow')} className="icon-button" onClick={() => set('rows', form.rows.filter((_, i) => i !== index))} type="button"><Trash2 size={15} /></button>
                    </div>
                  ))}
                  <button className="secondary-button" onClick={() => set('rows', [...form.rows, { variable: '', argument: '' }])} style={{ justifySelf: 'start' }} type="button"><Plus size={14} /> {t('flowConn.form.addRow')}</button>
                </div>
                {kind === 'workflow' && (
                  <label className="kb-field"><span>{t('flowConn.form.outputField')}</span><input onChange={(event) => set('outputField', event.target.value)} value={form.outputField} /><small>{t('flowConn.form.outputFieldHint')}</small></label>
                )}
                <label className="kb-field" style={{ flexDirection: 'row', gap: 8, alignItems: 'center' }}>
                  <input checked={form.streaming} onChange={(event) => set('streaming', event.target.checked)} type="checkbox" />
                  <span>{t('flowConn.form.streaming')}</span><small>{t('flowConn.form.streamingHint')}</small>
                </label>
            </>
          </fieldset>
          {!creating && conn && (
            <div className="capability-tools">
              <div className="capability-tools-head">
                <span>{t('flowConn.test')}</span>
                <button className="secondary-button" disabled={busy || locked} onClick={runTest} type="button">{busy ? t('flowConn.testing') : test && !test.ok ? t('flowConn.retry') : t('flowConn.test')}</button>
              </div>
              {test ? (
                <p className={test.ok ? 'capability-test ok' : 'capability-test bad'}>
                  {test.ok ? <CheckCircle2 size={14} /> : <XCircle size={14} />}{' '}
                  {test.ok ? t('flowConn.testOk', { ms: test.duration_ms ?? 0 }) : t('flowConn.testFailed', { category: category(test.error_category), reason: test.error ?? '' })}
                </p>
              ) : conn.last_test_at && (
                <p className={conn.last_test_ok ? 'capability-test ok' : 'capability-test bad'}>
                  {conn.last_test_ok ? <CheckCircle2 size={14} /> : <XCircle size={14} />}{' '}
                  {conn.last_test_ok ? t('flowConn.lastTestOk', { when: when(conn.last_test_at), ms: conn.last_test_ms ?? 0 }) : t('flowConn.lastTestFailed', { when: when(conn.last_test_at), reason: conn.last_test_error ?? '' })}
                </p>
              )}
            </div>
          )}
          {notice && <p className="assistants-notice" role="status">{notice}</p>}
          {error && <p className="form-error" role="alert">{error}</p>}
          <div className="kb-form-actions">
            <Link className="kb-cancel" to={`/capabilities?type=${kind}`}>{t('cancel')}</Link>
            {writable && <button className="primary-button" disabled={!canSubmit} type="submit">{busy ? t('saving') : t('save')}</button>}
          </div>
        </form>
      </div>
    </section>
  )
}
