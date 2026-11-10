import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import type { TenantMembership } from '../../types'
import { PRESET_CATEGORIES } from './types'
import type { Audience, PortalApp, SourceEntry } from './types'

type Org = { id: string; name: string; depth: number }

/** Add / edit a portal app (演进 PRD 10.1–10.2). The source can be chosen when adding and, while the app is listed, is
 * locked: the app never silently starts pointing at another service. */
export function AppDialog({ tenant, app, onClose, onSaved }: { tenant: TenantMembership; app: PortalApp | null; onClose: () => void; onSaved: (message: string) => void }) {
  const { t } = useTranslation('portal')
  const [sources, setSources] = useState<SourceEntry[]>([])
  const [orgs, setOrgs] = useState<Org[]>([])
  const [sourceKey, setSourceKey] = useState(app && app.source_id ? `${app.source_type}:${app.source_id}` : '')
  const [name, setName] = useState(app?.name ?? '')
  const [description, setDescription] = useState(app?.description ?? '')
  const [category, setCategory] = useState(app?.category ?? t(`category.${PRESET_CATEGORIES[1]}`))
  const [audience, setAudience] = useState<Audience>(app?.audience ?? 'all')
  const [orgIds, setOrgIds] = useState<string[]>(app?.organization_ids ?? [])
  const [featured, setFeatured] = useState(app?.featured ?? false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const locked = app?.status === 'listed'

  useEffect(() => {
    apiRequest<{ items: SourceEntry[] }>('/api/v1/portal/app-sources', {}, tenant.id).then((r) => setSources(r.items)).catch(() => setSources([]))
    apiRequest<{ organizations: Org[] }>('/api/v1/organizations', {}, tenant.id).then((r) => setOrgs(r.organizations)).catch(() => setOrgs([]))
  }, [tenant.id])

  const presets = PRESET_CATEGORIES.map((key) => t(`category.${key}`))
  const categoryOptions = presets.includes(category) || !category ? presets : [category, ...presets] // keep a custom historical value
  const selected = sources.find((entry) => `${entry.source_type}:${entry.source_id}` === sourceKey)

  function pickSource(key: string) {
    setSourceKey(key)
    const entry = sources.find((item) => `${item.source_type}:${item.source_id}` === key)
    if (entry && !name.trim()) setName(entry.name.slice(0, 50))
    if (entry && !description.trim()) setDescription(entry.description.slice(0, 200))
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    setSaving(true)
    setError('')
    const [sourceType, sourceId] = sourceKey.split(':')
    try {
      await apiRequest<PortalApp>(app ? `/api/v1/portal/apps/${app.id}` : '/api/v1/portal/apps', {
        method: app ? 'PUT' : 'POST',
        body: JSON.stringify({
          source_type: sourceType || null, source_id: sourceId || null, name: name.trim(), description: description.trim(), category,
          audience, organization_ids: audience === 'orgs' ? orgIds : [], mode: 'chat', icon: app?.icon ?? 'sparkles', featured,
          ...(app ? { row_version: app.row_version } : {}),
        }),
      }, tenant.id)
      onSaved(t(app ? 'saved' : 'added'))
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
      setSaving(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose} role="presentation">
      <form aria-labelledby="portal-app-title" aria-modal="true" className="create-modal portal-app-dialog" onClick={(event) => event.stopPropagation()} onSubmit={(event) => void submit(event)} role="dialog">
        <span className="eyebrow">AI PORTAL</span>
        <h2 id="portal-app-title">{t(app ? 'editApp' : 'addApp')}</h2>
        <label className="kb-field">
          <span>{t('source')} <em>*</em></span>
          <select disabled={locked} onChange={(event) => pickSource(event.target.value)} required value={sourceKey}>
            <option value="">{t('sourcePlaceholder')}</option>
            {app && app.source_id && !selected && <option value={sourceKey}>{app.source_name ?? t('sourceMissing')}</option>}
            {sources.map((entry) => <option key={`${entry.source_type}:${entry.source_id}`} value={`${entry.source_type}:${entry.source_id}`}>{entry.name}</option>)}
          </select>
          <small>{locked ? t('sourceLocked') : sources.length === 0 ? t('noSources') : t('sourceHint')}</small>
        </label>
        <label className="kb-field">
          <span>{t('appName')} <em>*</em></span>
          <input maxLength={50} onChange={(event) => setName(event.target.value)} required value={name} />
        </label>
        <label className="kb-field">
          <span>{t('appDescription')}</span>
          <textarea maxLength={200} onChange={(event) => setDescription(event.target.value)} rows={2} value={description} />
        </label>
        <div className="portal-app-row">
          <label className="kb-field">
            <span>{t('categoryLabel')}</span>
            <select onChange={(event) => setCategory(event.target.value)} value={category}>
              {categoryOptions.map((option) => <option key={option} value={option}>{option}</option>)}
            </select>
          </label>
          <label className="kb-field">
            <span>{t('audienceLabel')}</span>
            <select onChange={(event) => setAudience(event.target.value as Audience)} value={audience}>
              <option value="all">{t('audience.all')}</option>
              <option value="orgs">{t('audience.orgs')}</option>
              <option value="admins">{t('audience.admins')}</option>
            </select>
          </label>
        </div>
        {audience === 'orgs' && (
          <fieldset className="portal-org-list">
            <legend>{t('pickOrganizations')}</legend>
            {orgs.map((org) => (
              <label key={org.id} style={{ paddingLeft: `${org.depth * 14}px` }}>
                <input checked={orgIds.includes(org.id)} onChange={(event) => setOrgIds(event.target.checked ? [...orgIds, org.id] : orgIds.filter((id) => id !== org.id))} type="checkbox" />
                {org.name}
              </label>
            ))}
            {orgs.length === 0 && <small>{t('noOrganizations')}</small>}
          </fieldset>
        )}
        <label className="portal-featured"><input checked={featured} onChange={(event) => setFeatured(event.target.checked)} type="checkbox" /> {t('featured')}</label>
        {error && <p className="form-error" role="alert">{error}</p>}
        <div className="confirm-actions">
          <button className="secondary-button" disabled={saving} onClick={onClose} type="button">{t('cancel')}</button>
          <button className="primary-button" disabled={saving || !sourceKey || !name.trim()} type="submit">{saving ? t('saving') : t(app ? 'save' : 'addAndKeepDraft')}</button>
        </div>
      </form>
    </div>
  )
}
