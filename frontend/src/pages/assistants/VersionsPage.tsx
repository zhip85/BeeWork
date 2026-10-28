import { useCallback, useEffect, useState } from 'react'
import { ArrowLeft, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link, useParams } from 'react-router-dom'
import { apiRequest } from '../../api'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import { formatRelativeTime } from '../../i18n/format'
import type { TenantMembership } from '../../types'
import { ChangeList } from './VersionDiff'
import type { VersionChange } from './VersionDiff'
import type { Assistant } from './types'

type Version = {
  id: string
  version: string
  kind: 'publish' | 'rollback'
  change_note: string
  published_by: string | null
  published_at: string
  is_live: boolean
}
type VersionDetail = Version & { snapshot: Record<string, unknown>; change_diff: VersionChange[] }
type VersionList = { items: Version[]; live_version_id: string | null; draft_version: string }
type Pending = { kind: 'restore' | 'rollback'; version: Version }

export function VersionsPage({ tenant }: { tenant: TenantMembership }) {
  const { t } = useTranslation('versions')
  const { assistantId = '' } = useParams()
  const tenantId = tenant.id
  const writable = tenant.access_mode === 'full'
  const can = (permission: string) => writable && tenant.permissions.includes(permission)

  const [data, setData] = useState<VersionList | null>(null)
  const [assistant, setAssistant] = useState<Assistant | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [viewing, setViewing] = useState<VersionDetail | null>(null)
  const [pending, setPending] = useState<Pending | null>(null)
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('draft')
  const [changes, setChanges] = useState<VersionChange[] | null>(null)
  const [reload, setReload] = useState(0)

  useEffect(() => {
    let active = true
    Promise.all([
      apiRequest<VersionList>(`/api/v1/assistants/${assistantId}/versions`, {}, tenantId),
      apiRequest<Assistant>(`/api/v1/assistants/${assistantId}`, {}, tenantId),
    ])
      .then(([list, current]) => {
        if (!active) return
        setData(list)
        setAssistant(current)
        setFrom((value) => value || list.items[0]?.id || '')
        setError('')
      })
      .catch((requestError: Error) => active && setError(requestError.message || t('page.loadFailed')))
    return () => { active = false }
  }, [assistantId, tenantId, reload, t])

  const refresh = useCallback(() => setReload((value) => value + 1), [])

  async function openVersion(version: Version) {
    try {
      setViewing(await apiRequest<VersionDetail>(`/api/v1/assistants/${assistantId}/versions/${version.id}`, {}, tenantId))
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('page.loadFailed'))
    }
  }

  async function compare() {
    if (!from) return
    try {
      const result = await apiRequest<{ changes: VersionChange[] }>(
        `/api/v1/assistants/${assistantId}/versions/compare?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`, {}, tenantId,
      )
      setChanges(result.changes)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('page.loadFailed'))
    }
  }

  async function confirm(action: Pending) {
    try {
      if (action.kind === 'restore') {
        await apiRequest(`/api/v1/assistants/${assistantId}/versions/${action.version.id}/restore`, {
          method: 'POST', body: JSON.stringify({ row_version: assistant?.row_version ?? 1 }),
        }, tenantId)
        setNotice(t('confirm.restored'))
      } else {
        const created = await apiRequest<Version>(`/api/v1/assistants/${assistantId}/versions/${action.version.id}/rollback`, {
          method: 'POST', body: JSON.stringify({}),
        }, tenantId)
        setNotice(t('confirm.rolledBack', { version: created.version }))
      }
      setError('')
      refresh()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('page.loadFailed'))
    } finally {
      setPending(null)
    }
  }

  return (
    <section className="settings-page versions-page">
      <Link className="text-button" to="/assistants"><ArrowLeft size={14} /> {t('page.back')}</Link>
      <div className="page-heading"><div><p className="section-kicker">{assistant?.name ?? ''}</p><h1>{t('page.title')}</h1><p>{t('page.subtitle')}</p></div></div>
      {notice && <p className="assistants-notice" role="status">{notice}</p>}
      {error && <p className="form-error" role="alert">{error} <button className="text-button" onClick={refresh} type="button">{t('page.retry')}</button></p>}
      {!data && !error && <p>{t('page.loading')}</p>}
      {data && <p className="wizard-muted">{t('page.draftVersion', { version: data.draft_version })}</p>}
      {data && data.items.length === 0 && <p className="assistants-empty-filtered">{t('page.empty')}</p>}
      {data && data.items.length > 0 && (
        <ol className="version-timeline">
          {data.items.map((version) => (
            <li key={version.id}>
              <header>
                <strong>v{version.version}</strong>
                <span className={`assistant-status ${version.is_live ? 'published' : 'draft'}`}>{version.is_live ? t('page.live') : t('page.history')}</span>
                {version.kind === 'rollback' && <span className="assistant-status handoff">{t('page.kindRollback')}</span>}
              </header>
              <p>{version.change_note || t('page.noNote')}</p>
              <small>{t('page.by', { name: version.published_by ?? '—', time: formatRelativeTime(version.published_at) })}</small>
              <div className="version-actions">
                <button className="text-button" onClick={() => void openVersion(version)} type="button">{t('actions.view')}</button>
                {can('assistants:edit') && assistant?.can_edit && <button className="text-button" onClick={() => setPending({ kind: 'restore', version })} type="button">{t('actions.restore')}</button>}
                {can('assistants:publish') && assistant?.can_edit && !version.is_live && assistant.status !== 'disabled' && <button className="text-button" onClick={() => setPending({ kind: 'rollback', version })} type="button">{t('actions.rollback')}</button>}
              </div>
            </li>
          ))}
        </ol>
      )}

      {data && data.items.length > 0 && (
        <div className="version-compare">
          <h2>{t('compare.title')}</h2>
          <div className="assistants-toolbar">
            <label>{t('compare.from')}
              <select onChange={(event) => setFrom(event.target.value)} value={from}>
                {data.items.map((version) => <option key={version.id} value={version.id}>v{version.version}</option>)}
              </select>
            </label>
            <label>{t('compare.to')}
              <select onChange={(event) => setTo(event.target.value)} value={to}>
                <option value="draft">{t('compare.draft')}</option>
                {data.items.map((version) => <option key={version.id} value={version.id}>v{version.version}</option>)}
              </select>
            </label>
            <button className="secondary-button" onClick={() => void compare()} type="button">{t('compare.run')}</button>
          </div>
          {changes && (changes.length === 0 ? <p className="wizard-muted">{t('compare.identical')}</p> : <ChangeList changes={changes} />)}
        </div>
      )}

      {viewing && (
        <div className="modal-backdrop" onClick={() => setViewing(null)} role="presentation">
          <aside aria-modal="true" className="conversation-drawer" onClick={(event) => event.stopPropagation()} role="dialog">
            <header><h2>{t('view.title', { version: viewing.version })}</h2><button aria-label={t('actions.close')} className="modal-close" onClick={() => setViewing(null)} type="button"><X size={18} /></button></header>
            <p className="wizard-muted">{t('view.readOnly')}</p>
            <h3>{t('view.changes')}</h3>
            {viewing.change_diff.length === 0 ? <p className="wizard-muted">{t('view.noChanges')}</p> : <ChangeList changes={viewing.change_diff} />}
            <h3>{t('view.snapshot')}</h3>
            <pre className="version-lines">{JSON.stringify(viewing.snapshot, null, 2)}</pre>
          </aside>
        </div>
      )}

      {pending && (
        <ConfirmDialog
          body={t(pending.kind === 'restore' ? 'confirm.restoreBody' : 'confirm.rollbackBody', { version: pending.version.version })}
          onCancel={() => setPending(null)}
          onConfirm={() => confirm(pending)}
          title={t(pending.kind === 'restore' ? 'confirm.restoreTitle' : 'confirm.rollbackTitle', { version: pending.version.version })}
        />
      )}
    </section>
  )
}
