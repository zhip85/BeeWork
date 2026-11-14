import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { Copy, Webhook } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import { formatRelativeTime } from '../../i18n/format'
import { SettingsTabs } from './SettingsTabs'

type Hook = {
  id: string
  url: string
  events: string[]
  enabled: boolean
  secret_mask: string
  disabled_reason: 'failing' | null
  last_delivery: { status: string; status_code: number | null; at: string | null } | null
  row_version: number
}
type Delivery = { id: string; event: string; status: string; attempts: number; status_code: number | null; duration_ms: number | null; error: string | null; is_test: boolean; created_at: string; last_attempt_at: string | null; next_retry_at: string | null }
type Listing = { items: Hook[]; events: string[] }

/** Webhook endpoints (SET-091): add, switch on and off, test, rotate the signing secret (shown once), the last 100 deliveries and resend. */
export function WebhooksPage({ tenantId, canEdit }: { tenantId: string; canEdit: boolean }) {
  const { t } = useTranslation('settings')
  const [listing, setListing] = useState<Listing | null>(null)
  const [url, setUrl] = useState('')
  const [chosen, setChosen] = useState<string[]>([])
  const [secret, setSecret] = useState<string | null>(null)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const [deleting, setDeleting] = useState<Hook | null>(null)
  const [records, setRecords] = useState<{ hook: Hook; items: Delivery[] } | null>(null)

  const load = useCallback(() => {
    apiRequest<Listing>('/api/v1/webhooks', {}, tenantId)
      .then((value) => { setListing(value); setError('') })
      .catch((requestError: Error) => setError(requestError.message))
  }, [tenantId])
  useEffect(load, [load])

  const fail = (requestError: unknown) => setError(requestError instanceof Error ? requestError.message : t('webhooks.failed'))

  async function create(event: FormEvent) {
    event.preventDefault()
    setSaving(true)
    setError('')
    try {
      const created = await apiRequest<{ signing_secret: string }>('/api/v1/webhooks', { method: 'POST', body: JSON.stringify({ url, events: chosen }) }, tenantId)
      setSecret(created.signing_secret)
      setUrl('')
      setChosen([])
      setNotice(t('webhooks.added'))
      load()
    } catch (requestError) { fail(requestError) } finally { setSaving(false) }
  }

  async function toggle(hook: Hook) {
    try {
      await apiRequest(`/api/v1/webhooks/${hook.id}`, { method: 'PUT', body: JSON.stringify({ url: hook.url, events: hook.events, enabled: !hook.enabled, row_version: hook.row_version }) }, tenantId)
      load()
    } catch (requestError) { fail(requestError) }
  }

  async function test(hook: Hook) {
    try {
      await apiRequest(`/api/v1/webhooks/${hook.id}/test`, { method: 'POST' }, tenantId)
      setNotice(t('webhooks.testSent'))
      window.setTimeout(load, 2500) // the Worker sends it; refresh the latest result shortly after
    } catch (requestError) { fail(requestError) }
  }

  async function rotate(hook: Hook) {
    try {
      const result = await apiRequest<{ signing_secret: string }>(`/api/v1/webhooks/${hook.id}/rotate-secret`, { method: 'POST' }, tenantId)
      setSecret(result.signing_secret)
      load()
    } catch (requestError) { fail(requestError) }
  }

  async function remove(hook: Hook) {
    try {
      await apiRequest(`/api/v1/webhooks/${hook.id}`, { method: 'DELETE' }, tenantId)
      setNotice(t('webhooks.deleted'))
      load()
    } catch (requestError) { fail(requestError) }
    setDeleting(null)
  }

  async function open(hook: Hook) {
    try {
      const result = await apiRequest<{ items: Delivery[] }>(`/api/v1/webhooks/${hook.id}/deliveries`, {}, tenantId)
      setRecords({ hook, items: result.items })
    } catch (requestError) { fail(requestError) }
  }

  async function resend(hook: Hook, delivery: Delivery) {
    try {
      await apiRequest(`/api/v1/webhooks/${hook.id}/deliveries/${delivery.id}/redeliver`, { method: 'POST' }, tenantId)
      setNotice(t('webhooks.resent'))
      window.setTimeout(() => void open(hook), 2500)
    } catch (requestError) { fail(requestError) }
  }

  async function copyAndClose() {
    try { await navigator.clipboard?.writeText(secret ?? '') } catch { /* clipboard unavailable */ }
    setSecret(null)
    setNotice(t('webhooks.copied'))
  }

  const latest = (hook: Hook) => (hook.last_delivery
    ? `${hook.last_delivery.status_code ?? t(`webhooks.status.${hook.last_delivery.status}`)} ${hook.last_delivery.status === 'delivered' ? 'OK' : ''} · ${hook.last_delivery.at ? formatRelativeTime(hook.last_delivery.at) : ''}`.trim()
    : t('webhooks.never'))

  return (
    <section className="settings-page">
      <SettingsTabs />
      <div className="page-heading"><div><h1>{t('webhooks.title')}</h1><p>{t('webhooks.subtitle')}</p></div></div>
      {secret && (
        <div className="key-reveal" role="alert">
          <strong>{t('webhooks.copyNow')}</strong>
          <code>{secret}</code>
          <button className="primary-button" onClick={() => void copyAndClose()} type="button"><Copy size={14} /> {t('webhooks.copyClose')}</button>
        </div>
      )}
      {notice && <p className="assistants-notice" role="status">{notice}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}

      {canEdit && listing && (
        <form className="detail-card api-key-form" onSubmit={(event) => void create(event)}>
          <h2><Webhook size={16} /> {t('webhooks.add')}</h2>
          <label>{t('webhooks.url')}<input maxLength={500} onChange={(event) => setUrl(event.target.value)} placeholder="https://hooks.example.com/beework" required type="url" value={url} /></label>
          <fieldset><legend>{t('webhooks.events')}</legend>
            {listing.events.map((name) => (
              <label className="wizard-radio" key={name}>
                <input checked={chosen.includes(name)} onChange={(event) => setChosen(event.target.checked ? [...chosen, name] : chosen.filter((item) => item !== name))} type="checkbox" /> <code>{name}</code>
              </label>
            ))}
          </fieldset>
          <p className="wizard-muted">{t('webhooks.verifyHint')}</p>
          <button className="primary-button" disabled={saving || !url.trim() || chosen.length === 0} type="submit">{saving ? t('webhooks.adding') : t('webhooks.add')}</button>
        </form>
      )}

      {listing && listing.items.length === 0 && <p className="assistants-empty-filtered">{t('webhooks.empty')}</p>}
      {listing && listing.items.length > 0 && (
        <div className="assistants-table-wrap">
          <table className="assistants-table">
            <thead><tr><th>{t('webhooks.url')}</th><th>{t('webhooks.events')}</th><th>{t('webhooks.lastPush')}</th><th>{t('webhooks.enabled')}</th><th /></tr></thead>
            <tbody>
              {listing.items.map((hook) => (
                <tr className={hook.enabled ? '' : 'row-muted'} key={hook.id}>
                  <td><code>{hook.url}</code><small className="platform-tenant-slug">{hook.secret_mask}</small></td>
                  <td>{hook.events.join(', ')}</td>
                  <td>{latest(hook)}{hook.disabled_reason === 'failing' && <small className="tenant-status-note">{t('webhooks.autoOff')}</small>}</td>
                  <td><input aria-label={t('webhooks.enabled')} checked={hook.enabled} disabled={!canEdit} onChange={() => void toggle(hook)} type="checkbox" /></td>
                  <td className="row-actions">
                    <button className="text-button" onClick={() => void open(hook)} type="button">{t('webhooks.records')}</button>
                    {canEdit && <button className="text-button" onClick={() => void test(hook)} type="button">{t('webhooks.test')}</button>}
                    {canEdit && <button className="text-button" onClick={() => void rotate(hook)} type="button">{t('webhooks.rotate')}</button>}
                    {canEdit && <button className="text-button" onClick={() => setDeleting(hook)} type="button">{t('webhooks.delete')}</button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {records && (
        <div className="modal-backdrop confirm-backdrop" role="presentation" onClick={() => setRecords(null)}>
          <div aria-modal="true" className="create-modal webhook-records" onClick={(event) => event.stopPropagation()} role="dialog" aria-label={t('webhooks.records')}>
            <h2>{t('webhooks.records')}</h2>
            <p className="wizard-muted"><code>{records.hook.url}</code></p>
            {records.items.length === 0 && <p className="wizard-muted">{t('webhooks.noRecords')}</p>}
            <table className="assistants-table">
              <thead><tr><th>{t('webhooks.event')}</th><th>{t('webhooks.result')}</th><th>{t('webhooks.attempts')}</th><th>{t('webhooks.time')}</th><th /></tr></thead>
              <tbody>
                {records.items.map((item) => (
                  <tr key={item.id}>
                    <td><code>{item.event}</code></td>
                    <td>{t(`webhooks.status.${item.status}`)}{item.status_code ? ` · ${item.status_code}` : ''}{item.duration_ms !== null ? ` · ${item.duration_ms} ms` : ''}{item.error ? <small className="platform-tenant-slug">{item.error}</small> : null}</td>
                    <td>{item.attempts}</td>
                    <td>{formatRelativeTime(item.last_attempt_at ?? item.created_at)}</td>
                    <td>{canEdit && <button className="text-button" onClick={() => void resend(records.hook, item)} type="button">{t('webhooks.resend')}</button>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="confirm-actions"><button className="secondary-button" onClick={() => setRecords(null)} type="button">{t('webhooks.close')}</button></div>
          </div>
        </div>
      )}
      {deleting && <ConfirmDialog body={t('webhooks.deleteBody', { url: deleting.url })} danger onCancel={() => setDeleting(null)} onConfirm={() => remove(deleting)} title={t('webhooks.delete')} />}
    </section>
  )
}
