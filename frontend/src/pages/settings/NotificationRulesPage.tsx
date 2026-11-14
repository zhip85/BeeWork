import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { Bell, Plus, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import { SettingsTabs } from './SettingsTabs'

type Rule = { id: string; event: string; channel: string; recipients: Record<string, unknown>; summary: string; frequency: string; enabled: boolean; row_version: number }
type Named = { id: string; name: string }
type Catalog = { events: { key: string; zh: string; en: string }[]; channels: { key: string; available: boolean; reason: string | null }[]; frequencies: string[]; roles: Named[]; members: Named[]; webhooks: Named[] }
type Draft = { event: string; channel: string; frequency: string; enabled: boolean; members: string[]; roles: string[]; emails: string; url: string; phones: string; webhooks: string[] }

const blank = (event: string): Draft => ({ event, channel: 'email', frequency: 'realtime', enabled: true, members: [], roles: [], emails: '', url: '', phones: '', webhooks: [] })
const lines = (text: string) => text.split(/[\n,;，；\s]+/).map((item) => item.trim()).filter(Boolean)

function toBody(draft: Draft, rule: Rule | null) {
  const recipients: Record<string, unknown> = draft.channel === 'email'
    ? { members: draft.members, roles: draft.roles, emails: lines(draft.emails) }
    : draft.channel === 'slack' ? { url: draft.url.trim() }
      : draft.channel === 'sms' ? { phones: lines(draft.phones) } : { webhook_ids: draft.webhooks }
  return { event: draft.event, channel: draft.channel, frequency: draft.frequency, enabled: draft.enabled, recipients, row_version: rule?.row_version }
}

function draftOf(rule: Rule): Draft {
  const people = rule.recipients
  const list = (key: string) => (Array.isArray(people[key]) ? (people[key] as string[]) : [])
  return { ...blank(rule.event), channel: rule.channel, frequency: rule.frequency, enabled: rule.enabled, members: list('members'), roles: list('roles'), emails: list('emails').join('\n'), phones: list('phones').join('\n'), webhooks: list('webhook_ids') }
}

/** Notification rules (SET-100–102): which event is told to whom, over which channel, how often. */
export function NotificationRulesPage({ tenantId, canEdit }: { tenantId: string; canEdit: boolean }) {
  const { t, i18n } = useTranslation('settings')
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [rules, setRules] = useState<Rule[] | null>(null)
  const [search, setSearch] = useState('')
  const [editing, setEditing] = useState<Rule | 'new' | null>(null)
  const [deleting, setDeleting] = useState<Rule | null>(null)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')

  const load = useCallback(() => {
    const params = new URLSearchParams()
    if (search.trim()) params.set('q', search.trim())
    apiRequest<{ items: Rule[] }>(`/api/v1/notification-rules?${params}`, {}, tenantId).then((value) => { setRules(value.items); setError('') }).catch((requestError: Error) => setError(requestError.message))
  }, [tenantId, search])
  useEffect(load, [load])
  useEffect(() => {
    apiRequest<Catalog>('/api/v1/notification-rules/catalog', {}, tenantId).then(setCatalog).catch(() => setCatalog(null))
  }, [tenantId])

  const label = (key: string) => { const found = catalog?.events.find((item) => item.key === key); return found ? (i18n.language.startsWith('en') ? found.en : found.zh) : key }

  async function remove(rule: Rule) {
    try {
      await apiRequest(`/api/v1/notification-rules/${rule.id}`, { method: 'DELETE' }, tenantId)
      setNotice(t('rules.deleted'))
      load()
    } catch (requestError) { setError(requestError instanceof Error ? requestError.message : t('rules.failed')) }
    setDeleting(null)
  }

  return (
    <section className="settings-page">
      <SettingsTabs />
      <div className="page-heading">
        <div><h1>{t('rules.title')}</h1><p>{t('rules.subtitle')}</p></div>
        {canEdit && <button className="primary-button" disabled={!catalog} onClick={() => setEditing('new')} type="button"><Plus size={14} /> {t('rules.new')}</button>}
      </div>
      {notice && <p className="assistants-notice" role="status">{notice}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      <div className="assistants-toolbar"><input aria-label={t('rules.search')} onChange={(event) => setSearch(event.target.value)} placeholder={t('rules.search')} value={search} /></div>
      {rules && rules.length === 0 && <p className="assistants-empty-filtered">{t('rules.empty')}</p>}
      {rules && rules.length > 0 && (
        <div className="assistants-table-wrap">
          <table className="assistants-table">
            <thead><tr><th>{t('rules.event')}</th><th>{t('rules.channel')}</th><th>{t('rules.recipients')}</th><th>{t('rules.frequency')}</th><th>{t('rules.status')}</th><th /></tr></thead>
            <tbody>
              {rules.map((rule) => (
                <tr className={rule.enabled ? '' : 'row-muted'} key={rule.id}>
                  <td><Bell size={13} /> <strong>{label(rule.event)}</strong></td>
                  <td>{t(`rules.channels.${rule.channel}`)}</td>
                  <td>{rule.summary}</td>
                  <td>{t(`rules.frequencies.${rule.frequency}`)}</td>
                  <td><span className={`approval-status ${rule.enabled ? 'approved' : 'withdrawn'}`}>{rule.enabled ? t('rules.enabled') : t('rules.disabled')}</span></td>
                  <td className="row-actions">
                    {canEdit && <button className="text-button" onClick={() => setEditing(rule)} type="button">{t('rules.edit')}</button>}
                    {canEdit && <button className="text-button" onClick={() => setDeleting(rule)} type="button">{t('rules.delete')}</button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {editing && catalog && (
        <RuleDialog catalog={catalog} label={label} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); setNotice(t('rules.saved')); load() }} rule={editing === 'new' ? null : editing} tenantId={tenantId} />
      )}
      {deleting && <ConfirmDialog body={t('rules.deleteBody', { event: label(deleting.event) })} danger onCancel={() => setDeleting(null)} onConfirm={() => remove(deleting)} title={t('rules.delete')} />}
    </section>
  )
}

function RuleDialog({ catalog, rule, tenantId, label, onClose, onSaved }: { catalog: Catalog; rule: Rule | null; tenantId: string; label: (key: string) => string; onClose: () => void; onSaved: () => void }) {
  const { t } = useTranslation('settings')
  const [draft, setDraft] = useState<Draft>(rule ? draftOf(rule) : blank(catalog.events[0].key))
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const set = (next: Partial<Draft>) => setDraft((current) => ({ ...current, ...next }))
  const toggle = (key: 'members' | 'roles' | 'webhooks', id: string) => set({ [key]: draft[key].includes(id) ? draft[key].filter((item) => item !== id) : [...draft[key], id] } as Partial<Draft>)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setSaving(true)
    setError('')
    try {
      await apiRequest(rule ? `/api/v1/notification-rules/${rule.id}` : '/api/v1/notification-rules', { method: rule ? 'PUT' : 'POST', body: JSON.stringify(toBody(draft, rule)) }, tenantId)
      onSaved()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('rules.failed'))
      setSaving(false)
    }
  }

  return (
    <div className="modal-backdrop confirm-backdrop" role="presentation" onClick={onClose}>
      <form aria-modal="true" className="create-modal rule-dialog" onClick={(event) => event.stopPropagation()} onSubmit={(event) => void submit(event)} role="dialog" aria-label={rule ? t('rules.edit') : t('rules.new')}>
        <button aria-label={t('rules.close')} className="modal-close" onClick={onClose} type="button"><X size={18} /></button>
        <h2>{rule ? t('rules.edit') : t('rules.new')}</h2>
        <label>{t('rules.event')}
          <select onChange={(event) => set({ event: event.target.value })} value={draft.event}>{catalog.events.map((item) => <option key={item.key} value={item.key}>{label(item.key)}</option>)}</select>
        </label>
        <label>{t('rules.channel')}
          <select onChange={(event) => set({ channel: event.target.value })} value={draft.channel}>
            {catalog.channels.map((item) => <option disabled={!item.available} key={item.key} value={item.key}>{t(`rules.channels.${item.key}`)}{item.available ? '' : `（${t('rules.unconfigured')}）`}</option>)}
          </select>
        </label>
        {draft.channel === 'email' && (
          <>
            <fieldset><legend>{t('rules.members')}</legend>
              <div className="rule-checks">{catalog.members.map((item) => <label className="wizard-radio" key={item.id}><input checked={draft.members.includes(item.id)} onChange={() => toggle('members', item.id)} type="checkbox" /> {item.name}</label>)}</div>
            </fieldset>
            <fieldset><legend>{t('rules.roles')}</legend>
              <div className="rule-checks">{catalog.roles.map((item) => <label className="wizard-radio" key={item.id}><input checked={draft.roles.includes(item.id)} onChange={() => toggle('roles', item.id)} type="checkbox" /> {item.name}</label>)}</div>
              <small className="wizard-muted">{t('rules.rolesHint')}</small>
            </fieldset>
            <label>{t('rules.emails')}<textarea onChange={(event) => set({ emails: event.target.value })} placeholder="ops@example.com" rows={2} value={draft.emails} /></label>
          </>
        )}
        {draft.channel === 'slack' && (
          <label>{t('rules.slackUrl')}
            <input autoComplete="off" onChange={(event) => set({ url: event.target.value })} placeholder={rule && rule.channel === 'slack' ? String(rule.recipients.url_mask ?? '') : 'https://hooks.slack.com/services/…'} type="url" value={draft.url} />
            <small className="wizard-muted">{rule?.channel === 'slack' ? t('rules.slackKeep') : t('rules.slackHint')}</small>
          </label>
        )}
        {draft.channel === 'sms' && <label>{t('rules.phones')}<textarea onChange={(event) => set({ phones: event.target.value })} placeholder="+8613800138000" rows={2} value={draft.phones} /></label>}
        {draft.channel === 'webhook' && (
          <fieldset><legend>{t('rules.webhooks')}</legend>
            {catalog.webhooks.length === 0 && <small className="wizard-muted">{t('rules.noWebhooks')}</small>}
            <div className="rule-checks">{catalog.webhooks.map((item) => <label className="wizard-radio" key={item.id}><input checked={draft.webhooks.includes(item.id)} onChange={() => toggle('webhooks', item.id)} type="checkbox" /> {item.name}</label>)}</div>
          </fieldset>
        )}
        <label>{t('rules.frequency')}
          <select onChange={(event) => set({ frequency: event.target.value })} value={draft.frequency}>{catalog.frequencies.map((item) => <option key={item} value={item}>{t(`rules.frequencies.${item}`)}</option>)}</select>
          <small className="wizard-muted">{t(`rules.frequencyHint.${draft.frequency}`)}</small>
        </label>
        <label className="governance-inherit"><input checked={draft.enabled} onChange={(event) => set({ enabled: event.target.checked })} type="checkbox" /><span><strong>{t('rules.enabled')}</strong></span></label>
        {error && <p className="form-error" role="alert">{error}</p>}
        <div className="confirm-actions">
          <button className="secondary-button" onClick={onClose} type="button">{t('rules.cancel')}</button>
          <button className="primary-button" disabled={saving} type="submit">{saving ? t('rules.saving') : t('rules.save')}</button>
        </div>
      </form>
    </div>
  )
}
