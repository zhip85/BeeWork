import { useCallback, useEffect, useState } from 'react'
import { Plus, Search } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import type { TenantMembership } from '../../types'
import { AppDialog } from './AppDialog'
import { SettingsPanel } from './SettingsPanel'
import type { PortalApp, Summary } from './types'

const TABS = ['apps', 'settings', 'login', 'analytics'] as const
const READY: readonly string[] = ['apps', 'settings']

/** AI portal for administrators (演进 PRD 10): put published agents in front of employees. Settings, login and
 * analytics arrive with T-141 / T-146 / T-147 and say so rather than pretending. */
export function PortalAdminPage({ tenant }: { tenant: TenantMembership }) {
  const { t } = useTranslation('portal')
  const writable = tenant.access_mode === 'full'
  const [tab, setTab] = useState<(typeof TABS)[number]>('apps')
  const [apps, setApps] = useState<PortalApp[]>([])
  const [summary, setSummary] = useState<Summary | null>(null)
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [editing, setEditing] = useState<PortalApp | 'new' | null>(null)
  const [confirm, setConfirm] = useState<{ kind: 'list' | 'delist' | 'delete'; app: PortalApp } | null>(null)

  const load = useCallback(() => {
    const params = new URLSearchParams()
    if (search.trim()) params.set('q', search.trim())
    Promise.all([
      apiRequest<PortalApp[]>(`/api/v1/portal/apps?${params}`, {}, tenant.id),
      apiRequest<Summary>('/api/v1/portal/summary', {}, tenant.id),
    ])
      .then(([list, counts]) => { setApps(list); setSummary(counts); setError('') })
      .catch((requestError: Error) => setError(requestError.message || t('loadFailed')))
      .finally(() => setLoading(false))
  }, [search, tenant.id, t])

  useEffect(() => {
    const timer = window.setTimeout(load, 200)
    return () => window.clearTimeout(timer)
  }, [load])

  async function runConfirmed() {
    if (!confirm) return
    const { kind, app } = confirm
    try {
      if (kind === 'delete') await apiRequest(`/api/v1/portal/apps/${app.id}`, { method: 'DELETE' }, tenant.id)
      else await apiRequest(`/api/v1/portal/apps/${app.id}/${kind}`, { method: 'POST', body: JSON.stringify({}) }, tenant.id)
      setNotice(t(`done.${kind}`))
      setError('')
      load()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
    setConfirm(null)
  }

  return (
    <section className="portal-admin">
      <div className="page-heading">
        <div>
          <span className="eyebrow">{t('eyebrow')}</span>
          <h1>{t('title')}</h1>
          <p>{t('subtitle')}</p>
        </div>
      </div>
      <div className="portal-tabs" role="tablist">
        {TABS.map((key) => (
          <button aria-selected={tab === key} className={tab === key ? 'selected' : ''} disabled={!READY.includes(key)} key={key} onClick={() => setTab(key)} role="tab" title={READY.includes(key) ? undefined : t('soon')} type="button">
            {t(`tabs.${key}`)}{!READY.includes(key) && <small>{t('soon')}</small>}
          </button>
        ))}
      </div>
      {tab === 'settings' && <SettingsPanel tenant={tenant} />}
      {tab === 'apps' && (
        <>
      <div className="portal-stats">
        <div><small>{t('stats.listed')}</small><strong>{summary?.listed ?? '–'}</strong></div>
        <div><small>{t('stats.mau')}</small><strong>{summary?.monthly_active_employees ?? '–'}</strong></div>
        <div><small>{t('stats.calls')}</small><strong>{summary?.monthly_calls ?? '–'}</strong></div>
      </div>
      <div className="knowledge-toolbar">
        <label className="search-field"><Search size={15} /><input aria-label={t('search')} onChange={(event) => setSearch(event.target.value)} placeholder={t('search')} value={search} /></label>
        {writable && tenant.permissions.includes('portal:edit') && <button className="primary-button" onClick={() => setEditing('new')} type="button"><Plus size={16} /> {t('addApp')}</button>}
      </div>
      {notice && <p className="assistants-notice" role="status">{notice}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      {loading && <p>{t('loading')}</p>}
      {!loading && !error && apps.length === 0 && <p className="assistants-empty-filtered">{search.trim() ? t('emptyFiltered') : t('empty')}</p>}
      <div className="portal-app-grid">
        {apps.map((app) => (
          <article className="portal-app-card" key={app.id}>
            <header>
              <h3>{app.name}</h3>
              <span className={`assistant-status portal-status-${app.status}`}>{t(`status.${app.status}`)}</span>
              {app.featured && <span className="assistant-status published">{t('featuredBadge')}</span>}
            </header>
            <p>{app.description || t('noDescription')}</p>
            <dl>
              <div><dt>{t('categoryLabel')}</dt><dd>{app.category || '–'}</dd></div>
              <div><dt>{t('audienceLabel')}</dt><dd>{app.audience === 'orgs' ? app.organization_names.join('、') || t('audience.orgs') : t(`audience.${app.audience}`)}</dd></div>
              <div><dt>{t('source')}</dt><dd className={app.source_state === 'available' ? '' : 'invalid'}>{app.source_state === 'unset' ? t('sourceUnset') : app.source_state === 'unavailable' ? t('sourceUnavailable') : app.source_name}</dd></div>
            </dl>
            {app.status === 'delisted' && app.delisted_reason && <small>{t('delistedBecause', { reason: app.delisted_reason })}</small>}
            {writable && (
              <footer>
                {app.can_edit && <button className="secondary-button" onClick={() => setEditing(app)} type="button">{t('edit')}</button>}
                {app.can_publish && app.status !== 'listed' && <button className="primary-button" onClick={() => setConfirm({ kind: 'list', app })} type="button">{t('list')}</button>}
                {app.can_publish && app.status === 'listed' && <button className="secondary-button" onClick={() => setConfirm({ kind: 'delist', app })} type="button">{t('delist')}</button>}
                {app.can_edit && app.status !== 'listed' && <button className="text-button danger" onClick={() => setConfirm({ kind: 'delete', app })} type="button">{t('delete')}</button>}
              </footer>
            )}
          </article>
        ))}
      </div>
        </>
      )}
      {editing && <AppDialog app={editing === 'new' ? null : editing} onClose={() => setEditing(null)} onSaved={(message) => { setEditing(null); setNotice(message); load() }} tenant={tenant} />}
      {confirm && (
        <ConfirmDialog
          body={t(`confirm.${confirm.kind}Body`, { name: confirm.app.name })}
          confirmLabel={t(confirm.kind)}
          danger={confirm.kind !== 'list'}
          onCancel={() => setConfirm(null)}
          onConfirm={runConfirmed}
          title={t(`confirm.${confirm.kind}Title`, { name: confirm.app.name })}
        />
      )}
    </section>
  )
}
