import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import type { TenantMembership } from '../../types'

type Settings = {
  portal_name: string
  input_placeholder: string
  default_audience: 'all' | 'orgs' | 'admins'
  answer_style: 'concise' | 'balanced' | 'detailed'
  nav_search: boolean
  nav_projects: boolean
  nav_app_library: boolean
  nav_recent: boolean
  nav_pinned: boolean
  file_upload: boolean
  voice_input: boolean
  profile_edit: boolean
  personal_memory: boolean
  task_notifications: boolean
  delisted_history: 'readonly' | 'hidden'
}
type Loaded = Settings & { row_version: number; can_edit: boolean }

const GROUPS: { key: string; switches: (keyof Settings)[] }[] = [
  { key: 'nav', switches: ['nav_search', 'nav_projects', 'nav_app_library', 'nav_recent', 'nav_pinned'] },
  { key: 'session', switches: ['file_upload', 'voice_input'] },
  { key: 'personal', switches: ['profile_edit', 'personal_memory', 'task_notifications'] },
]

/** Portal settings (演进 PRD 10.4): brand and defaults, navigation, conversation abilities, what employees may change.
 * The server enforces every switch; the employee side only reads them to hide menus. */
export function SettingsPanel({ tenant }: { tenant: TenantMembership }) {
  const { t } = useTranslation('portal')
  const [loaded, setLoaded] = useState<Loaded | null>(null)
  const [form, setForm] = useState<Settings | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    apiRequest<Loaded>('/api/v1/portal/settings', {}, tenant.id)
      .then((data) => { setLoaded(data); setForm(data) })
      .catch((requestError: Error) => setError(requestError.message || t('loadFailed')))
  }, [tenant.id, t])

  const canEdit = tenant.access_mode === 'full' && loaded?.can_edit === true
  const dirty = Boolean(loaded && form && (Object.keys(form) as (keyof Settings)[]).some((key) => form[key] !== loaded[key]))

  useEffect(() => {
    if (!dirty) return
    const guard = (event: BeforeUnloadEvent) => { event.preventDefault() }
    window.addEventListener('beforeunload', guard)
    return () => window.removeEventListener('beforeunload', guard)
  }, [dirty])

  function change<K extends keyof Settings>(key: K, value: Settings[K]) {
    setForm((current) => (current ? { ...current, [key]: value } : current))
    setNotice('')
  }

  async function save() {
    if (!form || !loaded) return
    setSaving(true)
    setError('')
    try {
      const saved = await apiRequest<Loaded>('/api/v1/portal/settings', { method: 'PUT', body: JSON.stringify({ ...form, row_version: loaded.row_version }) }, tenant.id)
      setLoaded(saved)
      setForm(saved)
      setNotice(t('settings.saved'))
    } catch (requestError) {
      // Nothing was saved: say so, never show a success (演进 PRD 16.2).
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
    setSaving(false)
  }

  if (!form) return error ? <p className="form-error" role="alert">{error}</p> : <p>{t('loading')}</p>

  return (
    <div className="portal-settings">
      <div className="portal-settings-bar">
        <span>{dirty ? t('settings.unsaved') : t('settings.upToDate')}</span>
        <button className="secondary-button" disabled title={t('settings.previewSoon')} type="button">{t('settings.preview')}</button>
        {canEdit && <button className="primary-button" disabled={saving || !dirty} onClick={() => void save()} type="button">{saving ? t('saving') : t('settings.save')}</button>}
      </div>
      {notice && <p className="assistants-notice" role="status">{notice}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      <fieldset className="detail-card" disabled={!canEdit}>
        <legend><h2>{t('settings.brand.title')}</h2></legend>
        <p className="wizard-muted">{t('settings.brand.hint')}</p>
        <label className="kb-field"><span>{t('settings.brand.name')}</span><input maxLength={50} onChange={(event) => change('portal_name', event.target.value)} placeholder={t('settings.brand.namePlaceholder', { tenant: tenant.name })} value={form.portal_name} /></label>
        <label className="kb-field"><span>{t('settings.brand.placeholder')}</span><input maxLength={100} onChange={(event) => change('input_placeholder', event.target.value)} placeholder={t('settings.brand.placeholderDefault')} value={form.input_placeholder} /></label>
        <div className="portal-app-row">
          <label className="kb-field">
            <span>{t('settings.brand.audience')}</span>
            <select onChange={(event) => change('default_audience', event.target.value as Settings['default_audience'])} value={form.default_audience}>
              <option value="all">{t('audience.all')}</option>
              <option value="orgs">{t('audience.orgs')}</option>
              <option value="admins">{t('audience.admins')}</option>
            </select>
          </label>
          <label className="kb-field">
            <span>{t('settings.brand.style')}</span>
            <select onChange={(event) => change('answer_style', event.target.value as Settings['answer_style'])} value={form.answer_style}>
              {(['concise', 'balanced', 'detailed'] as const).map((value) => <option key={value} value={value}>{t(`settings.style.${value}`)}</option>)}
            </select>
          </label>
        </div>
      </fieldset>
      {GROUPS.map((group) => (
        <fieldset className="detail-card" disabled={!canEdit} key={group.key}>
          <legend><h2>{t(`settings.${group.key}.title`)}</h2></legend>
          <p className="wizard-muted">{t(`settings.${group.key}.hint`)}</p>
          <ul className="portal-switches">
            {group.switches.map((key) => (
              <li key={key}>
                <label>
                  <input checked={Boolean(form[key])} onChange={(event) => change(key, event.target.checked as never)} type="checkbox" />
                  <span><strong>{t(`settings.switch.${key}.label`)}</strong><small>{t(`settings.switch.${key}.hint`)}</small></span>
                </label>
              </li>
            ))}
          </ul>
        </fieldset>
      ))}
      <fieldset className="detail-card" disabled={!canEdit}>
        <legend><h2>{t('settings.delisted.title')}</h2></legend>
        <p className="wizard-muted">{t('settings.delisted.hint')}</p>
        <label className="kb-field">
          <select onChange={(event) => change('delisted_history', event.target.value as Settings['delisted_history'])} value={form.delisted_history}>
            <option value="readonly">{t('settings.delisted.readonly')}</option>
            <option value="hidden">{t('settings.delisted.hidden')}</option>
          </select>
        </label>
      </fieldset>
    </div>
  )
}
