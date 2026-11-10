import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, Copy, Plus, Search, Trash2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link, useSearchParams } from 'react-router-dom'
import { apiRequest } from '../../api'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import type { TenantMembership } from '../../types'
import { FlowIcon } from './FlowIcon'
import { KINDS, parseKind } from './types'
import type { FlowCounts, FlowKind, FlowPage, FlowReferences, FlowSummary } from './types'

/** Flow studio list (AGT-001–007): Chatflows and Workflows, selected by the ``type`` query parameter. */
export function FlowsPage({ tenant }: { tenant: TenantMembership }) {
  const { t } = useTranslation('flows')
  const [params, setParams] = useSearchParams()
  const kind = parseKind(params.get('type'))
  const writable = tenant.access_mode === 'full'
  const canEdit = writable && tenant.permissions.includes('agents:edit')
  const canDelete = writable && tenant.permissions.includes('agents:delete')
  const [items, setItems] = useState<FlowSummary[]>([])
  const [counts, setCounts] = useState<FlowCounts | null>(null)
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [corrupted, setCorrupted] = useState(false)
  const [deleting, setDeleting] = useState<{ flow: FlowSummary; references: number } | null>(null)

  const load = useCallback(() => {
    const query = new URLSearchParams({ kind, limit: '100' })
    if (search.trim()) query.set('q', search.trim())
    Promise.all([
      apiRequest<FlowPage>(`/api/v1/agents?${query}`, {}, tenant.id),
      apiRequest<FlowCounts>('/api/v1/agents/summary', {}, tenant.id),
    ])
      .then(([page, summary]) => { setItems(page.items); setCounts(summary); setError('') })
      .catch((requestError: Error) => setError(requestError.message || t('loadFailed')))
      .finally(() => setLoading(false))
  }, [kind, search, tenant.id, t])

  useEffect(() => {
    const timer = window.setTimeout(load, 200)
    return () => window.clearTimeout(timer)
  }, [load])

  function selectKind(next: FlowKind) {
    setSearch('')
    setCorrupted(false)
    setNotice('')
    setParams(next === 'chatflow' ? {} : { type: next })
  }

  async function askDelete(flow: FlowSummary) {
    try {
      const references = await apiRequest<FlowReferences>(`/api/v1/agents/${flow.id}/references`, {}, tenant.id)
      setDeleting({ flow, references: references.total })
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
  }

  async function duplicate(flow: FlowSummary) {
    try {
      const copy = await apiRequest<FlowSummary>(`/api/v1/agents/${flow.id}/duplicate`, { method: 'POST' }, tenant.id)
      setNotice(t('duplicated', { name: copy.name }))
      load()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
  }

  async function confirmDelete() {
    if (!deleting) return
    try {
      await apiRequest(`/api/v1/agents/${deleting.flow.id}`, { method: 'DELETE' }, tenant.id)
      setNotice(t('deleted'))
      load()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
    setDeleting(null)
  }

  const kindName = t(`kind.${kind}`)
  const createLink = kind === 'chatflow' ? '/flow-studio/new' : `/flow-studio/new?type=${kind}`

  return (
    <section className="flows-page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">FLOW STUDIO</span>
          <h1>{t(`pageTitle.${kind}`)}</h1>
          <p>{t(`pageSubtitle.${kind}`)}</p>
        </div>
        {canEdit && <Link className="primary-button" to={createLink}><Plus size={16} /> {t('create', { kind: kindName })}</Link>}
      </div>
      <div className="flows-kind-tabs" role="tablist">
        {KINDS.map((value) => (
          <button aria-selected={kind === value} className={kind === value ? 'selected' : ''} key={value} onClick={() => selectKind(value)} role="tab" type="button">
            <strong>{t(`kind.${value}`)}</strong>
            <b>{counts ? counts[value] : '–'}</b>
            <small>{t(`kindHint.${value}`)}</small>
          </button>
        ))}
      </div>
      <div className="knowledge-toolbar">
        <label className="search-field"><Search size={15} /><input aria-label={t('search', { kind: kindName })} onChange={(event) => setSearch(event.target.value)} placeholder={t('search', { kind: kindName })} value={search} /></label>
      </div>
      {corrupted && <p className="form-error" role="alert"><AlertTriangle size={14} /> {t('corrupted')}</p>}
      {notice && <p className="assistants-notice" role="status">{notice}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      {loading && <p>{t('loading')}</p>}
      <div className="flows-grid">
        {items.map((flow) => (
          <article className="flows-card" key={flow.id}>
            <header>
              <span className={`flows-icon ${flow.kind}`}><FlowIcon name={flow.icon} /></span>
              <span className={`assistant-status flows-status-${flow.status}`}>{t(`status.${flow.status}`)}</span>
            </header>
            {flow.config_corrupted ? (
              <button className="flows-card-body" onClick={() => setCorrupted(true)} type="button">
                <h3>{flow.name}</h3>
                <p className="form-error">{t('corruptedShort')}</p>
              </button>
            ) : (
              <Link className="flows-card-body" to={`/flow-studio/${flow.id}`}>
                <h3>{flow.name}</h3>
                <p>{flow.description || t('defaultDescription')}</p>
              </Link>
            )}
            <div className="flows-meta">
              <span className="flows-tag">{t(`kind.${flow.kind}`)}</span>
              <span>{flow.live_version ? t('versionLive', { version: flow.live_version }) : t('nodeCount', { count: flow.node_count })}</span>
            </div>
            <footer>
              {flow.config_corrupted ? <span /> : <Link to={`/flow-studio/${flow.id}`}>{t('configure')}</Link>}
              <span className="flows-card-actions">
              {canEdit && !flow.config_corrupted && <button aria-label={t('duplicateAria', { name: flow.name })} className="icon-button" onClick={() => void duplicate(flow)} type="button"><Copy size={15} /></button>}
              {canDelete && (
                <button aria-label={t('deleteAria', { name: flow.name })} className="icon-button" onClick={() => void askDelete(flow)} type="button"><Trash2 size={15} /></button>
              )}
              </span>
            </footer>
          </article>
        ))}
      </div>
      {!loading && !error && items.length === 0 && (
        <div className="flows-empty">
          <p>{search.trim() ? t('emptyFiltered') : t('empty', { kind: kindName })}</p>
          {canEdit && !search.trim() && <Link className="primary-button" to={createLink}><Plus size={16} /> {t('create', { kind: kindName })}</Link>}
        </div>
      )}
      {deleting && (
        <ConfirmDialog
          body={deleting.references > 0 ? t('deleteBodyReferenced', { kind: kindName, count: deleting.references }) : t('deleteBody', { kind: kindName })}
          danger
          onCancel={() => setDeleting(null)}
          onConfirm={() => confirmDelete()}
          title={t('deleteTitle', { kind: kindName })}
        />
      )}
    </section>
  )
}
